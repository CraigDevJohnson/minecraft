package main

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
	"github.com/aws/smithy-go"
)

const (
	hostID        = "i-0123456789abcdef0"
	stateName     = "/minecraft-test/launcher/state"
	idleStateName = "/minecraft-test/idle-stop/state"
	address       = "203.0.113.10"
	commandID     = "11111111-2222-3333-4444-555555555555"
)

var now = time.Date(2026, 10, 11, 16, 0, 0, 0, time.UTC)

func init() { pollEvery = time.Millisecond }

type fakeEC2 struct {
	mu       sync.Mutex
	states   []ec2types.InstanceStateName // successive DescribeInstances answers; the last repeats
	launch   time.Time
	ip       string
	err      error
	startErr error
	starts   int
}

func (f *fakeEC2) DescribeInstances(context.Context, *ec2.DescribeInstancesInput, ...func(*ec2.Options)) (*ec2.DescribeInstancesOutput, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.err != nil {
		return nil, f.err
	}
	state := f.states[0]
	if len(f.states) > 1 {
		f.states = f.states[1:]
	}
	instance := ec2types.Instance{InstanceId: aws.String(hostID), State: &ec2types.InstanceState{Name: state}}
	if state == ec2types.InstanceStateNameRunning || state == ec2types.InstanceStateNamePending {
		instance.LaunchTime = aws.Time(f.launch)
	}
	if state == ec2types.InstanceStateNameRunning {
		instance.PublicIpAddress = aws.String(f.ip)
	}
	return &ec2.DescribeInstancesOutput{Reservations: []ec2types.Reservation{{Instances: []ec2types.Instance{instance}}}}, nil
}

func (f *fakeEC2) StartInstances(_ context.Context, input *ec2.StartInstancesInput, _ ...func(*ec2.Options)) (*ec2.StartInstancesOutput, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(input.InstanceIds) != 1 || input.InstanceIds[0] != hostID {
		panic("started another host")
	}
	f.starts++
	return &ec2.StartInstancesOutput{}, f.startErr
}

type fakeSSM struct {
	mu         sync.Mutex
	params     map[string]string
	getErr     error
	putErr     error
	online     []bool // successive SSM ping answers; the last repeats
	commands   []string
	invocation []ssm.GetCommandInvocationOutput // successive answers; the last repeats
}

func (f *fakeSSM) GetParameter(_ context.Context, input *ssm.GetParameterInput, _ ...func(*ssm.Options)) (*ssm.GetParameterOutput, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.getErr != nil {
		return nil, f.getErr
	}
	value, ok := f.params[aws.ToString(input.Name)]
	if !ok {
		return nil, &ssmtypes.ParameterNotFound{}
	}
	return &ssm.GetParameterOutput{Parameter: &ssmtypes.Parameter{Value: aws.String(value)}}, nil
}

func (f *fakeSSM) PutParameter(_ context.Context, input *ssm.PutParameterInput, _ ...func(*ssm.Options)) (*ssm.PutParameterOutput, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.putErr != nil {
		return nil, f.putErr
	}
	if aws.ToString(input.Name) != stateName {
		panic("wrote a parameter other than its own state")
	}
	f.params[stateName] = aws.ToString(input.Value)
	return &ssm.PutParameterOutput{}, nil
}

func (f *fakeSSM) SendCommand(_ context.Context, input *ssm.SendCommandInput, _ ...func(*ssm.Options)) (*ssm.SendCommandOutput, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if aws.ToString(input.DocumentName) != "AWS-RunShellScript" || len(input.InstanceIds) != 1 || input.InstanceIds[0] != hostID {
		panic("sent a command to another host or document")
	}
	f.commands = append(f.commands, input.Parameters["commands"]...)
	return &ssm.SendCommandOutput{Command: &ssmtypes.Command{CommandId: aws.String(commandID)}}, nil
}

