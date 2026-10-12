package main

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

const (
	drainOutput  = `{"draining": true, "instances": {"family": {"instance": "family", "application": "stopped", "backup": "complete"}}}`
	statusOutput = `{"draining": true, "instances": {"family": {"instance": "family", "application": "stopped", "backup": "complete", "ready": false}}}`
)

func TestStopARunningServerNobodyIsPlaying(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.gameUp = true
	status, stopped, err := r.launcher.Stop(context.Background(), false)
	if err != nil || !stopped || status.State != Stopping || r.followUpStops != 1 || r.ec2.stops != 0 {
		t.Fatalf("state=%s stopped=%v err=%v followUps=%d stops=%d", status.State, stopped, err, r.followUpStops, r.ec2.stops)
	}
	session := r.session(t)
	if session.Phase != Stopping || !session.RequestedAt.Equal(now) || !session.LaunchTime.Equal(now.Add(-time.Hour)) || session.DrainCommandID != "" {
		t.Fatalf("session %+v", session)
	}
	// The stop is recorded, so the next status is stopping without asking the game.
	r.pinged = nil
	if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Stopping || len(r.pinged) != 0 {
		t.Fatalf("state=%s err=%v pinged=%v", status.State, err, r.pinged)
	}
}

func TestStopRefusedWhilePlayersAreOnline(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.gameUp = true
	r.players = 2
	status, stopped, err := r.launcher.Stop(context.Background(), false)
	var online ErrPlayersOnline
	if !errors.As(err, &online) || online.Players != 2 || stopped || status.State != Ready || r.followUpStops != 0 {
		t.Fatalf("state=%s stopped=%v err=%v", status.State, stopped, err)
	}
	if session := r.session(t); session.Phase != "" {
		t.Fatalf("session %+v", session)
	}
	// The operator's workflow may force it.
	if status, stopped, err := r.launcher.Stop(context.Background(), true); err != nil || !stopped || status.State != Stopping {
		t.Fatalf("state=%s stopped=%v err=%v", status.State, stopped, err)
	}
}

func TestStopRefusedWhenTheGameDoesNotAnswer(t *testing.T) {
	for _, tc := range []struct {
		name    string
		session *Session
	}{
		{name: "maintenance"},
		{name: "our start failed", session: &Session{RequestedAt: now.Add(-20 * time.Minute), LaunchTime: now.Add(-time.Hour), Phase: Failed}},
		{name: "our stop failed", session: &Session{RequestedAt: now.Add(-20 * time.Minute), LaunchTime: now.Add(-time.Hour), Phase: StopFailed}},
	} {
		r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
		if tc.session != nil {
			r.setSession(t, *tc.session)
		}
		status, stopped, err := r.launcher.Stop(context.Background(), false)
		if !errors.Is(err, ErrPlayersUnknown) || stopped || r.followUpStops != 0 || status.State == Stopping {
			t.Fatalf("%s: state=%s stopped=%v err=%v", tc.name, status.State, stopped, err)
		}
		if status, stopped, err := r.launcher.Stop(context.Background(), true); err != nil || !stopped || status.State != Stopping || r.followUpStops != 1 {
			t.Fatalf("%s forced: state=%s stopped=%v err=%v", tc.name, status.State, stopped, err)
		}
	}
}

func TestStopRefusedDuringAStart(t *testing.T) {
	r := newRig(ec2types.InstanceStateNamePending, 0)
	if _, stopped, err := r.launcher.Stop(context.Background(), true); !errors.Is(err, ErrStarting) || stopped {
		t.Fatalf("stopped=%v err=%v", stopped, err)
	}
	r = newRig(ec2types.InstanceStateNameRunning, 3*time.Minute)
	r.setSession(t, Session{RequestedAt: now.Add(-4 * time.Minute), Phase: Starting})
	if _, stopped, err := r.launcher.Stop(context.Background(), true); !errors.Is(err, ErrStarting) || stopped || r.session(t).Phase != Starting {
		t.Fatalf("stopped=%v err=%v", stopped, err)
	}
	// A host that launched moments ago without this function's start is starting too.
	r = newRig(ec2types.InstanceStateNameRunning, 2*time.Minute)
	if _, stopped, err := r.launcher.Stop(context.Background(), true); !errors.Is(err, ErrStarting) || stopped {
		t.Fatalf("stopped=%v err=%v", stopped, err)
	}
}

