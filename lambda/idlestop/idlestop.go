package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

// DrainCommand is the same fixed host-side interface scripts/minecraft_host.py uses for host stop.
const DrainCommand = "/usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json host-drain"
const HostStatusCommand = "/usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json host-status"

const (
	remoteTimeoutSeconds = 3600
	// drainDeadline bounds how long a submitted drain may stay pending before the host is left running and
	// the function reports a blocked stop: the remote execution limit plus SSM's delivery window and margin.
	drainDeadline = (remoteTimeoutSeconds + 60 + 240) * time.Second
	// invisibleGrace covers Run Command's eventual consistency right after SendCommand.
	invisibleGrace = 2 * time.Minute
	// launcherStopDeadline is how long a stop the launcher recorded is believed; the launcher gives its own
	// stop the same (lambda/launcher stopDeadline), after which the host is checked as if nobody were
	// stopping it.
	launcherStopDeadline = 75 * time.Minute
)

var (
	instanceID      = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)
	commandIdentity = regexp.MustCompile(`^[0-9a-f-]{36}$`)
	parameterName   = regexp.MustCompile(`^/[A-Za-z0-9/_.-]{1,500}$`)
)

// EC2API is the subset of EC2 the function calls.
type EC2API interface {
	DescribeInstances(context.Context, *ec2.DescribeInstancesInput, ...func(*ec2.Options)) (*ec2.DescribeInstancesOutput, error)
	StopInstances(context.Context, *ec2.StopInstancesInput, ...func(*ec2.Options)) (*ec2.StopInstancesOutput, error)
}

// SSMAPI is the subset of Systems Manager the function calls.
type SSMAPI interface {
	SendCommand(context.Context, *ssm.SendCommandInput, ...func(*ssm.Options)) (*ssm.SendCommandOutput, error)
	GetCommandInvocation(context.Context, *ssm.GetCommandInvocationInput, ...func(*ssm.Options)) (*ssm.GetCommandInvocationOutput, error)
	GetParameter(context.Context, *ssm.GetParameterInput, ...func(*ssm.Options)) (*ssm.GetParameterOutput, error)
	PutParameter(context.Context, *ssm.PutParameterInput, ...func(*ssm.Options)) (*ssm.PutParameterOutput, error)
}

// Pinger returns the number of players online at a Java edition server address.
type Pinger func(ctx context.Context, address string) (int, error)

// Config is supplied by the function's environment.
type Config struct {
	HostID                   string
	StateParameter           string
	IdleAfter                time.Duration
	Instances                []string
	PublicHostname           string
	CloudflareZoneID         string
	CloudflareTokenSecretARN string
	// LauncherStateParameter names the launcher's state, when the launcher is deployed, so a stop it is
	// running is left to it; empty, the idle stop never looks.
	LauncherStateParameter string
}

// State persists between invocations in an SSM parameter. LaunchTime ties it to one EC2 run, so a stop and
// start between invocations can never carry an old idle timer or drain into a new session.
type State struct {
	LaunchTime     time.Time  `json:"launch_time,omitzero"`
	IdleSince      *time.Time `json:"idle_since,omitempty"`
	DrainCommandID string     `json:"drain_command_id,omitempty"`
	DrainSentAt    *time.Time `json:"drain_sent_at,omitempty"`
	Blocked        string     `json:"blocked,omitempty"`
	StopRequested  bool       `json:"stop_requested,omitempty"`
	DNSPending     bool       `json:"dns_pending,omitempty"`
}

// Result is logged after every invocation. It holds no addresses, identifiers or player data.
type Result struct {
	Action  string `json:"action"`
	Players *int   `json:"players,omitempty"`
	Detail  string `json:"detail,omitempty"`
}

// ErrBlocked marks an invocation that refuses to stop a host until an operator intervenes.
var ErrBlocked = errors.New("idle stop blocked")