func (f *fakeSSM) GetCommandInvocation(context.Context, *ssm.GetCommandInvocationInput, ...func(*ssm.Options)) (*ssm.GetCommandInvocationOutput, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.invocation) == 0 {
		return nil, &ssmtypes.InvocationDoesNotExist{}
	}
	answer := f.invocation[0]
	if len(f.invocation) > 1 {
		f.invocation = f.invocation[1:]
	}
	return &answer, nil
}

func (f *fakeSSM) DescribeInstanceInformation(context.Context, *ssm.DescribeInstanceInformationInput, ...func(*ssm.Options)) (*ssm.DescribeInstanceInformationOutput, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	online := len(f.online) > 0 && f.online[0]
	if len(f.online) > 1 {
		f.online = f.online[1:]
	}
	if !online {
		return &ssm.DescribeInstanceInformationOutput{}, nil
	}
	return &ssm.DescribeInstanceInformationOutput{InstanceInformationList: []ssmtypes.InstanceInformation{{InstanceId: aws.String(hostID), PingStatus: ssmtypes.PingStatusOnline}}}, nil
}

type rig struct {
	ec2       *fakeEC2
	ssm       *fakeSSM
	launcher  *Launcher
	gameUp    bool
	pinged    []string
	dnsErr    error
	pointedAt []string
	followUps int
	followErr error
}

func newRig(state ec2types.InstanceStateName, launchedAgo time.Duration) *rig {
	r := &rig{
		ec2: &fakeEC2{states: []ec2types.InstanceStateName{state}, launch: now.Add(-launchedAgo), ip: address},
		ssm: &fakeSSM{params: map[string]string{stateName: "{}"}},
	}
	r.launcher = &Launcher{
		Config: Config{HostID: hostID, Instance: "family", ServerName: "Family server", PublicHostname: "mc.example.com", BedrockPort: 19132, StateParameter: stateName},
		EC2:    r.ec2,
		SSM:    r.ssm,
		Ping: func(_ context.Context, target string) (int, error) {
			r.pinged = append(r.pinged, target)
			if r.gameUp {
				return 0, nil
			}
			return 0, errors.New("no answer")
		},
		PointDNS: func(_ context.Context, target string) error {
			r.pointedAt = append(r.pointedAt, target)
			return r.dnsErr
		},
		FollowUp: func(context.Context) error { r.followUps++; return r.followErr },
		Now:      func() time.Time { return now },
	}
	return r
}

func (r *rig) setSession(t *testing.T, session Session) {
	t.Helper()
	encoded, err := json.Marshal(session)
	if err != nil {
		t.Fatal(err)
	}
	r.ssm.params[stateName] = string(encoded)
}

func (r *rig) session(t *testing.T) Session {
	t.Helper()
	var session Session
	if err := json.Unmarshal([]byte(r.ssm.params[stateName]), &session); err != nil {
		t.Fatal(err)
	}
	return session
}

func TestStatusFollowsTheHost(t *testing.T) {
	for _, tc := range []struct {
		host ec2types.InstanceStateName
		want string
	}{
		{ec2types.InstanceStateNameStopped, Stopped},
		{ec2types.InstanceStateNamePending, Starting},
		{ec2types.InstanceStateNameStopping, Stopping},
		{ec2types.InstanceStateNameShuttingDown, Stopping},
		{ec2types.InstanceStateNameTerminated, Unknown},
	} {
		r := newRig(tc.host, time.Hour)
		status, err := r.launcher.Status(context.Background())
		if err != nil || status.State != tc.want {
			t.Fatalf("%s: state=%s err=%v", tc.host, status.State, err)
		}
		if len(r.pinged) != 0 {
			t.Fatalf("%s: pinged a host that is not running", tc.host)
		}
	}
}