func TestStopIsIdempotent(t *testing.T) {
	for _, tc := range []struct {
		name    string
		host    ec2types.InstanceStateName
		session *Session
		idle    string
		want    string
	}{
		{name: "stopped", host: ec2types.InstanceStateNameStopped, want: Stopped},
		{name: "EC2 stopping", host: ec2types.InstanceStateNameStopping, want: Stopping},
		{name: "our stop under way", host: ec2types.InstanceStateNameRunning, session: &Session{RequestedAt: now.Add(-5 * time.Minute), LaunchTime: now.Add(-time.Hour), Phase: Stopping, DrainCommandID: commandID}, want: Stopping},
		{name: "the idle stop is draining", host: ec2types.InstanceStateNameRunning, idle: `{"launch_time":"2026-10-11T15:00:00Z","drain_command_id":"x"}`, want: Stopping},
	} {
		r := newRig(tc.host, time.Hour)
		r.gameUp = true
		r.players = 1
		if tc.session != nil {
			r.setSession(t, *tc.session)
		}
		if tc.idle != "" {
			r.launcher.Config.IdleStopStateParameter = idleStateName
			r.ssm.params[idleStateName] = tc.idle
		}
		before := r.ssm.params[stateName]
		status, stopped, err := r.launcher.Stop(context.Background(), false)
		if err != nil || stopped || status.State != tc.want || r.followUpStops != 0 || r.ssm.params[stateName] != before {
			t.Fatalf("%s: state=%s stopped=%v err=%v followUps=%d", tc.name, status.State, stopped, err, r.followUpStops)
		}
	}
}

func TestAStoppedHostLeftUnparkedShowsNoAddressUntilAStopParksIt(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameStopped, time.Hour)
	r.setSession(t, Session{RequestedAt: now.Add(-time.Hour), LaunchTime: now.Add(-2 * time.Hour), Phase: Stopped, DNS: "failed", DrainCommandID: commandID})
	status, err := r.launcher.Status(context.Background())
	if err != nil || status.State != Stopped || status.Connection.Java != nil {
		t.Fatalf("an unparked hostname must not be offered: state=%s connection=%+v err=%v", status.State, status.Connection, err)
	}
	r.dnsErr = errors.New("cloudflare down")
	status, stopped, err := r.launcher.Stop(context.Background(), false)
	if err == nil || stopped || status.State != Stopped || status.Connection.Java != nil || strings.Join(r.pointedAt, ",") != parkedAddress {
		t.Fatalf("a parking retry that fails must say so: err=%v stopped=%v status=%+v pointed=%v", err, stopped, status, r.pointedAt)
	}
	if session := r.session(t); !unparked(session) {
		t.Fatalf("session %+v", session)
	}
	r.dnsErr = nil
	status, stopped, err = r.launcher.Stop(context.Background(), false)
	if err != nil || stopped || status.State != Stopped || status.Connection.Java == nil || len(r.pointedAt) != 2 || r.ec2.stops != 0 {
		t.Fatalf("a repeated stop must park the hostname: err=%v stopped=%v status=%+v pointed=%v stops=%d", err, stopped, status, r.pointedAt, r.ec2.stops)
	}
	if session := r.session(t); session.Phase != Stopped || session.DNS != "current" || session.DrainCommandID != commandID {
		t.Fatalf("session %+v", session)
	}
	status, err = r.launcher.Status(context.Background())
	if err != nil || status.State != Stopped || status.Connection.Java == nil {
		t.Fatalf("status after parking: %+v err=%v", status, err)
	}
	if _, _, err := r.launcher.Stop(context.Background(), false); err != nil || len(r.pointedAt) != 2 {
		t.Fatalf("a parked hostname is not parked again: err=%v pointed=%v", err, r.pointedAt)
	}
}

func TestParkingLeavesAStartThatBeganMeanwhileAlone(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameStopped, time.Hour)
	r.setSession(t, Session{RequestedAt: now.Add(-time.Hour), LaunchTime: now.Add(-2 * time.Hour), Phase: Stopped, DNS: "failed"})
	start := Session{RequestedAt: now, Phase: Starting}
	r.launcher.PointDNS = func(_ context.Context, target string) error {
		r.pointedAt = append(r.pointedAt, target)
		r.setSession(t, start) // a Member pressed Start while the hostname was being parked
		return nil
	}
	if _, _, err := r.launcher.Stop(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	if session := r.session(t); session.Phase != Starting || !session.RequestedAt.Equal(now) {
		t.Fatalf("the start's record was overwritten: %+v", session)
	}
}

