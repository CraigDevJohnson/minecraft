package main

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
	"github.com/aws/smithy-go"
)

const (
	archiveFile = "20261016T031500Z.tar.gz"
	archiveKey  = "instances/family/crafty/" + archiveFile
	// restoredOutput is the adapter's answer to a restore, as scripts/minecraft_crafty.py prints it.
	restoredOutput = `{"instance": "family", "replaced": "/srv/minecraft/crafty/staging/archived-family-20261011T160100Z", "restored": "` + archiveKey + `"}`
	restoreCommand = remoteCommand + " restore family --key '" + archiveKey + "'"
)

func TestRestoreAStoppedHostRecordsTheRestoreBeforeEC2(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameStopped, 0)
	var recorded Session
	r.ec2.onStart = func() { recorded = r.session(t) }
	status, started, err := r.launcher.Restore(context.Background(), archiveFile)
	if err != nil || !started || status.State != Starting || status.Connection.Java == nil {
		t.Fatalf("status=%+v started=%v err=%v", status, started, err)
	}
	if r.ec2.starts != 1 || r.followUps != 1 {
		t.Fatalf("starts=%d followUps=%d", r.ec2.starts, r.followUps)
	}
	want := Session{RequestedAt: now, Phase: Restoring, RestoreKey: archiveKey}
	if recorded != want || r.session(t) != want {
		t.Fatalf("recorded before EC2 %+v, after %+v", recorded, r.session(t))
	}
	// A record that cannot be written starts nothing.
	r = newRig(ec2types.InstanceStateNameStopped, 0)
	r.ssm.putErr = errors.New("denied")
	if _, started, err := r.launcher.Restore(context.Background(), archiveFile); err == nil || started || r.ec2.starts != 0 || r.followUps != 0 {
		t.Fatalf("started=%v starts=%d followUps=%d err=%v", started, r.ec2.starts, r.followUps, err)
	}
	// A lost follow-up is reported, but the restore stands: it is reported failed after its deadline.
	r = newRig(ec2types.InstanceStateNameStopped, 0)
	r.followErr = errors.New("throttled")
	if status, started, err := r.launcher.Restore(context.Background(), archiveFile); err == nil || !started || status.State != Starting || r.session(t).Phase != Restoring {
		t.Fatalf("state=%s started=%v err=%v", status.State, started, err)
	}
}

func TestRestoreHandlesEC2LikeAStart(t *testing.T) {
	for _, tc := range []struct {
		name     string
		startErr error
		counted  bool
	}{
		{"another start moved the host on first", &smithy.GenericAPIError{Code: "IncorrectInstanceState"}, false},
		{"EC2 refused", &smithy.GenericAPIError{Code: "InsufficientInstanceCapacity"}, true},
		{"the answer was lost", errors.New("connection reset"), false},
	} {
		r := newRig(ec2types.InstanceStateNameStopped, 0)
		r.ec2.startErr = tc.startErr
		status, started, err := r.launcher.Restore(context.Background(), archiveFile)
		// The restore stands either way: its follow-up fails it once the host has stayed stopped.
		if (err != nil) != tc.counted || !started || status.State != Starting || r.followUps != 1 || r.session(t).Phase != Restoring {
			t.Fatalf("%s: state=%s started=%v err=%v followUps=%d", tc.name, status.State, started, err, r.followUps)
		}
	}
}

func TestRestoreOnARunningHostWhoseStartOrRestoreFailed(t *testing.T) {
	for _, tc := range []struct {
		name     string
		launched time.Duration
		session  Session
	}{
		{"a start failed", 5 * time.Minute, Session{RequestedAt: now.Add(-6 * time.Minute), LaunchTime: now.Add(-5 * time.Minute), Phase: Failed, DNS: "current"}},
		{"a restore failed", 10 * time.Minute, Session{RequestedAt: now.Add(-11 * time.Minute), LaunchTime: now.Add(-10 * time.Minute), Phase: Failed, RestoreKey: "instances/family/crafty/20261015T031500Z.tar.gz", Detail: "restore the archive: the archive does not match its recorded SHA-256"}},
		{"a restore ran out of time", 45 * time.Minute, Session{RequestedAt: now.Add(-46 * time.Minute), LaunchTime: now.Add(-45 * time.Minute), Phase: Restoring, RestoreKey: archiveKey, RestoreCommandID: commandID}},
	} {
		r := newRig(ec2types.InstanceStateNameRunning, tc.launched)
		r.setSession(t, tc.session)
		status, started, err := r.launcher.Restore(context.Background(), archiveFile)
		if err != nil || !started || status.State != Starting || r.ec2.starts != 0 || r.followUps != 1 {
			t.Fatalf("%s: state=%s started=%v starts=%d followUps=%d err=%v", tc.name, status.State, started, r.ec2.starts, r.followUps, err)
		}
		if session := r.session(t); session != (Session{RequestedAt: now, LaunchTime: now.Add(-tc.launched), Phase: Restoring, RestoreKey: archiveKey}) {
			t.Fatalf("%s: session %+v", tc.name, session)
		}
	}
}