// Controller decides, one scheduled invocation at a time, whether an idle host should be drained and stopped.
type Controller struct {
	Config  Config
	EC2     EC2API
	SSM     SSMAPI
	Ping    Pinger
	Now     func() time.Time
	ParkDNS func(context.Context) error
}

type host struct {
	state      ec2types.InstanceStateName
	launchTime time.Time
	publicIP   string
}

// Run performs one idle check. A returned error fails the invocation, which the deployment alarms on.
func (c *Controller) Run(ctx context.Context) (Result, error) {
	h, err := c.describeHost(ctx)
	if err != nil {
		return Result{Action: "error"}, err
	}
	saved, err := c.loadState(ctx)
	if err != nil {
		return Result{Action: "error"}, err
	}
	if h.state != ec2types.InstanceStateNameRunning {
		if saved.DNSPending && (h.state == ec2types.InstanceStateNameStopping || h.state == ec2types.InstanceStateNameStopped) {
			if err := c.parkDNS(ctx, &saved); err != nil {
				return Result{Action: "dns-pending"}, err
			}
		}
		if saved != (State{}) {
			if err := c.saveState(ctx, State{}); err != nil {
				return Result{Action: "error"}, err
			}
		}
		return Result{Action: "host-not-running", Detail: string(h.state)}, nil
	}
	state := saved
	if !state.LaunchTime.Equal(h.launchTime) {
		state = State{LaunchTime: h.launchTime}
	}
	result, err := c.step(ctx, &state, h.publicIP)
	if !statesEqual(state, saved) {
		if saveErr := c.saveState(ctx, state); saveErr != nil {
			return Result{Action: "error"}, errors.Join(err, saveErr)
		}
	}
	return result, err
}

func (c *Controller) step(ctx context.Context, state *State, publicIP string) (Result, error) {
	if state.StopRequested {
		if state.DNSPending {
			if err := c.parkDNS(ctx, state); err != nil {
				return Result{Action: "dns-pending"}, err
			}
		}
		return Result{Action: "stop-pending"}, nil
	}
	if state.Blocked != "" {
		return Result{Action: "blocked", Detail: state.Blocked}, fmt.Errorf("%w: %s", ErrBlocked, state.Blocked)
	}
	if state.DrainCommandID != "" {
		return c.followDrain(ctx, state)
	}
	now := c.Now().UTC()
	stopping, err := c.launcherStopping(ctx, state.LaunchTime, now)
	if err != nil {
		state.IdleSince = nil
		return Result{Action: "error"}, err
	}
	if stopping {
		state.IdleSince = nil
		return Result{Action: "launcher-stopping"}, nil
	}
	if net.ParseIP(publicIP) == nil {
		state.IdleSince = nil
		return Result{Action: "error"}, errors.New("running host has no valid public address")
	}
	players, pingErr := c.Ping(ctx, net.JoinHostPort(publicIP, "25565"))
	if pingErr != nil {
		state.IdleSince = nil
		return Result{Action: "error"}, errors.New("server status ping failed; idle countdown cleared")
	}
	if players > 0 {
		state.IdleSince = nil
		return Result{Action: "active", Players: &players}, nil
	}
	detail := "no players online"
	if state.IdleSince == nil {
		state.IdleSince = &now
		return Result{Action: "idle-started", Detail: detail}, nil
	}
	if now.Sub(*state.IdleSince) < c.Config.IdleAfter {
		return Result{Action: "idle", Detail: detail}, nil
	}
	id, err := c.sendDrain(ctx)
	if err != nil {
		return Result{Action: "error"}, err
	}
	state.DrainCommandID = id
	state.DrainSentAt = &now
	return Result{Action: "drain-sent", Detail: detail}, nil
}