func TestReadyOnlyWhenTheGameAnswers(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.gameUp = true
	status, err := r.launcher.Status(context.Background())
	if err != nil || status.State != Ready {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
	if strings.Join(r.pinged, ",") != address+":25565" {
		t.Fatalf("pinged %v", r.pinged)
	}
	want := Connection{Java: &Endpoint{Address: "mc.example.com"}, Bedrock: &Endpoint{Address: "mc.example.com", Port: 19132}}
	got, _ := json.Marshal(status.Connection)
	expected, _ := json.Marshal(want)
	if string(got) != string(expected) || status.Name != "Family server" {
		t.Fatalf("status %+v", status)
	}
}

func TestRunningHostWithoutGame(t *testing.T) {
	cases := []struct {
		name     string
		launched time.Duration
		session  *Session
		idle     string
		want     string
	}{
		{name: "someone else's start is recent", launched: 2 * time.Minute, want: Starting},
		{name: "someone else's host runs on without a game", launched: time.Hour, want: Maintenance},
		{name: "our start is under way", launched: 3 * time.Minute, session: &Session{RequestedAt: now.Add(-4 * time.Minute), Phase: Starting}, want: Starting},
		{name: "our start ran past its deadline", launched: 20 * time.Minute, session: &Session{RequestedAt: now.Add(-21 * time.Minute), LaunchTime: now.Add(-20 * time.Minute), Phase: Starting}, want: Failed},
		{name: "our start failed", launched: 5 * time.Minute, session: &Session{RequestedAt: now.Add(-6 * time.Minute), LaunchTime: now.Add(-5 * time.Minute), Phase: Failed}, want: Failed},
		{name: "our started game was stopped", launched: time.Hour, session: &Session{RequestedAt: now.Add(-61 * time.Minute), LaunchTime: now.Add(-time.Hour), Phase: Ready}, want: Maintenance},
		{name: "an old failure does not follow a later run", launched: time.Hour, session: &Session{RequestedAt: now.Add(-3 * time.Hour), Phase: Failed}, want: Maintenance},
		{name: "a failure tied to an earlier run does not follow", launched: 2 * time.Hour, session: &Session{RequestedAt: now.Add(-5 * time.Hour), LaunchTime: now.Add(-5 * time.Hour), Phase: Failed}, want: Maintenance},
		{name: "the idle stop is draining", launched: time.Hour, idle: `{"launch_time":"2026-10-11T15:00:00Z","drain_command_id":"x"}`, want: Stopping},
		{name: "the idle stop requested the EC2 stop", launched: time.Hour, idle: `{"launch_time":"2026-10-11T15:00:00Z","stop_requested":true}`, want: Stopping},
		{name: "an idle-stop drain of an earlier run", launched: time.Hour, idle: `{"launch_time":"2026-10-11T09:00:00Z","drain_command_id":"x"}`, want: Maintenance},
	}
	for _, tc := range cases {
		r := newRig(ec2types.InstanceStateNameRunning, tc.launched)
		if tc.session != nil {
			r.setSession(t, *tc.session)
		}
		if tc.idle != "" {
			r.launcher.Config.IdleStopStateParameter = idleStateName
			r.ssm.params[idleStateName] = tc.idle
		}
		status, err := r.launcher.Status(context.Background())
		if err != nil || status.State != tc.want {
			t.Fatalf("%s: state=%s err=%v", tc.name, status.State, err)
		}
		if wantPings := map[bool]int{true: 0, false: 2}[tc.idle != "" && tc.want == Stopping]; len(r.pinged) != wantPings {
			t.Fatalf("%s: pinged %d times, want %d", tc.name, len(r.pinged), wantPings)
		}
	}
}

func TestADrainIsStoppingWhileTheGameStillAnswers(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.gameUp = true
	r.launcher.Config.IdleStopStateParameter = idleStateName
	r.ssm.params[idleStateName] = `{"launch_time":"2026-10-11T15:00:00Z","drain_command_id":"x"}`
	status, err := r.launcher.Status(context.Background())
	if err != nil || status.State != Stopping {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
}

func TestUnknownKeepsAFailedDNSHidden(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, 5*time.Minute)
	r.setSession(t, Session{RequestedAt: now.Add(-6 * time.Minute), LaunchTime: now.Add(-5 * time.Minute), Phase: Ready, DNS: "failed"})
	r.launcher.Config.IdleStopStateParameter = idleStateName
	r.ssm.params[idleStateName] = "not json"
	status, err := r.launcher.Status(context.Background())
	if err == nil || status.State != Unknown || status.Connection.Java != nil || status.Connection.Bedrock != nil {
		t.Fatalf("status %+v err=%v", status, err)
	}
}

func TestMissingIdleStopStateIsNotADrain(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.launcher.Config.IdleStopStateParameter = idleStateName
	status, err := r.launcher.Status(context.Background())
	if err != nil || status.State != Maintenance {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
}

func TestUnreadableAWSIsUnknown(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.ec2.err = errors.New("throttled")
	status, err := r.launcher.Status(context.Background())
	if err == nil || status.State != Unknown {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
	r = newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.ssm.getErr = errors.New("throttled")
	if status, err := r.launcher.Status(context.Background()); err == nil || status.State != Unknown {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
}

func TestFailedDNSHidesTheAddress(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, 5*time.Minute)
	r.gameUp = true
	r.setSession(t, Session{RequestedAt: now.Add(-6 * time.Minute), LaunchTime: now.Add(-5 * time.Minute), Phase: Ready, DNS: "failed"})
	status, _ := r.launcher.Status(context.Background())
	if status.State != Ready || status.Connection.Java != nil || status.Connection.Bedrock != nil {
		t.Fatalf("status %+v", status)
	}
	// The next stopped host shows the address again: the hostname is fixed.
	r = newRig(ec2types.InstanceStateNameStopped, 0)
	r.setSession(t, Session{RequestedAt: now.Add(-6 * time.Minute), LaunchTime: now.Add(-5 * time.Minute), Phase: Ready, DNS: "failed"})
	if status, _ := r.launcher.Status(context.Background()); status.Connection.Java == nil {
		t.Fatalf("status %+v", status)
	}
}

func TestNoHostIsNoServer(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameStopped, 0)
	r.launcher.Config.HostID = ""
	if _, err := r.launcher.Status(context.Background()); !errors.Is(err, ErrNoServer) {
		t.Fatalf("err=%v", err)
	}
	if _, _, err := r.launcher.Start(context.Background()); !errors.Is(err, ErrNoServer) {
		t.Fatalf("err=%v", err)
	}
}

func TestStartAStoppedHost(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameStopped, 0)
	status, started, err := r.launcher.Start(context.Background())
	if err != nil || !started || status.State != Starting {
		t.Fatalf("state=%s started=%v err=%v", status.State, started, err)
	}
	if r.ec2.starts != 1 || r.followUps != 1 {
		t.Fatalf("starts=%d followUps=%d", r.ec2.starts, r.followUps)
	}
	if session := r.session(t); session.Phase != Starting || !session.RequestedAt.Equal(now) || !session.LaunchTime.IsZero() {
		t.Fatalf("session %+v", session)
	}
}

func TestStartRecordsBeforeEC2(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameStopped, 0)
	r.ssm.putErr = errors.New("denied")
	if _, started, err := r.launcher.Start(context.Background()); err == nil || started || r.ec2.starts != 0 {
		t.Fatalf("started=%v starts=%d err=%v", started, r.ec2.starts, err)
	}
}