func TestRestoreRefusesUnlessTheServerIsStoppedOrFailed(t *testing.T) {
	for _, tc := range []struct {
		name     string
		host     ec2types.InstanceStateName
		launched time.Duration
		gameUp   bool
		session  *Session
		idle     string
		want     string
	}{
		{name: "pending", host: ec2types.InstanceStateNamePending, launched: time.Minute, want: Starting},
		{name: "ready", host: ec2types.InstanceStateNameRunning, launched: time.Hour, gameUp: true, want: Ready},
		{name: "maintenance", host: ec2types.InstanceStateNameRunning, launched: time.Hour, want: Maintenance},
		{name: "EC2 stopping", host: ec2types.InstanceStateNameStopping, launched: time.Hour, want: Stopping},
		{name: "our start under way", host: ec2types.InstanceStateNameRunning, launched: time.Minute, session: &Session{RequestedAt: now.Add(-2 * time.Minute), LaunchTime: now.Add(-time.Minute), Phase: Starting}, want: Starting},
		{name: "our restore under way", host: ec2types.InstanceStateNameRunning, launched: 20 * time.Minute, session: &Session{RequestedAt: now.Add(-21 * time.Minute), LaunchTime: now.Add(-20 * time.Minute), Phase: Restoring, RestoreKey: archiveKey}, want: Starting},
		{name: "our stop under way", host: ec2types.InstanceStateNameRunning, launched: time.Hour, gameUp: true, session: &Session{RequestedAt: now.Add(-5 * time.Minute), LaunchTime: now.Add(-time.Hour), Phase: Stopping}, want: Stopping},
		{name: "the idle stop is draining", host: ec2types.InstanceStateNameRunning, launched: time.Hour, idle: `{"launch_time":"2026-10-11T15:00:00Z","drain_command_id":"x"}`, want: Stopping},
		{name: "a start just recorded that EC2 does not show yet", host: ec2types.InstanceStateNameStopped, session: &Session{RequestedAt: now.Add(-time.Second), Phase: Starting}, want: Starting},
		{name: "a restore just recorded that EC2 does not show yet", host: ec2types.InstanceStateNameStopped, session: &Session{RequestedAt: now.Add(-time.Second), Phase: Restoring, RestoreKey: archiveKey}, want: Starting},
		// The failed restore reads failed, but the game started by hand plays the world a restore would replace.
		{name: "a game started by hand after our restore failed", host: ec2types.InstanceStateNameRunning, launched: 10 * time.Minute, gameUp: true, session: &Session{RequestedAt: now.Add(-11 * time.Minute), LaunchTime: now.Add(-10 * time.Minute), Phase: Failed, RestoreKey: archiveKey}, want: Failed},
	} {
		r := newRig(tc.host, tc.launched)
		r.gameUp = tc.gameUp
		if tc.session != nil {
			r.setSession(t, *tc.session)
		}
		if tc.idle != "" {
			r.launcher.Config.IdleStopStateParameter = idleStateName
			r.ssm.params[idleStateName] = tc.idle
		}
		before := r.ssm.params[stateName]
		status, started, err := r.launcher.Restore(context.Background(), archiveFile)
		if err != nil || started || status.State != tc.want || r.ec2.starts != 0 || r.followUps != 0 || r.ssm.params[stateName] != before {
			t.Fatalf("%s: state=%s started=%v starts=%d followUps=%d err=%v", tc.name, status.State, started, r.ec2.starts, r.followUps, err)
		}
	}
}

