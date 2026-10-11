package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"regexp"
	"strconv"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
	"github.com/aws/smithy-go"
)

// The states the Launcher page understands (the-lobby docs/minecraft-backend.md).
const (
	Stopped     = "stopped"
	Starting    = "starting"
	Ready       = "ready"
	Stopping    = "stopping"
	Maintenance = "maintenance"
	Failed      = "failed"
	Unknown     = "unknown"
)

const (
	javaPort = 25565
	// remoteCommand is the same host interface minecraft_host.py and the idle stop use.
	remoteCommand = "/usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json"
	// startDeadline bounds a start this function began. A start still unfinished after it is reported failed,
	// so a Member can try again even if the follow-up run was lost.
	startDeadline = 15 * time.Minute
	// bootGrace covers a host that some other caller, such as the workstation start, launched recently:
	// it is starting until then, and a running host without a game after it is in maintenance.
	bootGrace = 10 * time.Minute
	// pingAttempt bounds one status ping of the game.
	pingAttempt = 2 * time.Second
	// finishMargin ends a follow-up this long before its start's deadline, so it has written its last state
	// before the start is reported failed and a Member can start again.
	finishMargin = 30 * time.Second
	// commandDelivery is how long SSM may hold the game start before delivering it, in seconds (its minimum).
	commandDelivery = 30
)

// pollEvery is how often the follow-up run checks EC2, SSM and the remote start. Tests shorten it.
var pollEvery = 3 * time.Second

var commandIdentity = regexp.MustCompile(`^[0-9a-f-]{36}$`)

// EC2API is the subset of EC2 the function calls.
type EC2API interface {
	DescribeInstances(context.Context, *ec2.DescribeInstancesInput, ...func(*ec2.Options)) (*ec2.DescribeInstancesOutput, error)
	StartInstances(context.Context, *ec2.StartInstancesInput, ...func(*ec2.Options)) (*ec2.StartInstancesOutput, error)
}

// SSMAPI is the subset of Systems Manager the function calls.
type SSMAPI interface {
	ParameterAPI
	PutParameter(context.Context, *ssm.PutParameterInput, ...func(*ssm.Options)) (*ssm.PutParameterOutput, error)
	SendCommand(context.Context, *ssm.SendCommandInput, ...func(*ssm.Options)) (*ssm.SendCommandOutput, error)
	GetCommandInvocation(context.Context, *ssm.GetCommandInvocationInput, ...func(*ssm.Options)) (*ssm.GetCommandInvocationOutput, error)
	DescribeInstanceInformation(context.Context, *ssm.DescribeInstanceInformationInput, ...func(*ssm.Options)) (*ssm.DescribeInstanceInformationOutput, error)
}

// Config is supplied by the function's environment.
type Config struct {
	HostID                 string
	Instance               string
	ServerName             string
	PublicHostname         string
	BedrockPort            int
	StateParameter         string
	IdleStopStateParameter string
}

// Session is this function's record of the last start it began, kept in an SSM parameter. LaunchTime ties it
// to one EC2 run once the host is running, so a later run never inherits an old start's outcome.
type Session struct {
	RequestedAt time.Time `json:"requested_at,omitzero"`
	LaunchTime  time.Time `json:"launch_time,omitzero"`
	Phase       string    `json:"phase,omitempty"` // starting, ready or failed
	DNS         string    `json:"dns,omitempty"`   // current or failed
	Detail      string    `json:"detail,omitempty"`
}

// Status is the answer to GET /server and POST /server/start.
type Status struct {
	Name       string     `json:"name"`
	State      string     `json:"state"`
	LastKnown  string     `json:"last_known,omitempty"`
	Connection Connection `json:"connection"`
}

type Connection struct {
	Java    *Endpoint `json:"java,omitempty"`
	Bedrock *Endpoint `json:"bedrock,omitempty"`
}

