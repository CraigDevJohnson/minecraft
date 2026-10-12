package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

// StopFailed is the phase of a stop of ours that failed or ran out of time. The state rules then follow the
// host again: a game that still answers is ready and can be stopped again, a host without one is in maintenance.
const StopFailed = "stop-failed"

const (
	// stopDeadline bounds a stop this function began: the drain's own hour on the host, the EC2 stop and the
	// margins. A stop still unfinished after it is reported failed and the state follows the host again.
	stopDeadline = 75 * time.Minute
	// drainTimeoutSeconds is how long the host may take to save, archive and stop the game, as in the
	// workstation stop and the idle stop.
	drainTimeoutSeconds = 3600
	// invisibleGrace covers Run Command's eventual consistency right after SendCommand.
	invisibleGrace = 2 * time.Minute
	// confirmWindow bounds the fresh host status read before the EC2 stop.
	confirmWindow = 20 * time.Second
	// parkedAddress is where the players' hostname points while the host is stopped: a never-routed address,
	// the same the workstation stop and the idle stop use (ADR 0002).
	parkedAddress = "192.0.2.1"
)

// Refusals a stop request can answer with. The site relays them as 409.
var (
	// ErrStarting means a start is still under way; a stop waits for it to settle.
	ErrStarting = errors.New("a start is under way")
	// ErrPlayersUnknown means the game did not answer, so nobody can say whether anyone is playing.
	ErrPlayersUnknown = errors.New("the game could not be asked who is online")
)

// ErrPlayersOnline means players are online and the stop was not forced.
type ErrPlayersOnline struct{ Players int }

func (e ErrPlayersOnline) Error() string { return fmt.Sprintf("%d players are online", e.Players) }

// Stop drains and stops a running server and answers its status. It refuses while anyone is playing, unless
// forced, and while a start is under way. Repeating it while a stop is under way, or once the host is
// stopped, changes nothing.
func (l *Launcher) Stop(ctx context.Context, force bool) (Status, bool, error) {
	if l.Config.HostID == "" {
		return Status{}, false, ErrNoServer
	}
	h, err := l.describeHost(ctx)
	if err != nil {
		return l.unknown(ctx), false, err
	}
	session, err := l.loadSession(ctx)
	if err != nil {
		return l.answer(Unknown, unreadable), false, err
	}
	state, players, err := l.classify(ctx, h, session)
	if err != nil {
		return l.answer(Unknown, l.current(h, session)), false, err
	}
	current := l.current(h, session)
	switch {
	case h.state == ec2types.InstanceStateNamePending || state == Starting:
		return l.answer(state, current), false, ErrStarting
	case h.state == ec2types.InstanceStateNameStopped && unparked(session):
		// A stop of ours has the host off but did not park the hostname, which may still point at the old
		// address: park it now. Until that succeeds the status shows no address.
		if err := l.park(ctx, session); err != nil {
			return l.answer(state, current), false, fmt.Errorf("park the hostname: %w", err)
		}
		parked := session
		parked.DNS = "current"
		if parked.Phase == Stopping {
			parked.Phase, parked.Detail = Stopped, ""
		}
		if err := l.saveParked(ctx, parked); err != nil {
			return l.answer(state, current), false, err
		}
		return l.answer(state, parked), false, nil
	case h.state != ec2types.InstanceStateNameRunning || state == Stopping:
		// Stopped, stopping, or a stop already under way (ours or the idle stop's): nothing to do.
		return l.answer(state, current), false, nil
	}
	if !force {
		// Only a game that answers can say nobody is playing. A host in maintenance, or a start of ours that
		// failed, has no game to ask; an operator stops those with the force flag.
		if state != Ready {
			return l.answer(state, current), false, ErrPlayersUnknown
		}
		if players > 0 {
			return l.answer(state, current), false, ErrPlayersOnline{Players: players}
		}
	}
	// Record the stop before anything else, so every status read from here on shows it, and tie it to this
	// run of the host: a later run never inherits it.
	session = Session{RequestedAt: l.Now().UTC(), LaunchTime: h.launchTime, Phase: Stopping, Force: force}
	if err := l.saveSession(ctx, session); err != nil {
		return l.answer(Unknown, Session{}), false, err
	}
	if err := l.FollowUpStop(ctx); err != nil {
		// The stop stands; without its follow-up it is reported failed after stopDeadline and can be retried.
		return l.answer(Stopping, session), true, fmt.Errorf("hand off the rest of the stop: %w", err)
	}
	return l.answer(Stopping, session), true, nil
}