func TestRestoreRefusesABadArchiveNameBeforeReadingAnything(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameStopped, 0)
	described := 0
	r.ec2.onDescribe = func() { described++ }
	for _, name := range []string{
		"",
		archiveKey,
		"s3://bucket/" + archiveKey,
		archiveFile + "\n",
		archiveFile + ".sha256",
		archiveFile + "'; reboot; '",
		"../" + archiveFile,
		"2026101T031500Z.tar.gz",
		"20261016T031500.tar.gz",
		"20261016t031500z.tar.gz",
	} {
		if _, started, err := r.launcher.Restore(context.Background(), name); !errors.Is(err, ErrBadRestoreRequest) || started {
			t.Fatalf("%q: started=%v err=%v", name, started, err)
		}
	}
	if described != 0 || r.ec2.starts != 0 || r.followUps != 0 || r.session(t) != (Session{}) {
		t.Fatalf("a bad name must neither read nor change anything: described=%d starts=%d followUps=%d", described, r.ec2.starts, r.followUps)
	}
	r.launcher.Config.HostID = ""
	if _, _, err := r.launcher.Restore(context.Background(), archiveFile); !errors.Is(err, ErrNoServer) {
		t.Fatalf("err=%v", err)
	}
}

func TestARestoreReadsAsStarting(t *testing.T) {
	for _, tc := range []struct {
		name     string
		host     ec2types.InstanceStateName
		launched time.Duration
		gameUp   bool
		session  Session
		want     string
	}{
		// A start would have failed by now; a restore has longer.
		{"pending past a start's deadline", ec2types.InstanceStateNamePending, 19 * time.Minute, false, Session{RequestedAt: now.Add(-20 * time.Minute), Phase: Restoring, RestoreKey: archiveKey}, Starting},
		{"pending past its own deadline", ec2types.InstanceStateNamePending, 45 * time.Minute, false, Session{RequestedAt: now.Add(-46 * time.Minute), Phase: Restoring, RestoreKey: archiveKey}, Failed},
		// A game that answers before the archive is confirmed was started by hand on the old world: never ready.
		{"a game started by hand answers", ec2types.InstanceStateNameRunning, 10 * time.Minute, true, Session{RequestedAt: now.Add(-11 * time.Minute), LaunchTime: now.Add(-10 * time.Minute), Phase: Restoring, RestoreKey: archiveKey}, Starting},
		{"a game started by hand outlasts the restore", ec2types.InstanceStateNameRunning, 45 * time.Minute, true, Session{RequestedAt: now.Add(-46 * time.Minute), LaunchTime: now.Add(-45 * time.Minute), Phase: Restoring, RestoreKey: archiveKey}, Failed},
		{"the adapter refused under a game started by hand", ec2types.InstanceStateNameRunning, 10 * time.Minute, true, Session{RequestedAt: now.Add(-11 * time.Minute), LaunchTime: now.Add(-10 * time.Minute), Phase: Failed, RestoreKey: archiveKey, Detail: "restore the archive: stop the server before restoring it"}, Failed},
		// The start that follows a confirmed restore carries no archive, so its game reads ready as any start's.
		{"the game the restore's start began answers", ec2types.InstanceStateNameRunning, 12 * time.Minute, true, Session{RequestedAt: now.Add(-time.Minute), LaunchTime: now.Add(-12 * time.Minute), Phase: Starting}, Ready},
		// A failed start of ours carries no archive: its game reads ready once it answers.
		{"a failed start's game answers", ec2types.InstanceStateNameRunning, 10 * time.Minute, true, Session{RequestedAt: now.Add(-11 * time.Minute), LaunchTime: now.Add(-10 * time.Minute), Phase: Failed}, Ready},
		// A restore that failed before EC2 ran the host never reached a run: one started otherwise soon after is not its.
		{"a restore that never reached the host", ec2types.InstanceStateNameRunning, 5 * time.Minute, true, Session{RequestedAt: now.Add(-12 * time.Minute), Phase: Failed, RestoreKey: archiveKey, Detail: "EC2 did not start the host"}, Ready},
		// A failed restore of an earlier run of the host no longer decides.
		{"a failed restore of an earlier run", ec2types.InstanceStateNameRunning, 10 * time.Minute, true, Session{RequestedAt: now.Add(-3 * time.Hour), LaunchTime: now.Add(-3 * time.Hour), Phase: Failed, RestoreKey: archiveKey}, Ready},
	} {
		r := newRig(tc.host, tc.launched)
		r.gameUp = tc.gameUp
		r.setSession(t, tc.session)
		if status, err := r.launcher.Status(context.Background()); err != nil || status.State != tc.want {
			t.Fatalf("%s: state=%s err=%v", tc.name, status.State, err)
		}
	}
}