type Endpoint struct {
	Address string `json:"address"`
	Port    int    `json:"port,omitempty"`
}

// Launcher answers the site and starts the host the way the workstation start does.
type Launcher struct {
	Config Config
	EC2    EC2API
	SSM    SSMAPI
	// Ping reports whether a Minecraft server answers a status request at address.
	Ping func(ctx context.Context, address string) (int, error)
	// PointDNS points the players' hostname at the running host's address.
	PointDNS func(ctx context.Context, address string) error
	// FollowUp runs FinishStart outside the request, so the page gets its answer at once.
	FollowUp func(ctx context.Context) error
	Now      func() time.Time
}

type host struct {
	state      ec2types.InstanceStateName
	launchTime time.Time
	publicIP   string
}

// ErrNoServer means the function has no host configured.
var ErrNoServer = errors.New("no server is configured")

// Status reports the server as players see it. AWS errors become unknown, never a guess.
func (l *Launcher) Status(ctx context.Context) (Status, error) {
	if l.Config.HostID == "" {
		return Status{}, ErrNoServer
	}
	h, err := l.describeHost(ctx)
	if err != nil {
		return l.unknown(ctx), err
	}
	session, err := l.loadSession(ctx)
	if err != nil {
		return l.answer(Unknown, Session{}), err
	}
	state, err := l.decide(ctx, h, session)
	if err != nil {
		// Keep the current run's record, so a DNS failure stays hidden while AWS can't be read.
		return l.answer(Unknown, l.current(h, session)), err
	}
	return l.answer(state, l.current(h, session)), nil
}

// Start starts a stopped or failed server and answers its status. Repeating it while a start is under way,
// or while the server runs, changes nothing.
func (l *Launcher) Start(ctx context.Context) (Status, bool, error) {
	if l.Config.HostID == "" {
		return Status{}, false, ErrNoServer
	}
	h, err := l.describeHost(ctx)
	if err != nil {
		return l.unknown(ctx), false, err
	}
	session, err := l.loadSession(ctx)
	if err != nil {
		return l.answer(Unknown, Session{}), false, err
	}
	now := l.Now().UTC()
	switch h.state {
	case ec2types.InstanceStateNameStopped:
		// Record the start before EC2 sees it, so every status read from here on knows a start is under way.
		session = Session{RequestedAt: now, Phase: Starting}
		if err := l.saveSession(ctx, session); err != nil {
			return l.answer(Unknown, Session{}), false, err
		}
		if _, err := l.EC2.StartInstances(ctx, &ec2.StartInstancesInput{InstanceIds: []string{l.Config.HostID}}); err != nil {
			// Only EC2's own answer refusing this start is final. IncorrectInstanceState means another Member's
			// start moved the host on first, and an error without EC2's answer may hide an accepted start. In both
			// the start stands and gets a follow-up, which repeats harmlessly or fails at the start's deadline.
			var refused smithy.APIError
			if errors.As(err, &refused) && refused.ErrorCode() != "IncorrectInstanceState" && !l.hostMoved(ctx) {
				// A refusal, such as throttling, can meet another Member's accepted start: record the failure
				// only while the record is still this request's and the host has not moved.
				_ = l.fail(ctx, session, errors.New("EC2 refused the start"))
				return l.answer(Unknown, Session{}), false, fmt.Errorf("start host: %w", err)
			}
		}
		return l.answer(Starting, session), true, l.followUp(ctx)
	case ec2types.InstanceStateNameRunning, ec2types.InstanceStateNamePending:
		state, err := l.decide(ctx, h, session)
		if err != nil {
			return l.answer(Unknown, l.current(h, session)), false, err
		}
		if state != Failed {
			return l.answer(state, l.current(h, session)), false, nil
		}
		// The host runs, or is still pending, but this function's start failed: try again on the same run.
		session = Session{RequestedAt: now, LaunchTime: h.launchTime, Phase: Starting}
		if err := l.saveSession(ctx, session); err != nil {
			return l.answer(Unknown, Session{}), false, err
		}
		return l.answer(Starting, session), true, l.followUp(ctx)
	default:
		// Stopping and anything else: someone else's transition is under way.
		state, err := l.decide(ctx, h, session)
		if err != nil {
			return l.answer(Unknown, l.current(h, session)), false, err
		}
		return l.answer(state, l.current(h, session)), false, nil
	}
}