// unparked reports whether a stop of ours may have left the hostname pointing at the host's old address once
// the host is off: its parking failed, or its run ended between the EC2 stop and the parking.
func unparked(session Session) bool {
	switch session.Phase {
	case Stopping, StopFailed, Stopped:
		return session.DNS == "pending" || session.DNS == "failed"
	}
	return false
}

// park points the players' hostname at the parked address once a stop of ours has the host off. A Member may
// start the host as soon as EC2 shows it stopped, and that start's follow-up points the hostname at the new
// run once it runs; a park that lands after that would leave a ready game without its address. So once the
// park is done, a start recorded meanwhile whose host already runs gets the hostname back. A start recorded
// after that check points the hostname itself once its host runs, which is later than this park.
func (l *Launcher) park(ctx context.Context, stop Session) error {
	if err := l.PointDNS(ctx, parkedAddress); err != nil {
		return err
	}
	saved, err := l.loadSession(ctx)
	if err != nil {
		return fmt.Errorf("check for a start after parking: %w", err)
	}
	if saved.RequestedAt.Equal(stop.RequestedAt) {
		return nil
	}
	h, err := l.describeHost(ctx)
	if err != nil {
		return fmt.Errorf("check for a start after parking: %w", err)
	}
	if h.state != ec2types.InstanceStateNameRunning || net.ParseIP(h.publicIP) == nil {
		return nil
	}
	if err := l.PointDNS(ctx, h.publicIP); err != nil {
		return fmt.Errorf("point the hostname back at the host a start ran meanwhile: %w", err)
	}
	return nil
}

// saveParked records the parked hostname on the stop's own record. A start that began meanwhile owns the
// record now and is left alone: it points the hostname at the host itself.
func (l *Launcher) saveParked(ctx context.Context, session Session) error {
	saved, err := l.loadSession(ctx)
	if err != nil {
		return err
	}
	if !saved.RequestedAt.Equal(session.RequestedAt) || !unparked(saved) {
		return nil
	}
	return l.saveSession(ctx, session)
}

// FinishStop does the rest of a stop: the same steps as `minecraft_host.py stop`. It drains the game on the
// host, stops EC2 once the drain's evidence is complete, waits for stopped and parks the players' hostname.
// A run that Lambda ends before the stop's own deadline hands the rest to a fresh run of itself; the record
// carries the drain's command id, so that run follows the same drain.
func (l *Launcher) FinishStop(ctx context.Context) error {
	session, err := l.loadSession(ctx)
	if err != nil {
		return err
	}
	if session.Phase != Stopping {
		if session.Phase == Stopped {
			return nil // a repeated run found the stop already finished
		}
		return errors.New("no stop of this function is under way")
	}
	deadline := session.RequestedAt.Add(stopDeadline - finishMargin)
	if !l.Now().Before(deadline) {
		return l.failWith(ctx, session, errors.New("the stop passed its deadline before its follow-up ran"), StopFailed)
	}
	runCtx, cancel := context.WithTimeout(ctx, deadline.Sub(l.Now()))
	defer cancel()
	err = l.stopSteps(runCtx, &session)
	var online ErrPlayersOnline
	switch {
	case err == nil, errors.Is(err, errFinished):
		return nil
	case errors.As(err, &online), errors.Is(err, ErrPlayersUnknown):
		// Somebody joined after the request found nobody playing, or the game stopped answering: the stop
		// stands down and the host keeps running. That is a refusal like the request's own, not a failure to
		// alarm on.
		return l.record(ctx, session, err, StopFailed)
	case ctx.Err() != nil && l.Now().Before(deadline):
		// This run's own time ended first: another run takes it from here.
		handoff, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
		defer cancel()
		if err := l.FollowUpStop(handoff); err != nil {
			return l.failWith(handoff, session, fmt.Errorf("hand off the rest of the stop: %w", err), StopFailed)
		}
		return nil
	}
	return l.failWith(ctx, session, err, StopFailed)
}