func TestStopStartAndAllowlistWaitForARestore(t *testing.T) {
	restore := Session{RequestedAt: now.Add(-4 * time.Minute), Phase: Restoring, RestoreKey: archiveKey}
	r := newRig(ec2types.InstanceStateNameRunning, 3*time.Minute)
	r.setSession(t, restore)
	for _, force := range []bool{false, true} {
		if status, stopped, err := r.launcher.Stop(context.Background(), force); !errors.Is(err, ErrStarting) || stopped || status.State != Starting {
			t.Fatalf("force=%v: state=%s stopped=%v err=%v", force, status.State, stopped, err)
		}
	}
	if status, started, err := r.launcher.Start(context.Background()); err != nil || started || status.State != Starting || r.followUps != 0 {
		t.Fatalf("state=%s started=%v err=%v followUps=%d", status.State, started, err, r.followUps)
	}
	var notReady ErrServerNotReady
	if _, err := r.launcher.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"}); !errors.As(err, &notReady) || notReady.State != Starting {
		t.Fatalf("err=%v", err)
	}
	if r.session(t) != restore || len(r.ssm.commands) != 0 || r.followUpStops != 0 || r.ec2.stops != 0 {
		t.Fatalf("session=%+v commands=%v followUpStops=%d stops=%d", r.session(t), r.ssm.commands, r.followUpStops, r.ec2.stops)
	}

	// EC2's description lags the restore's start by seconds: a start then must not replace the restore.
	r = newRig(ec2types.InstanceStateNameStopped, 0)
	restore = Session{RequestedAt: now.Add(-time.Second), Phase: Restoring, RestoreKey: archiveKey}
	r.setSession(t, restore)
	if status, started, err := r.launcher.Start(context.Background()); err != nil || started || status.State != Starting || r.ec2.starts != 0 || r.session(t) != restore {
		t.Fatalf("state=%s started=%v err=%v starts=%d session=%+v", status.State, started, err, r.ec2.starts, r.session(t))
	}
	// A host still stopped after that was not started by the restore: a start goes ahead.
	r.setSession(t, Session{RequestedAt: now.Add(-stoppedGrace), Phase: Restoring, RestoreKey: archiveKey})
	if status, started, err := r.launcher.Start(context.Background()); err != nil || !started || status.State != Starting || r.session(t).Phase != Starting {
		t.Fatalf("state=%s started=%v err=%v", status.State, started, err)
	}
}

// restoreRig has a restore of this function recorded a minute ago against a stopped host, which then starts and
// comes online, with the host's adapter answering the restore.
func restoreRig(t *testing.T) *rig {
	r := finishRig(t)
	r.setSession(t, Session{RequestedAt: now.Add(-time.Minute), Phase: Restoring, RestoreKey: archiveKey})
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}, success(restoredOutput)}
	return r
}

