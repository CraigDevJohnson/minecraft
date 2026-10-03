package main

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

const (
	hostID          = "i-0123456789abcdef0"
	commandID       = "11111111-2222-3333-4444-555555555555"
	statusCommandID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
)

var launched = time.Date(2026, 10, 2, 18, 0, 0, 0, time.UTC)

type fakeAWS struct {
	publicIP   string
	described  int
	onDescribe func(*fakeAWS)
	hostState  ec2types.InstanceStateName
	launchTime time.Time
	stopped    int
	stopErr    error
	stopState  ec2types.InstanceStateName

	parameter    string
	puts         int
	putCalls     int
	putFailures  map[int]bool
	sent         []*ssm.SendCommandInput
	invocation   *ssm.GetCommandInvocationOutput
	invokeErr    error
	confirmation *ssm.GetCommandInvocationOutput
	confirmErr   error
}

func (f *fakeAWS) DescribeInstances(_ context.Context, in *ec2.DescribeInstancesInput, _ ...func(*ec2.Options)) (*ec2.DescribeInstancesOutput, error) {
	if len(in.InstanceIds) != 1 || in.InstanceIds[0] != hostID {
		return nil, errors.New("unexpected host")
	}
	f.described++
	if f.onDescribe != nil {
		f.onDescribe(f)
	}
	return &ec2.DescribeInstancesOutput{Reservations: []ec2types.Reservation{{Instances: []ec2types.Instance{{
		InstanceId:      aws.String(hostID),
		State:           &ec2types.InstanceState{Name: f.hostState},
		LaunchTime:      aws.Time(f.launchTime),
		PublicIpAddress: aws.String(f.publicIP),
	}}}}}, nil
}

func (f *fakeAWS) StopInstances(_ context.Context, in *ec2.StopInstancesInput, _ ...func(*ec2.Options)) (*ec2.StopInstancesOutput, error) {
	if len(in.InstanceIds) != 1 || in.InstanceIds[0] != hostID {
		return nil, errors.New("unexpected host")
	}
	if f.stopState != "" {
		f.hostState = f.stopState
	}
	if f.stopErr != nil {
		return nil, f.stopErr
	}
	f.stopped++
	return &ec2.StopInstancesOutput{}, nil
}

func (f *fakeAWS) SendCommand(_ context.Context, in *ssm.SendCommandInput, _ ...func(*ssm.Options)) (*ssm.SendCommandOutput, error) {
	f.sent = append(f.sent, in)
	id := commandID
	if in.Parameters["commands"][0] == HostStatusCommand {
		id = statusCommandID
	}
	return &ssm.SendCommandOutput{Command: &ssmtypes.Command{CommandId: aws.String(id)}}, nil
}