func TestAStopLostAfterEC2StoppedTheHostIsParkedByARepeatedStop(t *testing.T) {
	r := stopRig(t)
	var owed Session
	r.ec2.onStop = func() { owed = r.session(t) }
	if err := r.launcher.FinishStop(context.Background()); err != nil {
		t.Fatal(err)
	}
	if owed.Phase != Stopping || owed.DNS != "pending" || owed.DrainCommandID != commandID {
		t.Fatalf("the record must owe the parking before EC2 stops the host: %+v", owed)
	}
	// The run that stopped EC2 ended there, or its handoff failed: the host is off, the record owes the parking.
	failed := owed
	failed.Phase, failed.Detail = StopFailed, "hand off the rest of the stop: throttled"
	for _, tc := range []struct {
		lost Session
		want string
	}{{owed, Stopped}, {failed, StopFailed}} {
		r := newRig(ec2types.InstanceStateNameStopped, time.Hour)
		r.setSession(t, tc.lost)
		if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Stopped || status.Connection.Java != nil {
			t.Fatalf("%s: an unparked hostname must not be offered: %+v err=%v", tc.lost.Phase, status, err)
		}
		status, stopped, err := r.launcher.Stop(context.Background(), false)
		if err != nil || stopped || status.State != Stopped || status.Connection.Java == nil || strings.Join(r.pointedAt, ",") != parkedAddress || r.ec2.stops != 0 {
			t.Fatalf("%s: err=%v stopped=%v status=%+v pointed=%v stops=%d", tc.lost.Phase, err, stopped, status, r.pointedAt, r.ec2.stops)
		}
		if session := r.session(t); session.Phase != tc.want || session.DNS != "current" || !session.RequestedAt.Equal(now) {
			t.Fatalf("%s: session %+v", tc.lost.Phase, session)
		}
	}
	// A stop that failed before EC2 stopped the host owes nothing: whoever stopped the host later parked it.
	r = newRig(ec2types.InstanceStateNameStopped, time.Hour)
	r.setSession(t, Session{RequestedAt: now, LaunchTime: now.Add(-time.Hour), Phase: StopFailed, Detail: "the drain ended with status Failed"})
	if status, _, err := r.launcher.Stop(context.Background(), false); err != nil || status.Connection.Java == nil || len(r.pointedAt) != 0 {
		t.Fatalf("status=%+v err=%v pointed=%v", status, err, r.pointedAt)
	}
}

func TestParkingGivesTheHostnameBackToAStartThatRanMeanwhile(t *testing.T) {
	start := Session{RequestedAt: now.Add(time.Minute), Phase: Starting}
	// A Member started the host while the park was on its way, and the start's follow-up pointed the hostname
	// at the new run before the park landed.
	startMeanwhile := func(r *rig) {
		r.launcher.PointDNS = func(_ context.Context, target string) error {
			r.pointedAt = append(r.pointedAt, target)
			if target == parkedAddress {
				r.setSession(t, start)
				r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}
				r.ec2.launch = now.Add(time.Minute)
			}
			return nil
		}
	}
	r := stopRig(t)
	startMeanwhile(r)
	_ = r.launcher.FinishStop(context.Background())
	if strings.Join(r.pointedAt, ",") != parkedAddress+","+address {
		t.Fatalf("the follow-up's park outlasted the start: pointed=%v", r.pointedAt)
	}
	if session := r.session(t); session != start {
		t.Fatalf("the start's record was overwritten: %+v", session)
	}
	r = newRig(ec2types.InstanceStateNameStopped, time.Hour)
	r.setSession(t, Session{RequestedAt: now.Add(-time.Hour), LaunchTime: now.Add(-2 * time.Hour), Phase: Stopped, DNS: "failed"})
	startMeanwhile(r)
	if _, _, err := r.launcher.Stop(context.Background(), false); err != nil || strings.Join(r.pointedAt, ",") != parkedAddress+","+address {
		t.Fatalf("a repeated stop's park outlasted the start: err=%v pointed=%v", err, r.pointedAt)
	}
	if session := r.session(t); session != start {
		t.Fatalf("the start's record was overwritten: %+v", session)
	}
}