// hostMoved reports whether a fresh description shows the host pending or running.
func (l *Launcher) hostMoved(ctx context.Context) bool {
	h, err := l.describeHost(ctx)
	return err == nil && (h.state == ec2types.InstanceStateNamePending || h.state == ec2types.InstanceStateNameRunning)
}

// unknown answers when the host cannot be read. A saved DNS failure still hides the address: it may be this run's.
func (l *Launcher) unknown(ctx context.Context) Status {
	session, err := l.loadSession(ctx)
	if err != nil {
		session = Session{}
	}
	return l.answer(Unknown, session)
}

func (l *Launcher) followUp(ctx context.Context) error {
	if err := l.FollowUp(ctx); err != nil {
		// The start stands; without its follow-up it is reported failed after startDeadline, and can be retried.
		return fmt.Errorf("hand off the rest of the start: %w", err)
	}
	return nil
}

// decide applies the state rules. Only a game answering a status ping is ready.
func (l *Launcher) decide(ctx context.Context, h host, session Session) (string, error) {
	switch h.state {
	case ec2types.InstanceStateNamePending:
		// A start of ours that failed or ran out of time stays failed even while EC2 is stuck pending.
		if session, ok := l.sessionFor(h, session); ok && (session.Phase == Failed ||
			session.Phase == Starting && l.Now().UTC().Sub(session.RequestedAt) >= startDeadline) {
			return Failed, nil
		}
		return Starting, nil
	case ec2types.InstanceStateNameStopping, ec2types.InstanceStateNameShuttingDown:
		return Stopping, nil
	case ec2types.InstanceStateNameStopped:
		return Stopped, nil
	case ec2types.InstanceStateNameRunning:
	default:
		return Unknown, nil
	}
	// The idle stop's drain comes first: the game still answers while it saves and backs up the server.
	draining, err := l.idleStopDraining(ctx, h)
	if err != nil {
		return "", err
	}
	if draining {
		return Stopping, nil
	}
	if l.gameAnswers(ctx, h.publicIP) {
		return Ready, nil
	}
	now := l.Now().UTC()
	if session, ok := l.sessionFor(h, session); ok {
		switch session.Phase {
		case Failed:
			return Failed, nil
		case Starting:
			if now.Sub(session.RequestedAt) < startDeadline {
				return Starting, nil
			}
			return Failed, nil
		}
		// A start that reached ready and whose game no longer answers was stopped on purpose or by a stop
		// in progress; either way it is not a Member's to start again.
		return Maintenance, nil
	}
	if now.Sub(h.launchTime) < bootGrace {
		return Starting, nil
	}
	return Maintenance, nil
}

func (l *Launcher) gameAnswers(ctx context.Context, address string) bool {
	if net.ParseIP(address) == nil {
		return false
	}
	target := net.JoinHostPort(address, strconv.Itoa(javaPort))
	// One retry, so a single dropped packet does not turn a running game into maintenance.
	// Each attempt is short: the site gives the whole status request only a few seconds.
	for attempt := 0; attempt < 2; attempt++ {
		attemptCtx, cancel := context.WithTimeout(ctx, pingAttempt)
		_, err := l.Ping(attemptCtx, target)
		cancel()
		if err == nil {
			return true
		}
	}
	return false
}