func TestStartRefusedByEC2IsRecordedFailed(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameStopped, 0)
	r.ec2.startErr = &smithy.GenericAPIError{Code: "InsufficientInstanceCapacity"}
	status, started, err := r.launcher.Start(context.Background())
	if err == nil || started || status.State != Unknown || r.followUps != 0 {
		t.Fatalf("state=%s started=%v err=%v", status.State, started, err)
	}
	if session := r.session(t); session.Phase != Failed {
		t.Fatalf("session %+v", session)
	}
}

func TestASecondStartRefusedBecauseTheFirstWonKeepsTheStart(t *testing.T) {
	// Both requests saw the host stopped; EC2 refuses the second because the first moved it to pending.
	r := newRig(ec2types.InstanceStateNameStopped, 0)
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameStopped, ec2types.InstanceStateNamePending}
	r.ec2.launch = now
	r.ec2.startErr = &smithy.GenericAPIError{Code: "IncorrectInstanceState"}
	status, started, err := r.launcher.Start(context.Background())
	// The start stands. Its follow-up is handed off again, since EC2 may have accepted this start and lost
	// the answer; a second follow-up repeats harmlessly.
	if err != nil || !started || status.State != Starting || r.followUps != 1 {
		t.Fatalf("state=%s started=%v err=%v followUps=%d", status.State, started, err, r.followUps)
	}
	if session := r.session(t); session.Phase != Starting {
		t.Fatalf("session %+v", session)
	}
}