func (f *fakeAWS) GetCommandInvocation(ctx context.Context, in *ssm.GetCommandInvocationInput, _ ...func(*ssm.Options)) (*ssm.GetCommandInvocationOutput, error) {
	if (aws.ToString(in.CommandId) != commandID && aws.ToString(in.CommandId) != statusCommandID) || aws.ToString(in.InstanceId) != hostID {
		return nil, errors.New("unexpected invocation")
	}
	if aws.ToString(in.CommandId) == statusCommandID {
		if f.confirmErr != nil {
			return nil, f.confirmErr
		}
		if f.confirmation != nil {
			return f.confirmation, nil
		}
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return f.invocation, f.invokeErr
}

func (f *fakeAWS) drains() int {
	count := 0
	for _, sent := range f.sent {
		if sent.Parameters["commands"][0] == DrainCommand {
			count++
		}
	}
	return count
}

func (f *fakeAWS) GetParameter(_ context.Context, _ *ssm.GetParameterInput, _ ...func(*ssm.Options)) (*ssm.GetParameterOutput, error) {
	return &ssm.GetParameterOutput{Parameter: &ssmtypes.Parameter{Value: aws.String(f.parameter)}}, nil
}

func (f *fakeAWS) PutParameter(_ context.Context, in *ssm.PutParameterInput, _ ...func(*ssm.Options)) (*ssm.PutParameterOutput, error) {
	if !aws.ToBool(in.Overwrite) {
		return nil, errors.New("state must be overwritten")
	}
	f.putCalls++
	if f.putFailures[f.putCalls] {
		return nil, errors.New("parameter write failed")
	}
	f.parameter = aws.ToString(in.Value)
	f.puts++
	return &ssm.PutParameterOutput{}, nil
}

type harness struct {
	aws       *fakeAWS
	now       time.Time
	players   int
	pingErr   error
	ctl       *Controller
	parked    int
	parkErr   error
	addresses []string
}

func newHarness() *harness {
	h := &harness{aws: &fakeAWS{hostState: ec2types.InstanceStateNameRunning, launchTime: launched, parameter: "{}", publicIP: "192.0.2.10"}, now: launched.Add(5 * time.Minute)}
	h.ctl = &Controller{
		Config: Config{HostID: hostID, StateParameter: "/test/idle-stop/state", IdleAfter: 30 * time.Minute, Instances: []string{"survival"}},
		EC2:    h.aws,
		SSM:    h.aws,
		Ping: func(_ context.Context, address string) (int, error) {
			h.addresses = append(h.addresses, address)
			return h.players, h.pingErr
		},
		ParkDNS: func(context.Context) error {
			if h.parkErr != nil {
				return h.parkErr
			}
			h.parked++
			return nil
		},
		Now: func() time.Time { return h.now },
	}
	return h
}

func (h *harness) run(t *testing.T) (Result, error) {
	t.Helper()
	return h.ctl.Run(context.Background())
}

func (h *harness) state(t *testing.T) State {
	t.Helper()
	var state State
	if err := json.Unmarshal([]byte(h.aws.parameter), &state); err != nil {
		t.Fatalf("state is not JSON: %v", err)
	}
	return state
}

func (h *harness) mustAction(t *testing.T, want string) Result {
	t.Helper()
	result, err := h.run(t)
	if err != nil {
		t.Fatalf("unexpected error for %s: %v", want, err)
	}
	if result.Action != want {
		t.Fatalf("action = %q, want %q", result.Action, want)
	}
	return result
}

// idleUntilDrain advances an idle server past the threshold and returns once the drain was sent.
func (h *harness) idleUntilDrain(t *testing.T) {
	t.Helper()
	h.mustAction(t, "idle-started")
	h.now = h.now.Add(29 * time.Minute)
	h.mustAction(t, "idle")
	h.now = h.now.Add(time.Minute)
	h.mustAction(t, "drain-sent")
}

func successfulDrain(output string) *ssm.GetCommandInvocationOutput {
	return &ssm.GetCommandInvocationOutput{Status: ssmtypes.CommandInvocationStatusSuccess, ResponseCode: 0, StandardOutputContent: aws.String(output)}
}

const goodDrain = `{"draining":true,"instances":{"survival":{"instance":"survival","application":"stopped","backup":"complete"}}}`

func TestActivePlayersKeepHostRunning(t *testing.T) {
	h := newHarness()
	h.players = 2
	result := h.mustAction(t, "active")
	if result.Players == nil || *result.Players != 2 {
		t.Fatalf("players = %v", result.Players)
	}
	if len(h.aws.sent) != 0 || h.aws.stopped != 0 {
		t.Fatal("an active server must not be drained or stopped")
	}
}

func TestIdleHostDrainsThenStopsAfterVerifiedBackup(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	sent := h.aws.sent[0]
	if aws.ToString(sent.DocumentName) != "AWS-RunShellScript" || sent.InstanceIds[0] != hostID || sent.Parameters["commands"][0] != DrainCommand || sent.Parameters["executionTimeout"][0] != "3600" {
		t.Fatalf("drain command = %+v", sent)
	}
	if h.state(t).DrainCommandID != commandID {
		t.Fatal("drain command must be recorded")
	}

	h.aws.invocation = &ssm.GetCommandInvocationOutput{Status: ssmtypes.CommandInvocationStatusInProgress}
	h.now = h.now.Add(5 * time.Minute)
	h.mustAction(t, "drain-pending")
	if h.aws.stopped != 0 {
		t.Fatal("host must not stop while the drain is running")
	}

	h.aws.invocation = successfulDrain(goodDrain)
	h.now = h.now.Add(5 * time.Minute)
	h.mustAction(t, "stopped")
	if h.aws.stopped != 1 || h.aws.drains() != 1 {
		t.Fatalf("stopped=%d sent=%d", h.aws.stopped, len(h.aws.sent))
	}
	if state := h.state(t); state.DrainCommandID != "" || state.IdleSince != nil {
		t.Fatalf("state after stop = %+v", state)
	}

	h.aws.hostState = ec2types.InstanceStateNameStopped
	h.mustAction(t, "host-not-running")
	if h.aws.parameter != "{}" {
		t.Fatalf("stopped host must clear state, got %s", h.aws.parameter)
	}
}

func TestPlayerJoiningResetsIdleTimer(t *testing.T) {
	h := newHarness()
	h.mustAction(t, "idle-started")
	h.now = h.now.Add(25 * time.Minute)
	h.players = 1
	h.mustAction(t, "active")
	h.players = 0
	h.now = h.now.Add(10 * time.Minute)
	h.mustAction(t, "idle-started")
	h.now = h.now.Add(10 * time.Minute)
	h.mustAction(t, "idle")
	if len(h.aws.sent) != 0 {
		t.Fatal("a reset idle timer must not drain early")
	}
}

func TestUnreachableServerClearsIdleCountdown(t *testing.T) {
	h := newHarness()
	h.mustAction(t, "idle-started")
	h.now = h.now.Add(time.Hour)
	h.pingErr = errors.New("private network detail")
	if _, err := h.run(t); err == nil || strings.Contains(err.Error(), "private") {
		t.Fatalf("err = %v", err)
	}
	if h.state(t).IdleSince != nil || len(h.aws.sent) != 0 || h.aws.stopped != 0 {
		t.Fatal("unknown activity must clear the countdown and leave the host running")
	}
	h.pingErr = nil
	h.mustAction(t, "idle-started")
}

func TestNewLaunchDiscardsOldIdleTimerAndDrain(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	// The host was stopped and started again between two invocations.
	h.aws.launchTime = launched.Add(2 * time.Hour)
	h.now = launched.Add(2*time.Hour + time.Minute)
	h.mustAction(t, "idle-started")
	if state := h.state(t); state.DrainCommandID != "" || !state.LaunchTime.Equal(h.aws.launchTime) {
		t.Fatalf("state = %+v", state)
	}
	if h.aws.stopped != 0 {
		t.Fatal("an old drain must never stop a new session")
	}
}

func TestBadDrainEvidenceBlocksStop(t *testing.T) {
	cases := map[string]*ssm.GetCommandInvocationOutput{
		"backup failed":        successfulDrain(`{"draining":true,"instances":{"survival":{"instance":"survival","application":"stopped","backup":"failed"}}}`),
		"still running":        successfulDrain(`{"draining":true,"instances":{"survival":{"instance":"survival","application":"running","backup":"pending"}}}`),
		"no barrier":           successfulDrain(`{"draining":false,"instances":{"survival":{"instance":"survival","application":"stopped","backup":"complete"}}}`),
		"missing instance":     successfulDrain(`{"draining":true,"instances":{}}`),
		"extra instance":       successfulDrain(`{"draining":true,"instances":{"survival":{"instance":"survival","application":"stopped","backup":"complete"},"creative":{"instance":"creative","application":"stopped","backup":"complete"}}}`),
		"mismatched name":      successfulDrain(`{"draining":true,"instances":{"survival":{"instance":"creative","application":"stopped","backup":"complete"}}}`),
		"not json":             successfulDrain(`drain finished`),
		"nonzero exit":         {Status: ssmtypes.CommandInvocationStatusSuccess, ResponseCode: 1, StandardOutputContent: aws.String(goodDrain)},
		"command failed":       {Status: ssmtypes.CommandInvocationStatusFailed, ResponseCode: 1},
		"command timed out":    {Status: ssmtypes.CommandInvocationStatusTimedOut},
		"command was canceled": {Status: ssmtypes.CommandInvocationStatusCancelled},
	}
	for name, invocation := range cases {
		t.Run(name, func(t *testing.T) {
			h := newHarness()
			h.idleUntilDrain(t)
			h.aws.invocation = invocation
			h.now = h.now.Add(5 * time.Minute)
			result, err := h.run(t)
			if !errors.Is(err, ErrBlocked) || result.Action != "blocked" {
				t.Fatalf("result=%+v err=%v", result, err)
			}
			if h.aws.stopped != 0 {
				t.Fatal("EC2 stop is forbidden without complete drain evidence")
			}
			// It stays blocked, failing every invocation so the alarm holds, and never re-drains on its own.
			h.now = h.now.Add(time.Hour)
			if _, err := h.run(t); !errors.Is(err, ErrBlocked) {
				t.Fatalf("second run err = %v", err)
			}
			if h.aws.drains() != 1 || h.aws.stopped != 0 {
				t.Fatalf("sent=%d stopped=%d", len(h.aws.sent), h.aws.stopped)
			}
		})
	}
}

func TestBlockClearsAfterHostStopsAndStartsAgain(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invocation = &ssm.GetCommandInvocationOutput{Status: ssmtypes.CommandInvocationStatusFailed}
	if _, err := h.run(t); !errors.Is(err, ErrBlocked) {
		t.Fatalf("err = %v", err)
	}
	// The operator repairs the failure and stops the host with minecraft_host.py.
	h.aws.hostState = ec2types.InstanceStateNameStopped
	h.mustAction(t, "host-not-running")
	h.aws.hostState = ec2types.InstanceStateNameRunning
	h.aws.launchTime = launched.Add(3 * time.Hour)
	h.mustAction(t, "idle-started")
}

func TestNotYetVisibleInvocationWaitsThenBlocks(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invokeErr = &ssmtypes.InvocationDoesNotExist{}
	h.mustAction(t, "drain-pending")
	h.now = h.now.Add(3 * time.Minute)
	if _, err := h.run(t); !errors.Is(err, ErrBlocked) {
		t.Fatalf("err = %v", err)
	}
}

func TestDrainPastDeadlineBlocks(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invocation = &ssm.GetCommandInvocationOutput{Status: ssmtypes.CommandInvocationStatusInProgress}
	h.now = h.now.Add(drainDeadline + time.Second)
	if _, err := h.run(t); !errors.Is(err, ErrBlocked) {
		t.Fatalf("err = %v", err)
	}
	if h.aws.stopped != 0 {
		t.Fatal("an unfinished drain must not stop EC2")
	}
}

func TestStopFailureRetriesWithoutRedraining(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invocation = successfulDrain(goodDrain)
	h.aws.stopErr = errors.New("throttled")
	if result, err := h.run(t); err == nil || errors.Is(err, ErrBlocked) || result.Action != "error" {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	h.aws.stopErr = nil
	h.mustAction(t, "stopped")
	if h.aws.drains() != 1 || h.aws.stopped != 1 {
		t.Fatalf("sent=%d stopped=%d", len(h.aws.sent), h.aws.stopped)
	}
}

func TestPendingAndStoppingHostsAreLeftAlone(t *testing.T) {
	for _, state := range []ec2types.InstanceStateName{ec2types.InstanceStateNamePending, ec2types.InstanceStateNameStopping, ec2types.InstanceStateNameStopped} {
		h := newHarness()
		h.aws.hostState = state
		h.mustAction(t, "host-not-running")
		if h.aws.puts != 0 || len(h.aws.sent) != 0 || h.aws.stopped != 0 {
			t.Fatalf("%s: unexpected writes", state)
		}
	}
}

func TestUnchangedStateIsNotRewritten(t *testing.T) {
	h := newHarness()
	h.players = 3
	h.mustAction(t, "active")
	puts := h.aws.puts
	h.mustAction(t, "active")
	if h.aws.puts != puts {
		t.Fatal("an unchanged state must not be written again")
	}
}

func TestLoadConfig(t *testing.T) {
	env := map[string]string{"HOST_ID": hostID, "STATE_PARAMETER": "/p", "IDLE_MINUTES": "30", "INSTANCES": "survival", "PUBLIC_HOSTNAME": "mc.example.com", "CLOUDFLARE_ZONE_ID": "0123456789abcdef0123456789abcdef", "CLOUDFLARE_TOKEN_SECRET_ARN": "arn:aws:secretsmanager:us-west-2:111122223333:secret:token-abcdef"}
	cfg, err := loadConfig(func(key string) string { return env[key] })
	if err != nil || cfg.IdleAfter != 30*time.Minute || len(cfg.Instances) != 1 {
		t.Fatalf("cfg=%+v err=%v", cfg, err)
	}
	for key, value := range map[string]string{"IDLE_MINUTES": "5", "INSTANCES": "Survival", "HOST_ID": "", "CLOUDFLARE_TOKEN_SECRET_ARN": "", "PUBLIC_HOSTNAME": "https://mc.example.com", "CLOUDFLARE_ZONE_ID": "bad"} {
		broken := map[string]string{}
		for k, v := range env {
			broken[k] = v
		}
		broken[key] = value
		if _, err := loadConfig(func(key string) string { return broken[key] }); err == nil {
			t.Fatalf("%s=%q must be rejected", key, value)
		}
	}
	for _, arn := range []string{"arn:aws:secretsmanager:us-west-2:111122223333:secret:*", "arn:aws:secretsmanager:us-west-2:111122223333:secret:token"} {
		if _, err := loadConfig(func(key string) string {
			if key == "CLOUDFLARE_TOKEN_SECRET_ARN" {
				return arn
			}
			return env[key]
		}); err == nil {
			t.Fatal("wildcard or partial secret ARN must be rejected")
		}
	}
	if _, err := ParseInstances("survival,survival"); err == nil {
		t.Fatal("duplicate instances must be rejected")
	}
}

func TestPingFollowsCurrentPublicAddress(t *testing.T) {
	h := newHarness()
	h.players = 1
	h.mustAction(t, "active")
	h.aws.publicIP = "192.0.2.20"
	h.mustAction(t, "active")
	if strings.Join(h.addresses, ",") != "192.0.2.10:25565,192.0.2.20:25565" {
		t.Fatalf("addresses = %v", h.addresses)
	}
}

func TestDNSFailureRetriesAfterStopWithoutAnotherDrain(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invocation = successfulDrain(goodDrain)
	h.parkErr = errors.New("private token detail")
	result, err := h.run(t)
	if err == nil || strings.Contains(err.Error(), "private") || result.Action != "dns-pending" {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	if !h.state(t).DNSPending || h.aws.stopped != 1 {
		t.Fatal("failed DNS parking must retain a retry after EC2 stop")
	}
	// An eventually consistent running description must never start another idle countdown.
	if _, err := h.run(t); err == nil {
		t.Fatal("DNS retry must report failure")
	}
	if h.aws.stopped != 1 || h.aws.drains() != 1 {
		t.Fatal("DNS retry must not drain or stop again")
	}
	h.aws.hostState = ec2types.InstanceStateNameStopped
	if _, err := h.run(t); err == nil {
		t.Fatal("DNS retry must continue after EC2 stops")
	}
	h.parkErr = nil
	h.mustAction(t, "host-not-running")
	if h.parked != 1 || h.aws.parameter != "{}" {
		t.Fatal("successful DNS retry must park and clear stop state")
	}
}

func TestNewLaunchDiscardsPendingDNSParking(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invocation = successfulDrain(goodDrain)
	h.parkErr = errors.New("unreachable")
	h.run(t)
	h.aws.launchTime = launched.Add(2 * time.Hour)
	h.now = h.aws.launchTime.Add(time.Minute)
	h.parkErr = nil
	h.players = 1
	h.mustAction(t, "active")
	if h.parked != 0 || h.state(t).DNSPending {
		t.Fatal("an old stop must not park a new session")
	}
}

func TestNewLaunchBetweenDrainReadAndStopRejectsOldEvidence(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invocation = successfulDrain(goodDrain)
	h.aws.described = 0
	h.aws.onDescribe = func(f *fakeAWS) {
		if f.described == 2 {
			f.launchTime = launched.Add(2 * time.Hour)
		}
	}
	h.mustAction(t, "host-changed")
	if h.aws.stopped != 0 || h.parked != 0 {
		t.Fatal("stale drain evidence must not stop or park a new launch")
	}
}

func TestSuccessfulStopParksDNSAndWaitsForEC2(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invocation = successfulDrain(goodDrain)
	h.mustAction(t, "stopped")
	h.now = h.now.Add(time.Hour)
	h.mustAction(t, "stop-pending")
	if h.parked != 1 || h.aws.stopped != 1 || h.aws.drains() != 1 {
		t.Fatal("successful stop must park once and not drain again")
	}
}

func TestMissingPublicAddressNeverDrains(t *testing.T) {
	h := newHarness()
	h.mustAction(t, "idle-started")
	h.now = h.now.Add(time.Hour)
	h.aws.publicIP = ""
	if _, err := h.run(t); err == nil {
		t.Fatal("missing public address must report an error")
	}
	if len(h.aws.sent) != 0 || h.state(t).IdleSince != nil {
		t.Fatal("an unreachable host must not be assumed idle")
	}
}

func TestPreStopCheckpointFailureLeavesHostRunning(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invocation = successfulDrain(goodDrain)
	h.aws.putFailures = map[int]bool{h.aws.putCalls + 1: true}
	if _, err := h.run(t); err == nil {
		t.Fatal("a failed pre-stop checkpoint must report an error")
	}
	if h.aws.stopped != 0 || h.parked != 0 || h.state(t).DNSPending {
		t.Fatal("EC2 stop requires a persisted DNS retry obligation")
	}
	h.aws.putFailures = nil
	h.mustAction(t, "stopped")
	if h.aws.drains() != 1 {
		t.Fatal("checkpoint retry must reuse the existing drain")
	}
}

func TestDNSRetrySurvivesPostStopStateWriteFailure(t *testing.T) {
	for _, ambiguous := range []bool{false, true} {
		t.Run(map[bool]string{false: "stop accepted", true: "stop response lost"}[ambiguous], func(t *testing.T) {
			h := newHarness()
			h.idleUntilDrain(t)
			h.aws.invocation = successfulDrain(goodDrain)
			h.aws.stopState = ec2types.InstanceStateNameStopped
			h.parkErr = errors.New("DNS unavailable")
			h.aws.putFailures = map[int]bool{h.aws.putCalls + 2: true}
			if ambiguous {
				h.aws.stopErr = errors.New("stop response lost")
			}
			if _, err := h.run(t); err == nil {
				t.Fatal("the stop/DNS failure must be reported")
			}
			if !h.state(t).DNSPending || h.state(t).DrainCommandID != commandID {
				t.Fatal("the durable checkpoint must retain DNS parking and completed drain evidence")
			}
			if _, err := h.run(t); err == nil {
				t.Fatal("DNS must still retry while EC2 is stopped")
			}
			h.parkErr = nil
			h.aws.putFailures = nil
			h.mustAction(t, "host-not-running")
			if h.parked != 1 || h.aws.parameter != "{}" || h.aws.drains() != 1 {
				t.Fatal("the retained obligation must park DNS without another drain")
			}
		})
	}
}

func TestRebootedHostWithClearedDrainBarrierIsNeverStopped(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invocation = successfulDrain(goodDrain)
	h.aws.confirmation = successfulDrain(strings.Replace(goodDrain, `"draining":true`, `"draining":false`, 1))
	if _, err := h.run(t); !errors.Is(err, ErrBlocked) {
		t.Fatalf("fresh evidence must reject a reboot: %v", err)
	}
	if h.aws.stopped != 0 || h.aws.drains() != 1 || h.parked != 0 {
		t.Fatal("a cleared barrier must forbid EC2 stop without another drain")
	}
}

func TestMalformedFreshStatusBlocksStop(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invocation = successfulDrain(goodDrain)
	h.aws.confirmation = successfulDrain("not json")
	if _, err := h.run(t); !errors.Is(err, ErrBlocked) || h.aws.stopped != 0 {
		t.Fatalf("invalid fresh evidence must block stop: %v", err)
	}
}

func TestFreshStatusWaitRespectsInvocationDeadline(t *testing.T) {
	for _, invisible := range []bool{false, true} {
		t.Run(map[bool]string{false: "pending", true: "invisible"}[invisible], func(t *testing.T) {
			h := newHarness()
			h.idleUntilDrain(t)
			h.aws.invocation = successfulDrain(goodDrain)
			h.aws.confirmation = &ssm.GetCommandInvocationOutput{Status: ssmtypes.CommandInvocationStatusPending}
			if invisible {
				h.aws.confirmErr = &ssmtypes.InvocationDoesNotExist{}
			}
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
			defer cancel()
			start := time.Now()
			if _, err := h.ctl.Run(ctx); err == nil || errors.Is(err, ErrBlocked) {
				t.Fatalf("unfinished status must leave the host running for retry: %v", err)
			}
			if time.Since(start) > time.Second || h.aws.stopped != 0 || h.aws.drains() != 1 {
				t.Fatal("fresh status polling must respect the shorter Lambda deadline")
			}
		})
	}
}

func TestStopRetryRechecksCurrentDrainWithoutRedraining(t *testing.T) {
	h := newHarness()
	h.idleUntilDrain(t)
	h.aws.invocation = successfulDrain(goodDrain)
	h.aws.stopErr = errors.New("throttled")
	if _, err := h.run(t); err == nil {
		t.Fatal("stop failure must report an error")
	}
	h.aws.stopErr = nil
	h.aws.confirmation = successfulDrain(strings.Replace(goodDrain, `"draining":true`, `"draining":false`, 1))
	if _, err := h.run(t); !errors.Is(err, ErrBlocked) {
		t.Fatalf("retry must check the current boot: %v", err)
	}
	if h.aws.stopped != 0 || h.aws.drains() != 1 {
		t.Fatal("an old confirmation cannot authorize a retry after reboot")
	}
}