// sessionFor returns this function's start record when it belongs to the host's current run.
func (l *Launcher) sessionFor(h host, session Session) (Session, bool) {
	if session.Phase == "" || session.RequestedAt.IsZero() {
		return Session{}, false
	}
	if !session.LaunchTime.IsZero() {
		return session, session.LaunchTime.Equal(h.launchTime)
	}
	// Not yet tied to a run: it belongs to a run launched soon after it was requested (with a little clock
	// slack), never to one somebody else launched much later.
	return session, !h.launchTime.Before(session.RequestedAt.Add(-time.Minute)) && h.launchTime.Before(session.RequestedAt.Add(startDeadline))
}

func (l *Launcher) current(h host, session Session) Session {
	if current, ok := l.sessionFor(h, session); ok && h.state == ec2types.InstanceStateNameRunning {
		return current
	}
	return Session{}
}

func (l *Launcher) answer(state string, session Session) Status {
	status := Status{Name: l.Config.ServerName, State: state}
	// The hostname is the verified address only while it follows the host. A start of ours that could not
	// update it leaves players without a working address, so the page shows none rather than a wrong one.
	if session.DNS != "failed" {
		status.Connection = Connection{
			Java:    &Endpoint{Address: l.Config.PublicHostname},
			Bedrock: &Endpoint{Address: l.Config.PublicHostname, Port: l.Config.BedrockPort},
		}
	}
	return status
}

// idleStopDraining reads the idle stop's own progress, when it is deployed, to see a drain under way.
func (l *Launcher) idleStopDraining(ctx context.Context, h host) (bool, error) {
	if l.Config.IdleStopStateParameter == "" {
		return false, nil
	}
	output, err := l.SSM.GetParameter(ctx, &ssm.GetParameterInput{Name: aws.String(l.Config.IdleStopStateParameter)})
	var missing *ssmtypes.ParameterNotFound
	if errors.As(err, &missing) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("read idle-stop state: %w", err)
	}
	var state struct {
		LaunchTime     time.Time `json:"launch_time"`
		DrainCommandID string    `json:"drain_command_id"`
		StopRequested  bool      `json:"stop_requested"`
	}
	if output.Parameter == nil || json.Unmarshal([]byte(aws.ToString(output.Parameter.Value)), &state) != nil {
		return false, errors.New("read idle-stop state: parameter does not hold a state object")
	}
	return state.LaunchTime.Equal(h.launchTime) && (state.DrainCommandID != "" || state.StopRequested), nil
}

// FinishStart does the rest of a start once EC2 has accepted it: the same steps as `minecraft_host.py start`.
// It waits for the host, points DNS at it, waits for SSM, and asks the host's adapter to start the game.
func (l *Launcher) FinishStart(ctx context.Context) error {
	requested, err := l.loadSession(ctx)
	if err != nil {
		return err
	}
	if requested.Phase == Starting {
		// A follow-up that waited in Lambda's queue still ends inside its start's deadline, so it never
		// overwrites a retry a Member began once the start was reported failed.
		remaining := requested.RequestedAt.Add(startDeadline - finishMargin).Sub(l.Now())
		if remaining <= 0 {
			return errors.New("the start passed its deadline before its follow-up ran")
		}
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, remaining)
		defer cancel()
	}
	h, err := l.waitRunning(ctx)
	if err != nil {
		return l.fail(ctx, Session{}, err)
	}
	session, err := l.loadSession(ctx)
	if err != nil {
		return err
	}
	if _, ok := l.sessionFor(h, session); !ok || session.Phase != Starting {
		if ok && session.Phase == Ready {
			return nil // a repeated tap's follow-up found the start already finished
		}
		return errors.New("no start of this function is under way for the running host")
	}
	session.LaunchTime = h.launchTime
	// DNS goes first, so the record propagates while the game boots. Its failure never stops the game.
	session.DNS = "current"
	if err := l.PointDNS(ctx, h.publicIP); err != nil {
		session.DNS = "failed"
	}
	if err := l.saveSession(ctx, session); err != nil {
		return err
	}
	if err := l.waitSSM(ctx); err != nil {
		return l.fail(ctx, session, err)
	}
	if err := l.startGame(ctx); err != nil {
		return l.fail(ctx, session, err)
	}
	session.Phase = Ready
	session.Detail = ""
	if err := l.saveSession(ctx, session); err != nil {
		return err
	}
	if session.DNS == "failed" {
		return errors.New("the game started but DNS was not updated")
	}
	return nil
}