func TestFinishRestorePutsTheArchiveInPlaceThenStartsTheGame(t *testing.T) {
	r := restoreRig(t)
	var during Session
	r.ssm.onInvocation = func() { during = r.session(t) }
	if err := r.launcher.FinishStart(context.Background()); err != nil {
		t.Fatal(err)
	}
	if strings.Join(r.pointedAt, ",") != address {
		t.Fatalf("DNS pointed at %v", r.pointedAt)
	}
	if strings.Join(r.ssm.commands, "|") != "set -eu\n"+restoreCommand {
		t.Fatalf("commands %q", r.ssm.commands)
	}
	if strings.Join(r.ssm.limits, ",") != "1800" {
		t.Fatalf("execution limits %v", r.ssm.limits)
	}
	// The restore's command is recorded while it runs, so a fresh run can follow it.
	launch := now.Add(30 * time.Second)
	if want := (Session{RequestedAt: now.Add(-time.Minute), LaunchTime: launch, Phase: Restoring, DNS: "current", RestoreKey: archiveKey, RestoreCommandID: commandID, RestoreSentAt: now}); during != want {
		t.Fatalf("while the restore ran: %+v", during)
	}
	// Then a fresh start, with a start's whole time, replaces the restore and its follow-up starts the game.
	if session := r.session(t); session != (Session{RequestedAt: now, LaunchTime: launch, Phase: Starting, DNS: "current"}) || r.followUps != 1 {
		t.Fatalf("session %+v followUps=%d", session, r.followUps)
	}
	if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Starting {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
	r.ssm.onInvocation = nil
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{success(readyOutput)}
	if err := r.launcher.FinishStart(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(r.ssm.commands) != 2 || r.ssm.commands[1] != "set -eu\n"+remoteCommand+" start family" {
		t.Fatalf("commands %q", r.ssm.commands)
	}
	if session := r.session(t); session.Phase != Ready || !session.LaunchTime.Equal(launch) {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishRestoreOnTheRunningHost(t *testing.T) {
	r := restoreRig(t)
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}
	r.ec2.launch = now.Add(-5 * time.Minute)
	r.setSession(t, Session{RequestedAt: now.Add(-time.Minute), LaunchTime: now.Add(-5 * time.Minute), Phase: Restoring, RestoreKey: archiveKey})
	if err := r.launcher.FinishStart(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(r.ssm.commands) != 1 || r.followUps != 1 {
		t.Fatalf("commands=%q followUps=%d", r.ssm.commands, r.followUps)
	}
	if session := r.session(t); session != (Session{RequestedAt: now, LaunchTime: now.Add(-5 * time.Minute), Phase: Starting, DNS: "current"}) {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishRestoreCarriesOnWhenDNSFails(t *testing.T) {
	r := restoreRig(t)
	r.dnsErr = errors.New("token missing")
	if err := r.launcher.FinishStart(context.Background()); err != nil {
		t.Fatalf("the start that follows reports DNS: %v", err)
	}
	if len(r.ssm.commands) != 1 || r.followUps != 1 {
		t.Fatalf("commands=%q followUps=%d", r.ssm.commands, r.followUps)
	}
	if session := r.session(t); session.Phase != Starting || session.DNS != "failed" {
		t.Fatalf("session %+v", session)
	}
	if status, _ := r.launcher.Status(context.Background()); status.State != Starting || status.Connection.Java != nil {
		t.Fatalf("status %+v", status)
	}
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{success(readyOutput)}
	if err := r.launcher.FinishStart(context.Background()); err == nil || !strings.Contains(err.Error(), "DNS") {
		t.Fatalf("err=%v", err)
	}
}

func TestFinishRestoreHandsOffWhenItsRunEndsFirst(t *testing.T) {
	r := restoreRig(t)
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if err := r.launcher.FinishStart(ctx); err != nil || r.followUps != 1 || len(r.ssm.commands) != 1 {
		t.Fatalf("err=%v followUps=%d commands=%d", err, r.followUps, len(r.ssm.commands))
	}
	session := r.session(t)
	if session.Phase != Restoring || session.RestoreCommandID != commandID || !session.RestoreSentAt.Equal(now) || !session.LaunchTime.Equal(now.Add(30*time.Second)) {
		t.Fatalf("session %+v", session)
	}
	// The next run follows the same restore instead of sending another.
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{success(restoredOutput)}
	if err := r.launcher.FinishStart(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(r.ssm.commands) != 1 || r.followUps != 2 || r.session(t).Phase != Starting {
		t.Fatalf("commands=%d followUps=%d session=%+v", len(r.ssm.commands), r.followUps, r.session(t))
	}
	// A run that ends before the host is even online hands off too: a restore has more time than one run.
	r = restoreRig(t)
	r.ssm.online = []bool{false}
	ctx, cancel = context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := r.launcher.FinishStart(ctx); err != nil || r.followUps != 1 || len(r.ssm.commands) != 0 || r.session(t).Phase != Restoring {
		t.Fatalf("err=%v followUps=%d commands=%v session=%+v", err, r.followUps, r.ssm.commands, r.session(t))
	}
	// A hand-off that cannot be made fails the restore, so the alarm sees it.
	r = restoreRig(t)
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}}
	r.followErr = errors.New("throttled")
	ctx, cancel = context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	if err := r.launcher.FinishStart(ctx); err == nil || r.session(t).Phase != Failed || r.session(t).Detail != "hand off the rest of the restore: throttled" {
		t.Fatalf("err=%v session=%+v", err, r.session(t))
	}
}

func TestFinishRestoreFailsWithTheAdaptersReason(t *testing.T) {
	failed := func(status ssmtypes.CommandInvocationStatus, code int32, output string) ssm.GetCommandInvocationOutput {
		return ssm.GetCommandInvocationOutput{Status: status, ResponseCode: code, StandardOutputContent: aws.String(output)}
	}
	for _, tc := range []struct {
		answer ssm.GetCommandInvocationOutput
		detail string
	}{
		{failed(ssmtypes.CommandInvocationStatusFailed, 1, `{"error": "the archive does not match its recorded SHA-256"}`+"\n"), "restore the archive: the archive does not match its recorded SHA-256"},
		{failed(ssmtypes.CommandInvocationStatusFailed, 1, `{"error": "stop the server before restoring it"}`+"\n"), "restore the archive: stop the server before restoring it"},
		{failed(ssmtypes.CommandInvocationStatusFailed, 1, ""), "the restore ended with status Failed"},
		{failed(ssmtypes.CommandInvocationStatusTimedOut, -1, ""), "the restore ended with status TimedOut"},
		{failed(ssmtypes.CommandInvocationStatusSuccess, 1, `{"error": "the archive could not be unpacked"}`), "restore the archive: the archive could not be unpacked"},
		{failed(ssmtypes.CommandInvocationStatusSuccess, 2, ""), "the host's adapter refused the restore"},
		{success(strings.Replace(restoredOutput, `"instance": "family"`, `"instance": "other"`, 1)), "the host's adapter did not confirm the restore"},
		{success(strings.Replace(restoredOutput, archiveFile, "20261015T031500Z.tar.gz", 1)), "the host's adapter did not confirm the restore"},
		{success(restoredOutput + "\n" + restoredOutput), "the host's adapter did not confirm the restore"},
		{success("not json"), "the host's adapter did not confirm the restore"},
	} {
		r := restoreRig(t)
		r.ssm.invocation = []ssm.GetCommandInvocationOutput{tc.answer}
		if err := r.launcher.FinishStart(context.Background()); err == nil || err.Error() != tc.detail {
			t.Fatalf("%s: err=%v", tc.detail, err)
		}
		session := r.session(t)
		if session.Phase != Failed || session.Detail != tc.detail || session.RestoreKey != archiveKey || session.LaunchTime.IsZero() || r.followUps != 0 {
			t.Fatalf("%s: session %+v followUps=%d", tc.detail, session, r.followUps)
		}
	}
	// The host keeps running without its game: the restore may be asked again.
	r := restoreRig(t)
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusFailed}}
	_ = r.launcher.FinishStart(context.Background())
	if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Failed {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
	if _, started, err := r.launcher.Restore(context.Background(), archiveFile); err != nil || !started {
		t.Fatalf("started=%v err=%v", started, err)
	}
}

func TestFinishRestoreFailsWhenItsCommandNeverAppears(t *testing.T) {
	sent := func(at time.Time) Session {
		return Session{RequestedAt: now.Add(-5 * time.Minute), LaunchTime: now.Add(30 * time.Second), Phase: Restoring, DNS: "current", RestoreKey: archiveKey, RestoreCommandID: commandID, RestoreSentAt: at}
	}
	r := restoreRig(t)
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}
	r.ssm.invocation = nil
	r.setSession(t, sent(now.Add(-invisibleGrace-time.Second)))
	if err := r.launcher.FinishStart(context.Background()); err == nil || !strings.Contains(err.Error(), "never became visible") || len(r.ssm.commands) != 0 {
		t.Fatalf("err=%v commands=%v", err, r.ssm.commands)
	}
	if session := r.session(t); session.Phase != Failed {
		t.Fatalf("session %+v", session)
	}
	// Just after it was sent, the run keeps looking, and hands off if its time ends first.
	r = restoreRig(t)
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}
	r.ssm.invocation = nil
	r.setSession(t, sent(now))
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := r.launcher.FinishStart(ctx); err != nil || r.followUps != 1 || r.session(t) != sent(now) {
		t.Fatalf("err=%v followUps=%d session=%+v", err, r.followUps, r.session(t))
	}
}

func TestFinishRestoreFailsWhenEC2NeverStartedTheHost(t *testing.T) {
	r := restoreRig(t)
	r.setSession(t, Session{RequestedAt: now.Add(-3 * time.Minute), Phase: Restoring, RestoreKey: archiveKey})
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameStopped}
	if err := r.launcher.FinishStart(context.Background()); err == nil || !strings.Contains(err.Error(), "did not start") {
		t.Fatalf("err=%v", err)
	}
	if session := r.session(t); session.Phase != Failed || session.Detail != "EC2 did not start the host" || r.followUps != 0 || len(r.ssm.commands) != 0 {
		t.Fatalf("session %+v followUps=%d commands=%v", session, r.followUps, r.ssm.commands)
	}
}

func TestFinishRestoreLeavesALaterRequestAlone(t *testing.T) {
	// A later request recorded before the restore is sent: nothing is sent.
	r := restoreRig(t)
	later := Session{RequestedAt: now, Phase: Starting}
	r.launcher.PointDNS = func(context.Context, string) error { r.setSession(t, later); return nil }
	if err := r.launcher.FinishStart(context.Background()); err == nil || len(r.ssm.commands) != 0 || r.followUps != 0 {
		t.Fatalf("err=%v commands=%v followUps=%d", err, r.ssm.commands, r.followUps)
	}
	if session := r.session(t); session != later {
		t.Fatalf("session %+v", session)
	}
	// One recorded while the restore runs is never replaced by the restore's start.
	r = restoreRig(t)
	later = Session{RequestedAt: now, LaunchTime: now.Add(30 * time.Second), Phase: Restoring, RestoreKey: "instances/family/crafty/20261015T031500Z.tar.gz"}
	r.ssm.onInvocation = func() { r.setSession(t, later) }
	if err := r.launcher.FinishStart(context.Background()); err == nil || r.followUps != 0 {
		t.Fatalf("err=%v followUps=%d", err, r.followUps)
	}
	if session := r.session(t); session != later {
		t.Fatalf("session %+v", session)
	}
	// A repeated run that finds its request finished does nothing more.
	r = restoreRig(t)
	finished := Session{RequestedAt: now.Add(-time.Minute), LaunchTime: now.Add(30 * time.Second), Phase: Ready, DNS: "current"}
	r.ec2.onDescribe = func() { r.setSession(t, finished) }
	if err := r.launcher.FinishStart(context.Background()); err != nil || len(r.ssm.commands) != 0 || len(r.pointedAt) != 0 || r.session(t) != finished {
		t.Fatalf("err=%v commands=%v pointed=%v session=%+v", err, r.ssm.commands, r.pointedAt, r.session(t))
	}
}

func TestFinishRestoreNeedsTimeToRestore(t *testing.T) {
	left := func(r *rig, remaining time.Duration) {
		r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}
		r.setSession(t, Session{RequestedAt: now.Add(remaining - restoreDeadline + finishMargin), LaunchTime: now.Add(30 * time.Second), Phase: Restoring, RestoreKey: archiveKey})
	}
	r := restoreRig(t)
	left(r, 100*time.Second)
	if err := r.launcher.FinishStart(context.Background()); err == nil || err.Error() != "too little time is left to restore the archive" || len(r.ssm.commands) != 0 {
		t.Fatalf("err=%v commands=%v", err, r.ssm.commands)
	}
	if session := r.session(t); session.Phase != Failed || session.Detail != "too little time is left to restore the archive" {
		t.Fatalf("session %+v", session)
	}
	// With a little more left, the adapter gets what remains, less SSM's delivery and a margin.
	r = restoreRig(t)
	left(r, 1000*time.Second)
	if err := r.launcher.FinishStart(context.Background()); err != nil || strings.Join(r.ssm.limits, ",") != "955" {
		t.Fatalf("err=%v limits=%v", err, r.ssm.limits)
	}
}