func (l *Launcher) stopSteps(ctx context.Context, session *Session) error {
	if session.DrainCommandID == "" {
		if err := l.waitSSM(ctx); err != nil {
			return err
		}
		if !session.Force {
			if err := l.nobodyJoined(ctx); err != nil {
				return err
			}
		}
		id, err := l.sendCommand(ctx, remoteCommand+" host-drain", "minecraft launcher stop", drainTimeoutSeconds)
		if err != nil {
			return fmt.Errorf("send drain: %w", err)
		}
		session.DrainCommandID = id
		session.DrainSentAt = l.Now().UTC()
		if err := l.saveOwned(ctx, *session); err != nil {
			return err
		}
	}
	if err := l.followDrain(ctx, *session); err != nil {
		return err
	}
	h, err := l.describeHost(ctx)
	if err != nil {
		return err
	}
	switch h.state {
	case ec2types.InstanceStateNameRunning:
		if !h.launchTime.Equal(session.LaunchTime) {
			return errors.New("the host was started again before this stop finished")
		}
		// Only a running host can answer; its fresh status must agree with the drain before EC2 stops it.
		if err := l.confirmDrain(ctx); err != nil {
			return err
		}
	case ec2types.InstanceStateNameStopping, ec2types.InstanceStateNameStopped:
		// An earlier run of this stop, or someone else's stop, got there first. The drain's evidence above
		// stands, and the host can no longer answer a command; this run waits, parks and records the outcome.
	default:
		return fmt.Errorf("host cannot be stopped from %s", h.state)
	}
	// The record owes the parking before the host goes off, so a stop whose run ends between the EC2 stop and
	// the parking, or whose last record is lost, shows no address once the host is off and is parked by a
	// repeated stop.
	if session.DNS != "pending" {
		session.DNS = "pending"
		if err := l.saveOwned(ctx, *session); err != nil {
			return err
		}
	}
	if h.state == ec2types.InstanceStateNameRunning {
		if _, err := l.EC2.StopInstances(ctx, &ec2.StopInstancesInput{InstanceIds: []string{l.Config.HostID}}); err != nil {
			return fmt.Errorf("stop host after verified drain: %w", err)
		}
	}
	if err := l.waitStopped(ctx); err != nil {
		return err
	}
	// Parking goes last, as in the workstation stop; its failure is recorded and alarmed, never hidden.
	parkErr := l.park(ctx, *session)
	session.DNS = "current"
	if parkErr != nil {
		session.DNS = "failed"
	}
	session.Phase = Stopped
	session.Detail = ""
	if err := l.saveOwned(ctx, *session); err != nil {
		return errors.Join(parkErr, err)
	}
	if parkErr != nil {
		return fmt.Errorf("the host stopped but DNS was not parked: %w", parkErr)
	}
	return nil
}

// nobodyJoined asks the game once more, just before the drain, whether anybody is playing: this run may begin a
// moment after the request found nobody online. Like the request, it drains only a game that answers with nobody
// on it; one that stopped answering in between is left for an operator's forced stop.
func (l *Launcher) nobodyJoined(ctx context.Context) error {
	h, err := l.describeHost(ctx)
	if err != nil {
		return err
	}
	if h.state != ec2types.InstanceStateNameRunning {
		return nil
	}
	players, up := l.gameAnswers(ctx, h.publicIP)
	switch {
	case !up:
		return ErrPlayersUnknown
	case players > 0:
		return ErrPlayersOnline{Players: players}
	}
	return nil
}

// followDrain waits for the drain to finish and checks its evidence the way the workstation stop and the idle
// stop do. A drain that fails, or vanishes, fails the stop; the host is left running for a retry.
func (l *Launcher) followDrain(ctx context.Context, session Session) error {
	for {
		invocation, err := l.SSM.GetCommandInvocation(ctx, &ssm.GetCommandInvocationInput{
			CommandId: aws.String(session.DrainCommandID), InstanceId: aws.String(l.Config.HostID),
		})
		var missing *ssmtypes.InvocationDoesNotExist
		switch {
		case errors.As(err, &missing):
			if l.Now().Sub(session.DrainSentAt) > invisibleGrace {
				return errors.New("the drain never became visible")
			}
		case err != nil:
			return fmt.Errorf("follow drain: %w", err)
		case invocation.Status == ssmtypes.CommandInvocationStatusSuccess:
			if invocation.ResponseCode != 0 {
				return errors.New("the host's adapter refused the drain")
			}
			return validateDrain(aws.ToString(invocation.StandardOutputContent), l.Config.Instance)
		case invocation.Status == ssmtypes.CommandInvocationStatusPending, invocation.Status == ssmtypes.CommandInvocationStatusInProgress, invocation.Status == ssmtypes.CommandInvocationStatusDelayed:
		default:
			return fmt.Errorf("the drain ended with status %s", invocation.Status)
		}
		if err := l.pause(ctx, "the drain did not finish"); err != nil {
			return err
		}
	}
}