func TestStatusFollowsTheHostOnceOurStopRanOut(t *testing.T) {
	stale := Session{RequestedAt: now.Add(-stopDeadline), LaunchTime: now.Add(-2 * time.Hour), Phase: Stopping, DrainCommandID: commandID}
	r := newRig(ec2types.InstanceStateNameRunning, 2*time.Hour)
	r.gameUp = true
	r.setSession(t, stale)
	if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Ready {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
	r = newRig(ec2types.InstanceStateNameRunning, 2*time.Hour)
	r.setSession(t, stale)
	if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Maintenance {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
	// A stop tied to an earlier run does not follow a later one.
	r = newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.setSession(t, Session{RequestedAt: now.Add(-5 * time.Minute), LaunchTime: now.Add(-3 * time.Hour), Phase: Stopping})
	if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Maintenance {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
}

func TestStopRecordsBeforeItsFollowUp(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.gameUp = true
	r.ssm.putErr = errors.New("denied")
	if _, stopped, err := r.launcher.Stop(context.Background(), false); err == nil || stopped || r.followUpStops != 0 {
		t.Fatalf("stopped=%v err=%v followUps=%d", stopped, err, r.followUpStops)
	}
	r = newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.gameUp = true
	r.followStopErr = errors.New("throttled")
	status, stopped, err := r.launcher.Stop(context.Background(), false)
	if err == nil || !stopped || status.State != Stopping || r.session(t).Phase != Stopping {
		t.Fatalf("state=%s stopped=%v err=%v", status.State, stopped, err)
	}
}

func TestStartIsUnchangedWhileOurStopRuns(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.setSession(t, Session{RequestedAt: now.Add(-5 * time.Minute), LaunchTime: now.Add(-time.Hour), Phase: Stopping, DrainCommandID: commandID})
	status, started, err := r.launcher.Start(context.Background())
	if err != nil || started || status.State != Stopping || r.followUps != 0 || r.session(t).Phase != Stopping {
		t.Fatalf("state=%s started=%v err=%v", status.State, started, err)
	}
}

// stopRig has a stop of this function recorded against a running host, with the host answering a drain and a
// fresh host status, then stopping. The host is described running twice: before the drain, for the player
// check, and after it.
func stopRig(t *testing.T) *rig {
	r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.gameUp = true // nobody playing
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning, ec2types.InstanceStateNameRunning, ec2types.InstanceStateNameStopping, ec2types.InstanceStateNameStopped}
	r.ssm.online = []bool{false, true}
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}, success(drainOutput), success(statusOutput)}
	r.setSession(t, Session{RequestedAt: now, LaunchTime: now.Add(-time.Hour), Phase: Stopping})
	return r
}

func TestFinishStopStandsDownWhenSomebodyJoinedAfterTheRequest(t *testing.T) {
	r := stopRig(t)
	r.gameUp, r.players = true, 2
	if err := r.launcher.FinishStop(context.Background()); err != nil {
		t.Fatalf("a player who joined is a refusal, not a failure to alarm on: %v", err)
	}
	if len(r.ssm.commands) != 0 || r.ec2.stops != 0 || len(r.pointedAt) != 0 {
		t.Fatalf("nothing may drain under a player: commands=%v stops=%d pointed=%v", r.ssm.commands, r.ec2.stops, r.pointedAt)
	}
	if session := r.session(t); session.Phase != StopFailed || session.Detail != "2 players are online" {
		t.Fatalf("session %+v", session)
	}
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}
	if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Ready {
		t.Fatalf("the game they joined is ready: state=%s err=%v", status.State, err)
	}
}

func TestFinishStopStandsDownWhenTheGameStoppedAnswering(t *testing.T) {
	r := stopRig(t)
	r.gameUp = false
	if err := r.launcher.FinishStop(context.Background()); err != nil {
		t.Fatalf("an unknown player count is a refusal, not a failure to alarm on: %v", err)
	}
	if len(r.ssm.commands) != 0 || r.ec2.stops != 0 || len(r.pointedAt) != 0 || len(r.pinged) == 0 {
		t.Fatalf("nothing may drain without a player count: commands=%v stops=%d pointed=%v pinged=%v", r.ssm.commands, r.ec2.stops, r.pointedAt, r.pinged)
	}
	if session := r.session(t); session.Phase != StopFailed || session.Detail != ErrPlayersUnknown.Error() {
		t.Fatalf("session %+v", session)
	}
}