func TestAStartWhoseAnswerWasLostStands(t *testing.T) {
	// No answer from EC2, and a description that may still be stale: the start may have been accepted.
	r := newRig(ec2types.InstanceStateNameStopped, 0)
	r.ec2.startErr = errors.New("connection reset")
	status, started, err := r.launcher.Start(context.Background())
	if err != nil || !started || status.State != Starting || r.followUps != 1 || r.session(t).Phase != Starting {
		t.Fatalf("state=%s started=%v err=%v followUps=%d", status.State, started, err, r.followUps)
	}
}

func TestAStartStuckPendingFailsAndCanBeRetried(t *testing.T) {
	r := newRig(ec2types.InstanceStateNamePending, 16*time.Minute)
	r.setSession(t, Session{RequestedAt: now.Add(-16 * time.Minute), Phase: Starting})
	if status, err := r.launcher.Status(context.Background()); err != nil || status.State != Failed {
		t.Fatalf("state=%s err=%v", status.State, err)
	}
	status, started, err := r.launcher.Start(context.Background())
	if err != nil || !started || status.State != Starting || r.followUps != 1 || r.ec2.starts != 0 {
		t.Fatalf("state=%s started=%v err=%v followUps=%d starts=%d", status.State, started, err, r.followUps, r.ec2.starts)
	}
	// Someone else's pending host is still just starting.
	r = newRig(ec2types.InstanceStateNamePending, 16*time.Minute)
	if status, _ := r.launcher.Status(context.Background()); status.State != Starting {
		t.Fatalf("state=%s", status.State)
	}
}

func TestLostFollowUpStillReportsTheStart(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameStopped, 0)
	r.followErr = errors.New("throttled")
	status, started, err := r.launcher.Start(context.Background())
	if err == nil || !started || status.State != Starting {
		t.Fatalf("state=%s started=%v err=%v", status.State, started, err)
	}
}

func TestStartIsIdempotent(t *testing.T) {
	for _, tc := range []struct {
		name    string
		host    ec2types.InstanceStateName
		gameUp  bool
		session *Session
		want    string
	}{
		{name: "pending", host: ec2types.InstanceStateNamePending, want: Starting},
		{name: "stopping", host: ec2types.InstanceStateNameStopping, want: Stopping},
		{name: "ready", host: ec2types.InstanceStateNameRunning, gameUp: true, want: Ready},
		{name: "our start under way", host: ec2types.InstanceStateNameRunning, session: &Session{RequestedAt: now.Add(-2 * time.Minute), LaunchTime: now.Add(-time.Minute), Phase: Starting}, want: Starting},
		{name: "maintenance", host: ec2types.InstanceStateNameRunning, want: Maintenance},
	} {
		r := newRig(tc.host, time.Hour)
		if tc.name == "our start under way" {
			r.ec2.launch = now.Add(-time.Minute)
		}
		r.gameUp = tc.gameUp
		if tc.session != nil {
			r.setSession(t, *tc.session)
		}
		status, started, err := r.launcher.Start(context.Background())
		if err != nil || started || status.State != tc.want || r.ec2.starts != 0 || r.followUps != 0 {
			t.Fatalf("%s: state=%s started=%v starts=%d followUps=%d err=%v", tc.name, status.State, started, r.ec2.starts, r.followUps, err)
		}
	}
}