// confirmDrain reads a fresh host status just before the EC2 stop. A reboot keeps EC2's launch time but
// clears the boot-bound drain barrier, so the drain's own answer is never reused.
func (l *Launcher) confirmDrain(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, confirmWindow)
	defer cancel()
	id, err := l.sendCommand(ctx, remoteCommand+" host-status", "minecraft launcher stop check", int(confirmWindow.Seconds()))
	if err != nil {
		return fmt.Errorf("send host status: %w", err)
	}
	for {
		invocation, err := l.SSM.GetCommandInvocation(ctx, &ssm.GetCommandInvocationInput{CommandId: aws.String(id), InstanceId: aws.String(l.Config.HostID)})
		var missing *ssmtypes.InvocationDoesNotExist
		switch {
		case errors.As(err, &missing):
		case err != nil:
			return fmt.Errorf("read fresh host status: %w", err)
		case invocation.Status == ssmtypes.CommandInvocationStatusSuccess:
			if invocation.ResponseCode != 0 || validateDrain(aws.ToString(invocation.StandardOutputContent), l.Config.Instance) != nil {
				return errors.New("fresh host status lacks complete drain evidence")
			}
			return nil
		case invocation.Status == ssmtypes.CommandInvocationStatusPending, invocation.Status == ssmtypes.CommandInvocationStatusInProgress, invocation.Status == ssmtypes.CommandInvocationStatusDelayed:
		default:
			return fmt.Errorf("the fresh host status ended with status %s", invocation.Status)
		}
		if err := l.pause(ctx, "the fresh host status did not finish"); err != nil {
			return err
		}
	}
}

// waitStopped waits for EC2 to report the host stopped. EC2 may show it running for a moment after StopInstances.
func (l *Launcher) waitStopped(ctx context.Context) error {
	for {
		h, err := l.describeHost(ctx)
		if err != nil {
			return err
		}
		switch h.state {
		case ec2types.InstanceStateNameStopped:
			return nil
		case ec2types.InstanceStateNameRunning, ec2types.InstanceStateNameStopping, ec2types.InstanceStateNameShuttingDown:
		default:
			return fmt.Errorf("host cannot reach stopped from %s", h.state)
		}
		if err := l.pause(ctx, "the host did not stop"); err != nil {
			return err
		}
	}
}

// validateDrain applies the evidence rules of `minecraft_host.py stop` and the idle stop's ValidateDrain, for
// the one configured instance: the response covers exactly that instance, confirms the drain barrier, and
// reports the game stopped with its backup complete (or not required for a never-started instance). Anything
// else forbids the EC2 stop.
func validateDrain(output string, instance string) error {
	var response struct {
		Draining  *bool                      `json:"draining"`
		Instances map[string]json.RawMessage `json:"instances"`
	}
	if err := json.Unmarshal([]byte(output), &response); err != nil {
		return errors.New("drain returned invalid completion evidence")
	}
	if instance == "" || len(response.Instances) != 1 || response.Instances[instance] == nil {
		return errors.New("drain instance inventory does not match the deployment")
	}
	var status struct {
		Instance    string `json:"instance"`
		Application string `json:"application"`
		Backup      string `json:"backup"`
	}
	if err := json.Unmarshal(response.Instances[instance], &status); err != nil || status.Instance != instance {
		return errors.New("drain response does not match the requested instance")
	}
	if !oneOf(status.Application, "running", "stopped", "unknown") || !oneOf(status.Backup, "pending", "complete", "not-required", "failed") {
		return errors.New("drain response has invalid application or backup status")
	}
	if status.Application != "stopped" || !oneOf(status.Backup, "complete", "not-required") || response.Draining == nil || !*response.Draining {
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