func TestFinishRestoreGivesUpAtItsDeadline(t *testing.T) {
	r := restoreRig(t)
	r.launcher.Now = func() time.Time { return now.Add(restoreDeadline) }
	if err := r.launcher.FinishStart(context.Background()); err == nil || len(r.ssm.commands) != 0 || len(r.pointedAt) != 0 || r.followUps != 0 {
		t.Fatalf("err=%v commands=%v pointed=%v followUps=%d", err, r.ssm.commands, r.pointedAt, r.followUps)
	}
	if session := r.session(t); session.Phase != Failed || session.Detail != "the restore passed its deadline before its follow-up ran" {
		t.Fatalf("session %+v", session)
	}
	// A run whose time ends as the restore's deadline passes fails rather than handing off.
	r = restoreRig(t)
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}}
	var late atomic.Bool
	r.launcher.Now = func() time.Time {
		if late.Load() {
			return now.Add(restoreDeadline)
		}
		return now
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	time.AfterFunc(10*time.Millisecond, func() { late.Store(true) })
	if err := r.launcher.FinishStart(ctx); err == nil || r.followUps != 0 || r.session(t).Phase != Failed {
		t.Fatalf("err=%v followUps=%d session=%+v", err, r.followUps, r.session(t))
	}
}

func TestFinishRestoreRecordsAStartItCouldNotHandOff(t *testing.T) {
	r := restoreRig(t)
	r.followErr = errors.New("throttled")
	if err := r.launcher.FinishStart(context.Background()); err == nil {
		t.Fatal("a game start that cannot be handed off must fail the run so its alarm sees it")
	}
	if session := r.session(t); session.Phase != Failed || !session.RequestedAt.Equal(now) || session.Detail != "hand off the rest of the start: throttled" || session.LaunchTime.IsZero() {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishRestoreTiesItsFailureToTheRunItReached(t *testing.T) {
	// The host runs, then the record cannot be read: the failed restore still names this run, so a game started
	// by hand on it reads failed, not ready.
	r := restoreRig(t)
	reads := 0
	r.ssm.onGet = func() error {
		if reads++; reads == 2 {
			return errors.New("throttled")
		}
		return nil
	}
	if err := r.launcher.FinishStart(context.Background()); err == nil || !strings.Contains(err.Error(), "throttled") {
		t.Fatalf("err=%v", err)
	}
	r.ssm.onGet = nil
	if session := r.session(t); session.Phase != Failed || !session.LaunchTime.Equal(now.Add(30*time.Second)) || session.RestoreKey != archiveKey || len(r.ssm.commands) != 0 {
		t.Fatalf("session %+v commands=%v", session, r.ssm.commands)
	}
	r.gameUp = true
	if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Failed {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
}

func TestFinishRestoreNeverTiesItsFailureToAnotherRun(t *testing.T) {
	// The restore was recorded on an earlier run, and the host was stopped and started again before its follow-up
	// looked: its failure keeps the earlier run, so a game started from the workstation on this one reads ready.
	r := restoreRig(t)
	earlier := now.Add(-20 * time.Minute)
	r.setSession(t, Session{RequestedAt: now.Add(-time.Minute), LaunchTime: earlier, Phase: Restoring, RestoreKey: archiveKey})
	if err := r.launcher.FinishStart(context.Background()); err == nil {
		t.Fatal("a restore of another run must fail")
	}
	if session := r.session(t); session.Phase != Failed || !session.LaunchTime.Equal(earlier) || len(r.ssm.commands) != 0 {
		t.Fatalf("session %+v commands=%v", session, r.ssm.commands)
	}
	r.gameUp = true
	if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Ready {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
}

func TestFinishRestoreSendsOnlyAnArchiveOfThisServer(t *testing.T) {
	for _, key := range []string{
		"",
		"instances/other/crafty/" + archiveFile,
		archiveKey + "'; reboot; '",
		"instances/family/crafty/../../" + archiveFile,
		archiveFile,
	} {
		r := restoreRig(t)
		r.setSession(t, Session{RequestedAt: now.Add(-time.Minute), Phase: Restoring, RestoreKey: key})
		if err := r.launcher.FinishStart(context.Background()); err == nil || len(r.ssm.commands) != 0 {
			t.Fatalf("%q: err=%v commands=%q", key, err, r.ssm.commands)
		}
		if session := r.session(t); session.Phase != Failed {
			t.Fatalf("%q: session %+v", key, session)
		}
	}
}