func TestStartRetriesAFailedGameOnTheRunningHost(t *testing.T) {
	r := newRig(ec2types.InstanceStateNameRunning, 5*time.Minute)
	r.setSession(t, Session{RequestedAt: now.Add(-6 * time.Minute), LaunchTime: now.Add(-5 * time.Minute), Phase: Failed})
	status, started, err := r.launcher.Start(context.Background())
	if err != nil || !started || status.State != Starting || r.ec2.starts != 0 || r.followUps != 1 {
		t.Fatalf("state=%s started=%v starts=%d followUps=%d err=%v", status.State, started, r.ec2.starts, r.followUps, err)
	}
	if session := r.session(t); session.Phase != Starting || !session.LaunchTime.Equal(now.Add(-5*time.Minute)) {
		t.Fatalf("session %+v", session)
	}
}

func success(output string) ssm.GetCommandInvocationOutput {
	return ssm.GetCommandInvocationOutput{Status: ssmtypes.CommandInvocationStatusSuccess, StandardOutputContent: aws.String(output)}
}

const readyOutput = `{"application": "running", "backup": "pending", "instance": "family", "ready": true, "storage": {}}`

func finishRig(t *testing.T) *rig {
	r := newRig(ec2types.InstanceStateNameStopped, 0)
	r.ec2.launch = now.Add(30 * time.Second)
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameStopped, ec2types.InstanceStateNamePending, ec2types.InstanceStateNameRunning}
	r.ssm.online = []bool{false, true}
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}, success(readyOutput)}
	r.setSession(t, Session{RequestedAt: now, Phase: Starting})
	return r
}