func (c *Controller) followDrain(ctx context.Context, state *State) (Result, error) {
	now := c.Now().UTC()
	sentAt := now
	if state.DrainSentAt != nil {
		sentAt = *state.DrainSentAt
	}
	invocation, err := c.SSM.GetCommandInvocation(ctx, &ssm.GetCommandInvocationInput{
		CommandId:  aws.String(state.DrainCommandID),
		InstanceId: aws.String(c.Config.HostID),
	})
	var missing *ssmtypes.InvocationDoesNotExist
	if errors.As(err, &missing) {
		if now.Sub(sentAt) < invisibleGrace {
			return Result{Action: "drain-pending", Detail: "invocation not visible yet"}, nil
		}
		return c.block(state, "drain invocation never became visible")
	}
	if err != nil {
		return Result{Action: "error"}, fmt.Errorf("get drain invocation: %w", err)
	}
	switch invocation.Status {
	case ssmtypes.CommandInvocationStatusPending, ssmtypes.CommandInvocationStatusInProgress, ssmtypes.CommandInvocationStatusDelayed:
		if now.Sub(sentAt) > drainDeadline {
			return c.block(state, "drain did not finish before its deadline")
		}
		return Result{Action: "drain-pending"}, nil
	case ssmtypes.CommandInvocationStatusSuccess:
	default:
		return c.block(state, fmt.Sprintf("drain ended with status %s", invocation.Status))
	}
	if invocation.ResponseCode != 0 {
		return c.block(state, "drain returned failure")
	}
	if err := ValidateDrain(aws.ToString(invocation.StandardOutputContent), c.Config.Instances); err != nil {
		return c.block(state, err.Error())
	}
	if err := c.confirmDrain(ctx); err != nil {
		if errors.Is(err, ErrBlocked) {
			return c.block(state, "fresh host status lacks complete drain evidence")
		}
		return Result{Action: "error"}, err
	}
	current, err := c.describeHost(ctx)
	if err != nil {
		return Result{Action: "error"}, err
	}
	if current.state != ec2types.InstanceStateNameRunning || !current.launchTime.Equal(state.LaunchTime) {
		*state = State{LaunchTime: current.launchTime}
		return Result{Action: "host-changed"}, nil
	}
	// Persist the parking obligation before stopping EC2. Even a timeout or failed later state write
	// must leave enough durable progress for the next invocation to park an already stopped host.
	if !state.DNSPending {
		state.DNSPending = true
		if err := c.saveState(ctx, *state); err != nil {
			state.DNSPending = false
			return Result{Action: "error"}, err
		}
	}
	if _, err := c.EC2.StopInstances(ctx, &ec2.StopInstancesInput{InstanceIds: []string{c.Config.HostID}}); err != nil {
		// The drain barrier is boot-bound and stays in place, so a retry on the next invocation is safe.
		return Result{Action: "error"}, fmt.Errorf("stop host after verified drain: %w", err)
	}
	*state = State{LaunchTime: state.LaunchTime, StopRequested: true, DNSPending: true}
	if err := c.parkDNS(ctx, state); err != nil {
		return Result{Action: "dns-pending"}, err
	}
	return Result{Action: "stopped"}, nil
}

// A reboot keeps EC2 LaunchTime but clears the boot-bound drain barrier. Read current host status
// immediately before every stop attempt; never reuse its completion on a later invocation.
func (c *Controller) confirmDrain(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	id, err := c.sendCommand(ctx, HostStatusCommand, 20)
	if err != nil {
		return err
	}
	for {
		invocation, err := c.SSM.GetCommandInvocation(ctx, &ssm.GetCommandInvocationInput{
			CommandId: aws.String(id), InstanceId: aws.String(c.Config.HostID),
		})
		var missing *ssmtypes.InvocationDoesNotExist
		if err != nil && !errors.As(err, &missing) {
			return errors.New("fresh host status could not be read")
		}
		if err == nil {
			switch invocation.Status {
			case ssmtypes.CommandInvocationStatusSuccess:
				if invocation.ResponseCode != 0 || ValidateDrain(aws.ToString(invocation.StandardOutputContent), c.Config.Instances) != nil {
					return ErrBlocked
				}
				return nil
			case ssmtypes.CommandInvocationStatusPending, ssmtypes.CommandInvocationStatusInProgress, ssmtypes.CommandInvocationStatusDelayed:
			default:
				return ErrBlocked
			}
		}
		timer := time.NewTimer(time.Second)
		select {
		case <-ctx.Done():
			timer.Stop()
			return errors.New("fresh host status did not complete within the stop check")
		case <-timer.C:
		}
	}
}