func TestAForcedStopDrainsWhoeverIsPlaying(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.gameUp, r.players = true, 2
	if _, stopped, err := r.launcher.Stop(context.Background(), true); err != nil || !stopped || !r.session(t).Force {
		t.Fatalf("stopped=%v err=%v session=%+v; the record keeps the force flag for the follow-up", stopped, err, r.session(t))
	}
	r = stopRig(t)
	session := r.session(t)
	session.Force = true
	r.setSession(t, session)
	r.gameUp, r.players = true, 2
	if err := r.launcher.FinishStop(context.Background()); err != nil || r.ec2.stops != 1 || len(r.pinged) != 0 {
		t.Fatalf("err=%v stops=%d pinged=%v", err, r.ec2.stops, r.pinged)
	}
}

func TestFinishStopRunsTheWorkstationSteps(t *testing.T) {
	r := stopRig(t)
	if err := r.launcher.FinishStop(context.Background()); err != nil {
		t.Fatal(err)
	}
	want := remoteCommand + " host-drain|" + remoteCommand + " host-status"
	if strings.Join(r.ssm.commands, "|") != want {
		t.Fatalf("commands %v", r.ssm.commands)
	}
	if r.ec2.stops != 1 || strings.Join(r.pointedAt, ",") != parkedAddress || r.followUpStops != 0 {
		t.Fatalf("stops=%d DNS pointed at %v followUps=%d", r.ec2.stops, r.pointedAt, r.followUpStops)
	}
	session := r.session(t)
	if session.Phase != Stopped || session.DNS != "current" || session.DrainCommandID != commandID || !session.DrainSentAt.Equal(now) || !session.RequestedAt.Equal(now) {
		t.Fatalf("session %+v", session)
	}
	// The stopped host reads as stopped, and a repeated follow-up finds nothing left to do.
	if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Stopped {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
	if err := r.launcher.FinishStop(context.Background()); err != nil || r.ec2.stops != 1 || len(r.ssm.commands) != 2 {
		t.Fatalf("err=%v stops=%d commands=%v", err, r.ec2.stops, r.ssm.commands)
	}
}

func TestFinishStopDrainsBeforeAnythingStops(t *testing.T) {
	for _, answer := range []ssm.GetCommandInvocationOutput{
		{Status: ssmtypes.CommandInvocationStatusFailed},
		{Status: ssmtypes.CommandInvocationStatusTimedOut},
		{Status: ssmtypes.CommandInvocationStatusSuccess, ResponseCode: 1, StandardOutputContent: aws.String(drainOutput)},
		success(`{"draining": true, "instances": {"family": {"instance": "family", "application": "running", "backup": "pending"}}}`),
		success(`{"draining": false, "instances": {"family": {"instance": "family", "application": "stopped", "backup": "complete"}}}`),
		success(`{"draining": true, "instances": {"other": {"instance": "other", "application": "stopped", "backup": "complete"}}}`),
		success(`{"error": "host drain refused"}`),
		success(`not json`),
	} {
		r := stopRig(t)
		r.ssm.invocation = []ssm.GetCommandInvocationOutput{answer}
		if err := r.launcher.FinishStop(context.Background()); err == nil {
			t.Fatalf("%+v must fail the stop", answer)
		}
		if session := r.session(t); session.Phase != StopFailed || session.Detail == "" || r.ec2.stops != 0 || len(r.pointedAt) != 0 {
			t.Fatalf("%+v: session %+v stops=%d pointed=%v", answer, session, r.ec2.stops, r.pointedAt)
		}
	}
	// A fresh host status that no longer shows the drain forbids the stop too.
	r := stopRig(t)
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{success(drainOutput), success(`{"draining": false, "instances": {"family": {"instance": "family", "application": "running", "backup": "pending"}}}`)}
	if err := r.launcher.FinishStop(context.Background()); err == nil || r.ec2.stops != 0 || r.session(t).Phase != StopFailed {
		t.Fatalf("err=%v stops=%d", err, r.ec2.stops)
	}
}

func TestFinishStopHandsOffWhenItsRunEndsFirst(t *testing.T) {
	r := stopRig(t)
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if err := r.launcher.FinishStop(ctx); err != nil || r.followUpStops != 1 || r.ec2.stops != 0 {
		t.Fatalf("err=%v followUps=%d stops=%d", err, r.followUpStops, r.ec2.stops)
	}
	session := r.session(t)
	if session.Phase != Stopping || session.DrainCommandID != commandID || !session.DrainSentAt.Equal(now) {
		t.Fatalf("session %+v", session)
	}
	// The next run follows the same drain instead of sending another.
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning, ec2types.InstanceStateNameStopped}
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{success(drainOutput), success(statusOutput)}
	if err := r.launcher.FinishStop(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(r.ssm.commands) != 2 || r.ec2.stops != 1 || r.session(t).Phase != Stopped {
		t.Fatalf("commands=%v stops=%d session=%+v", r.ssm.commands, r.ec2.stops, r.session(t))
	}
	// A hand-off that cannot be made fails the stop, so the alarm sees it.
	r = stopRig(t)
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}}
	r.followStopErr = errors.New("throttled")
	ctx, cancel = context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if err := r.launcher.FinishStop(ctx); err == nil || r.session(t).Phase != StopFailed {
		t.Fatalf("err=%v session=%+v", err, r.session(t))
	}
}