func TestFinishStartRunsTheWorkstationSteps(t *testing.T) {
	r := finishRig(t)
	if err := r.launcher.FinishStart(context.Background()); err != nil {
		t.Fatal(err)
	}
	if strings.Join(r.pointedAt, ",") != address {
		t.Fatalf("DNS pointed at %v", r.pointedAt)
	}
	if strings.Join(r.ssm.commands, "|") != "/usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json start family" {
		t.Fatalf("commands %v", r.ssm.commands)
	}
	session := r.session(t)
	if session.Phase != Ready || session.DNS != "current" || !session.LaunchTime.Equal(now.Add(30*time.Second)) || !session.RequestedAt.Equal(now) {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishStartStartsTheGameWhenDNSFails(t *testing.T) {
	r := finishRig(t)
	r.dnsErr = errors.New("token missing")
	if err := r.launcher.FinishStart(context.Background()); err == nil {
		t.Fatal("a DNS failure must fail the run so its alarm sees it")
	}
	if len(r.ssm.commands) != 1 {
		t.Fatal("the game must still start")
	}
	if session := r.session(t); session.Phase != Ready || session.DNS != "failed" {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishStartRecordsAFailedGame(t *testing.T) {
	for _, answer := range []ssm.GetCommandInvocationOutput{
		success(`{"application": "running", "instance": "family", "ready": false}`),
		success(`{"error": "the server did not become ready; inspect its Crafty console"}`),
		success(`not json`),
		{Status: ssmtypes.CommandInvocationStatusSuccess, ResponseCode: 1, StandardOutputContent: aws.String(readyOutput)},
		{Status: ssmtypes.CommandInvocationStatusFailed},
		{Status: ssmtypes.CommandInvocationStatusTimedOut},
	} {
		r := finishRig(t)
		r.ssm.invocation = []ssm.GetCommandInvocationOutput{answer}
		if err := r.launcher.FinishStart(context.Background()); err == nil {
			t.Fatalf("%+v must fail", answer)
		}
		if session := r.session(t); session.Phase != Failed || session.Detail == "" || session.LaunchTime.IsZero() {
			t.Fatalf("session %+v", session)
		}
	}
}

func TestFinishStartEndsWithinItsStartsDeadline(t *testing.T) {
	// The follow-up waited in Lambda's queue: it gets only what is left of the start's deadline.
	r := finishRig(t)
	r.launcher.Now = func() time.Time { return now.Add(startDeadline - finishMargin - 20*time.Millisecond) }
	r.ssm.online = []bool{false}
	if err := r.launcher.FinishStart(context.Background()); err == nil || !strings.Contains(err.Error(), "SSM") {
		t.Fatalf("err=%v", err)
	}
	// One that arrives after it does nothing at all.
	r = finishRig(t)
	r.launcher.Now = func() time.Time { return now.Add(startDeadline) }
	if err := r.launcher.FinishStart(context.Background()); err == nil || len(r.pointedAt) != 0 || len(r.ssm.commands) != 0 || r.session(t).Phase != Starting {
		t.Fatalf("err=%v pointed=%v commands=%v", err, r.pointedAt, r.ssm.commands)
	}
}

func TestAFailedFollowUpLeavesALaterStartAlone(t *testing.T) {
	r := finishRig(t)
	later := Session{RequestedAt: now.Add(time.Minute), Phase: Starting}
	r.setSession(t, later)
	if err := r.launcher.fail(context.Background(), Session{RequestedAt: now, Phase: Starting}, errors.New("late")); err == nil {
		t.Fatal("no error")
	}
	if session := r.session(t); !session.RequestedAt.Equal(later.RequestedAt) || session.Phase != Starting {
		t.Fatalf("session %+v", session)
	}
}

func TestARepeatedFollowUpLeavesAFinishedStartReady(t *testing.T) {
	r := finishRig(t)
	finished := Session{RequestedAt: now, LaunchTime: now.Add(30 * time.Second), Phase: Ready, DNS: "current"}
	r.setSession(t, finished)
	if err := r.launcher.fail(context.Background(), Session{RequestedAt: now, Phase: Starting, DNS: "failed"}, errors.New("lock held")); err == nil {
		t.Fatal("no error")
	}
	if session := r.session(t); session != finished {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishStartGivesUpAtItsDeadline(t *testing.T) {
	r := finishRig(t)
	r.ssm.online = []bool{false}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	if err := r.launcher.FinishStart(ctx); err == nil || !strings.Contains(err.Error(), "SSM") {
		t.Fatalf("err=%v", err)
	}
	if session := r.session(t); session.Phase != Failed {
		t.Fatalf("session %+v", session)
	}
}

func TestFinishStartStopsWhenTheHostStops(t *testing.T) {
	r := finishRig(t)
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNamePending, ec2types.InstanceStateNameStopping}
	if err := r.launcher.FinishStart(context.Background()); err == nil {
		t.Fatal("a stopping host must fail the start")
	}
	if session := r.session(t); session.Phase != Failed || len(r.ssm.commands) != 0 {
		t.Fatalf("session %+v commands %v", session, r.ssm.commands)
	}
}

func TestFinishStartNeedsOurStart(t *testing.T) {
	r := finishRig(t)
	r.setSession(t, Session{})
	if err := r.launcher.FinishStart(context.Background()); err == nil || len(r.ssm.commands) != 0 || len(r.pointedAt) != 0 {
		t.Fatalf("err=%v commands=%v dns=%v", err, r.ssm.commands, r.pointedAt)
	}
}