func (l *Launcher) fail(ctx context.Context, session Session, cause error) error {
	// The deadline that ended the start must not also lose its record.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
	defer cancel()
	saved, err := l.loadSession(ctx)
	if err != nil {
		return errors.Join(cause, err)
	}
	if saved.Phase == Ready || !session.RequestedAt.IsZero() && !saved.RequestedAt.Equal(session.RequestedAt) {
		return cause // a repeated follow-up already finished this start, or a later start owns the record now
	}
	if session.RequestedAt.IsZero() {
		session = saved
	}
	session.Phase = Failed
	session.Detail = cause.Error()
	return errors.Join(cause, l.saveSession(ctx, session))
}

func (l *Launcher) waitRunning(ctx context.Context) (host, error) {
	for {
		h, err := l.describeHost(ctx)
		if err != nil {
			return host{}, err
		}
		switch h.state {
		case ec2types.InstanceStateNameRunning:
			if net.ParseIP(h.publicIP) != nil {
				return h, nil
			}
		case ec2types.InstanceStateNamePending, ec2types.InstanceStateNameStopped:
			// Stopped is EC2's eventual consistency right after StartInstances.
		default:
			return host{}, fmt.Errorf("host cannot reach running from %s", h.state)
		}
		if err := l.pause(ctx, "the host did not reach running"); err != nil {
			return host{}, err
		}
	}
}

func (l *Launcher) waitSSM(ctx context.Context) error {
	for {
		output, err := l.SSM.DescribeInstanceInformation(ctx, &ssm.DescribeInstanceInformationInput{
			Filters: []ssmtypes.InstanceInformationStringFilter{{Key: aws.String("InstanceIds"), Values: []string{l.Config.HostID}}},
		})
		if err != nil {
			return fmt.Errorf("describe SSM node: %w", err)
		}
		for _, node := range output.InstanceInformationList {
			if aws.ToString(node.InstanceId) == l.Config.HostID && node.PingStatus == ssmtypes.PingStatusOnline {
				return nil
			}
		}
		if err := l.pause(ctx, "the host did not come online in SSM"); err != nil {
			return err
		}
	}
}

func (l *Launcher) startGame(ctx context.Context) error {
	// Delivery and execution together end before this run's deadline, so the remote start never outlives the
	// start it belongs to and overlaps a retry.
	timeout := 600
	if deadline, ok := ctx.Deadline(); ok {
		timeout = int(time.Until(deadline).Seconds()) - commandDelivery - 15
		if timeout < 30 {
			return errors.New("too little time is left to start the game")
		}
	}
	output, err := l.SSM.SendCommand(ctx, &ssm.SendCommandInput{
		InstanceIds:    []string{l.Config.HostID},
		DocumentName:   aws.String("AWS-RunShellScript"),
		Comment:        aws.String("minecraft launcher start"),
		TimeoutSeconds: aws.Int32(commandDelivery),
		Parameters: map[string][]string{
			"commands":         {remoteCommand + " start " + l.Config.Instance},
			"executionTimeout": {strconv.Itoa(timeout)},
		},
	})
	if err != nil {
		return fmt.Errorf("send game start: %w", err)
	}
	if output.Command == nil || !commandIdentity.MatchString(aws.ToString(output.Command.CommandId)) {
		return errors.New("send game start: SSM did not return a command identity")
	}
	id := aws.ToString(output.Command.CommandId)
	for {
		invocation, err := l.SSM.GetCommandInvocation(ctx, &ssm.GetCommandInvocationInput{CommandId: aws.String(id), InstanceId: aws.String(l.Config.HostID)})
		var missing *ssmtypes.InvocationDoesNotExist
		switch {
		case errors.As(err, &missing):
		case err != nil:
			return fmt.Errorf("follow game start: %w", err)
		case invocation.Status == ssmtypes.CommandInvocationStatusSuccess:
			return checkStarted(aws.ToString(invocation.StandardOutputContent), invocation.ResponseCode, l.Config.Instance)
		case invocation.Status == ssmtypes.CommandInvocationStatusPending, invocation.Status == ssmtypes.CommandInvocationStatusInProgress, invocation.Status == ssmtypes.CommandInvocationStatusDelayed:
		default:
			return fmt.Errorf("game start ended with status %s", invocation.Status)
		}
		if err := l.pause(ctx, "the game start did not finish"); err != nil {
			return err
		}
	}
}