func TestFinishStopGivesUpAtTheStopsDeadline(t *testing.T) {
	r := stopRig(t)
	r.launcher.Now = func() time.Time { return now.Add(stopDeadline) }
	if err := r.launcher.FinishStop(context.Background()); err == nil || len(r.ssm.commands) != 0 || r.followUpStops != 0 {
		t.Fatalf("err=%v commands=%v followUps=%d", err, r.ssm.commands, r.followUpStops)
	}
	if session := r.session(t); session.Phase != StopFailed || session.Detail == "" {
		t.Fatalf("session %+v", session)
	}
	// A run whose time ends as the stop's deadline passes fails rather than handing off.
	r = stopRig(t)
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}}
	var late atomic.Bool
	r.launcher.Now = func() time.Time {
		if late.Load() {
			return now.Add(stopDeadline)
		}
		return now
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	time.AfterFunc(10*time.Millisecond, func() { late.Store(true) })
	if err := r.launcher.FinishStop(ctx); err == nil || r.followUpStops != 0 || r.session(t).Phase != StopFailed {
		t.Fatalf("err=%v followUps=%d session=%+v", err, r.followUpStops, r.session(t))
	}
}

func TestFinishStopParksTheHostnameWhenSomeoneElseStoppedTheHost(t *testing.T) {
	r := stopRig(t)
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameStopped}
	if err := r.launcher.FinishStop(context.Background()); err != nil || r.ec2.stops != 0 || strings.Join(r.pointedAt, ",") != parkedAddress {
		t.Fatalf("err=%v stops=%d pointed=%v", err, r.ec2.stops, r.pointedAt)
	}
	if session := r.session(t); session.Phase != Stopped {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishStopResumedAfterTheHostStoppedDoesNotAskTheHostAgain(t *testing.T) {
	r := stopRig(t)
	// An earlier run sent the drain and stopped EC2, then ran out of time; this run picks it up.
	r.setSession(t, Session{RequestedAt: now, LaunchTime: now.Add(-time.Hour), Phase: Stopping, DrainCommandID: commandID, DrainSentAt: now})
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameStopping, ec2types.InstanceStateNameStopped}
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{success(drainOutput)}
	if err := r.launcher.FinishStop(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(r.ssm.commands) != 0 || r.ec2.stops != 0 || strings.Join(r.pointedAt, ",") != parkedAddress {
		t.Fatalf("commands=%v stops=%d pointed=%v", r.ssm.commands, r.ec2.stops, r.pointedAt)
	}
	if session := r.session(t); session.Phase != Stopped || session.DNS != "current" {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishStopLeavesARestartedHostAlone(t *testing.T) {
	r := stopRig(t)
	r.ec2.launch = now.Add(-time.Minute)
	if err := r.launcher.FinishStop(context.Background()); err == nil || r.ec2.stops != 0 || len(r.pointedAt) != 0 {
		t.Fatalf("err=%v stops=%d pointed=%v", err, r.ec2.stops, r.pointedAt)
	}
	if session := r.session(t); session.Phase != StopFailed {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishStopRecordsAParkingFailure(t *testing.T) {
	r := stopRig(t)
	r.dnsErr = errors.New("token missing")
	if err := r.launcher.FinishStop(context.Background()); err == nil || r.ec2.stops != 1 {
		t.Fatalf("a parking failure must fail the run so its alarm sees it: err=%v stops=%d", err, r.ec2.stops)
	}
	if session := r.session(t); session.Phase != Stopped || session.DNS != "failed" {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishStopFailsWhenEC2RefusesTheStop(t *testing.T) {
	r := stopRig(t)
	r.ec2.stopErr = errors.New("denied by the boundary")
	if err := r.launcher.FinishStop(context.Background()); err == nil || !strings.Contains(err.Error(), "boundary") || len(r.pointedAt) != 0 {
		t.Fatalf("err=%v pointed=%v", err, r.pointedAt)
	}
	if session := r.session(t); session.Phase != StopFailed || !strings.Contains(session.Detail, "boundary") {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishStopNeedsOurStop(t *testing.T) {
	for _, session := range []Session{{}, {RequestedAt: now, LaunchTime: now.Add(-time.Hour), Phase: Ready}} {
		r := stopRig(t)
		r.setSession(t, session)
		if err := r.launcher.FinishStop(context.Background()); err == nil || len(r.ssm.commands) != 0 || r.ec2.stops != 0 {
			t.Fatalf("%+v: err=%v commands=%v stops=%d", session, err, r.ssm.commands, r.ec2.stops)
		}
		if got := r.session(t); got != session {
			t.Fatalf("session %+v", got)
		}
	}
}

func TestFinishStopWaitsForSSM(t *testing.T) {
	r := stopRig(t)
	r.ssm.online = []bool{false}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	// Lambda's time ran out before the host came online: the next run keeps waiting.
	if err := r.launcher.FinishStop(ctx); err != nil || r.followUpStops != 1 || len(r.ssm.commands) != 0 {
		t.Fatalf("err=%v followUps=%d commands=%v", err, r.followUpStops, r.ssm.commands)
	}
}

func TestFinishStopDrainThatNeverAppearsFails(t *testing.T) {
	r := stopRig(t)
	r.ssm.invocation = nil
	reads := 0
	r.launcher.Now = func() time.Time {
		reads++
		if reads > 3 {
			return now.Add(invisibleGrace + time.Second)
		}
		return now
	}
	if err := r.launcher.FinishStop(context.Background()); err == nil || !strings.Contains(err.Error(), "visible") || r.ec2.stops != 0 {
		t.Fatalf("err=%v stops=%d", err, r.ec2.stops)
	}
}

func TestValidateDrainForOneInstance(t *testing.T) {
	if err := validateDrain(drainOutput, "family"); err != nil {
		t.Fatal(err)
	}
	if err := validateDrain(`{"draining": true, "instances": {"family": {"instance": "family", "application": "stopped", "backup": "not-required"}}}`, "family"); err != nil {
		t.Fatal(err)
	}
	for _, output := range []string{
		drainOutput + "x",
		`{"draining": true, "instances": {}}`,
		`{"draining": true, "instances": {"family": {"instance": "family", "application": "stopped", "backup": "complete"}, "other": {"instance": "other", "application": "stopped", "backup": "complete"}}}`,
		`{"draining": true, "instances": {"family": {"instance": "other", "application": "stopped", "backup": "complete"}}}`,
		`{"draining": true, "instances": {"family": {"instance": "family", "application": "stopped", "backup": "failed"}}}`,
		`{"draining": true, "instances": {"family": {"instance": "family", "application": "halted", "backup": "complete"}}}`,
		`{"instances": {"family": {"instance": "family", "application": "stopped", "backup": "complete"}}}`,
	} {
		if err := validateDrain(output, "family"); err == nil {
			t.Fatalf("%s must be refused", output)
		}
	}
	if err := validateDrain(drainOutput, "other"); err == nil {
		t.Fatal("another instance's evidence must be refused")
	}
}

var _ = ec2.StopInstancesInput{}