func (c *Controller) parkDNS(ctx context.Context, state *State) error {
	current, err := c.describeHost(ctx)
	if err != nil {
		return err
	}
	// A new launch supersedes the previous stop even when DNS parking failed.
	if !current.launchTime.Equal(state.LaunchTime) {
		*state = State{LaunchTime: current.launchTime}
		return nil
	}
	if err := c.ParkDNS(ctx); err != nil {
		return errors.New("DNS parking failed; retry pending")
	}
	state.DNSPending = false
	return nil
}

func (c *Controller) block(state *State, reason string) (Result, error) {
	state.Blocked = reason
	state.DrainCommandID = ""
	state.DrainSentAt = nil
	return Result{Action: "blocked", Detail: reason}, fmt.Errorf("%w: %s", ErrBlocked, reason)
}

func (c *Controller) sendDrain(ctx context.Context) (string, error) {
	return c.sendCommand(ctx, DrainCommand, remoteTimeoutSeconds)
}

func (c *Controller) sendCommand(ctx context.Context, command string, timeout int) (string, error) {
	output, err := c.SSM.SendCommand(ctx, &ssm.SendCommandInput{
		InstanceIds:    []string{c.Config.HostID},
		DocumentName:   aws.String("AWS-RunShellScript"),
		Comment:        aws.String("minecraft idle stop"),
		TimeoutSeconds: aws.Int32(60),
		Parameters: map[string][]string{
			"commands":         {command},
			"executionTimeout": {fmt.Sprint(timeout)},
		},
	})
	if err != nil {
		return "", fmt.Errorf("send drain: %w", err)
	}
	if output.Command == nil || !commandIdentity.MatchString(aws.ToString(output.Command.CommandId)) {
		return "", errors.New("send drain: SSM did not return a command identity")
	}
	return aws.ToString(output.Command.CommandId), nil
}