// checkStarted applies the workstation start's rule: the adapter reports the instance running and ready.
func checkStarted(output string, code int32, instance string) error {
	if code != 0 {
		return errors.New("the host's adapter refused the start")
	}
	var result struct {
		Instance    string `json:"instance"`
		Application string `json:"application"`
		Ready       *bool  `json:"ready"`
	}
	if json.Unmarshal([]byte(output), &result) != nil || result.Instance != instance {
		return errors.New("the host's adapter returned invalid completion evidence")
	}
	if result.Application != "running" || result.Ready == nil || !*result.Ready {
		return errors.New("the game did not report ready after start")
	}
	return nil
}

func (l *Launcher) pause(ctx context.Context, timeout string) error {
	timer := time.NewTimer(pollEvery)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return errors.New(timeout + " in time")
	case <-timer.C:
		return nil
	}
}

func (l *Launcher) describeHost(ctx context.Context) (host, error) {
	output, err := l.EC2.DescribeInstances(ctx, &ec2.DescribeInstancesInput{InstanceIds: []string{l.Config.HostID}})
	if err != nil {
		return host{}, fmt.Errorf("describe host: %w", err)
	}
	var found []ec2types.Instance
	for _, reservation := range output.Reservations {
		found = append(found, reservation.Instances...)
	}
	if len(found) != 1 || aws.ToString(found[0].InstanceId) != l.Config.HostID || found[0].State == nil {
		return host{}, errors.New("describe host: AWS returned an unexpected host")
	}
	h := host{state: found[0].State.Name, publicIP: aws.ToString(found[0].PublicIpAddress)}
	if found[0].LaunchTime != nil {
		h.launchTime = found[0].LaunchTime.UTC()
	}
	if h.state == ec2types.InstanceStateNameRunning && h.launchTime.IsZero() {
		return host{}, errors.New("describe host: running host has no launch time")
	}
	return h, nil
}

func (l *Launcher) loadSession(ctx context.Context) (Session, error) {
	output, err := l.SSM.GetParameter(ctx, &ssm.GetParameterInput{Name: aws.String(l.Config.StateParameter)})
	if err != nil {
		return Session{}, fmt.Errorf("read launcher state: %w", err)
	}
	var session Session
	if output.Parameter == nil || json.Unmarshal([]byte(aws.ToString(output.Parameter.Value)), &session) != nil {
		return Session{}, errors.New("read launcher state: parameter does not hold a state object")
	}
	return session, nil
}

func (l *Launcher) saveSession(ctx context.Context, session Session) error {
	encoded, err := json.Marshal(session)
	if err != nil {
		return fmt.Errorf("encode launcher state: %w", err)
	}
	_, err = l.SSM.PutParameter(ctx, &ssm.PutParameterInput{
		Name:      aws.String(l.Config.StateParameter),
		Value:     aws.String(string(encoded)),
		Overwrite: aws.Bool(true),
	})
	if err != nil {
		return fmt.Errorf("write launcher state: %w", err)
	}
	return nil
}