func (c *Controller) describeHost(ctx context.Context) (host, error) {
	output, err := c.EC2.DescribeInstances(ctx, &ec2.DescribeInstancesInput{InstanceIds: []string{c.Config.HostID}})
	if err != nil {
		return host{}, fmt.Errorf("describe host: %w", err)
	}
	var found []ec2types.Instance
	for _, reservation := range output.Reservations {
		found = append(found, reservation.Instances...)
	}
	if len(found) != 1 || aws.ToString(found[0].InstanceId) != c.Config.HostID || found[0].State == nil {
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

// launcherStopping reads the launcher's own progress, when it is deployed, to see a stop it is running on
// this launch of the host: the idle stop then stands aside, so the two never drain the host at once. A
// record older than the launcher's deadline, or from an earlier launch, is not a stop under way.
func (c *Controller) launcherStopping(ctx context.Context, launchTime, now time.Time) (bool, error) {
	if c.Config.LauncherStateParameter == "" {
		return false, nil
	}
	output, err := c.SSM.GetParameter(ctx, &ssm.GetParameterInput{Name: aws.String(c.Config.LauncherStateParameter)})
	var missing *ssmtypes.ParameterNotFound
	if errors.As(err, &missing) {
		return false, nil
	}
	if err != nil {
		return false, fmt.Errorf("read launcher state: %w", err)
	}
	var session struct {
		RequestedAt time.Time `json:"requested_at"`
		LaunchTime  time.Time `json:"launch_time"`
		Phase       string    `json:"phase"`
	}
	if output.Parameter == nil || json.Unmarshal([]byte(aws.ToString(output.Parameter.Value)), &session) != nil {
		return false, errors.New("read launcher state: parameter does not hold a state object")
	}
	return session.Phase == "stopping" && session.LaunchTime.Equal(launchTime) && now.Sub(session.RequestedAt) < launcherStopDeadline, nil
}

func (c *Controller) loadState(ctx context.Context) (State, error) {
	output, err := c.SSM.GetParameter(ctx, &ssm.GetParameterInput{Name: aws.String(c.Config.StateParameter)})
	if err != nil {
		return State{}, fmt.Errorf("read state: %w", err)
	}
	var state State
	if output.Parameter == nil || json.Unmarshal([]byte(aws.ToString(output.Parameter.Value)), &state) != nil {
		return State{}, errors.New("read state: parameter does not hold a state object")
	}
	return state, nil
}

func (c *Controller) saveState(ctx context.Context, state State) error {
	encoded, err := json.Marshal(state)
	if err != nil {
		return fmt.Errorf("encode state: %w", err)
	}
	_, err = c.SSM.PutParameter(ctx, &ssm.PutParameterInput{
		Name:      aws.String(c.Config.StateParameter),
		Value:     aws.String(string(encoded)),
		Overwrite: aws.Bool(true),
	})
	if err != nil {
		return fmt.Errorf("write state: %w", err)
	}
	return nil
}

func statesEqual(a, b State) bool {
	left, _ := json.Marshal(a)
	right, _ := json.Marshal(b)
	return string(left) == string(right)
}

// ValidateDrain applies the evidence rules of minecraft_host.py's stop: the response covers exactly the
// configured instances, confirms the drain barrier, and reports every application stopped with its backup
// complete (or not required for a never-started instance). Anything else forbids the EC2 stop.
func ValidateDrain(output string, configured []string) error {
	var response struct {
		Draining  *bool                      `json:"draining"`
		Instances map[string]json.RawMessage `json:"instances"`
	}
	if err := json.Unmarshal([]byte(output), &response); err != nil {
		return errors.New("drain returned invalid completion evidence")
	}
	got := make([]string, 0, len(response.Instances))
	for name := range response.Instances {
		got = append(got, name)
	}
	want := append([]string(nil), configured...)
	sort.Strings(got)
	sort.Strings(want)
	if len(want) == 0 || strings.Join(got, ",") != strings.Join(want, ",") {
		return errors.New("drain instance inventory does not match the deployment")
	}
	for _, name := range got {
		var status struct {
			Instance    string `json:"instance"`
			Application string `json:"application"`
			Backup      string `json:"backup"`
		}
		if err := json.Unmarshal(response.Instances[name], &status); err != nil || status.Instance != name {
			return errors.New("drain response does not match the requested instance")
		}
		if !oneOf(status.Application, "running", "stopped", "unknown") || !oneOf(status.Backup, "pending", "complete", "not-required", "failed") {
			return errors.New("drain response has invalid application or backup status")
		}
		if status.Application != "stopped" || !oneOf(status.Backup, "complete", "not-required") {
			return errors.New("drain lacks complete shutdown and backup evidence")
		}
	}
	if response.Draining == nil || !*response.Draining {
		return errors.New("drain lacks complete shutdown and backup evidence")
	}
	return nil
}

func oneOf(value string, allowed ...string) bool {
	for _, candidate := range allowed {
		if value == candidate {
			return true
		}
	}
	return false
}

// ParseInstances validates the comma-separated configured instance IDs.
func ParseInstances(value string) ([]string, error) {
	var instances []string
	seen := map[string]bool{}
	for _, name := range strings.Split(value, ",") {
		if !instanceID.MatchString(name) || seen[name] {
			return nil, fmt.Errorf("invalid configured instance list")
		}
		seen[name] = true
		instances = append(instances, name)
	}
	return instances, nil
}
