package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

// operated is the adapter's answer to an operator change on a world's server.
func operated(world, edition, operation, verdict string) ssm.GetCommandInvocationOutput {
	return success(`{"instance": "` + world + `", "edition": "` + edition + `", "operation": "` + operation + `", "operators": "` + verdict + `"}`)
}

// allowlistHolds puts entries on the allowlist of record.
func (w *worldsRig) allowlistHolds(t *testing.T, entries ...Listed) {
	t.Helper()
	encoded, err := json.Marshal(entries)
	if err != nil {
		t.Fatal(err)
	}
	w.shared.params[allowlistName] = string(encoded)
}

func (w *worldsRig) operatorsRecord() string { return w.shared.params[operatorsName] }

var (
	listedSteve  = Listed{Edition: "java", Player: steve}
	listedSample = Listed{Edition: "bedrock", Player: sample}
)

func TestOperatorsListsEveryWorldInOrder(t *testing.T) {
	w := newWorlds(t)
	w.shared.params[operatorsName] = `{"creative":[{"edition":"bedrock","uuid":"00000000-0000-0000-0009-01eed05d1009","name":"Sample Player"}],"nether":[]}`
	list, err := w.worlds.Operators(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// Every world the pinned content has, in its order, with no UUID; a world the record names that the content
	// does not have is not shown.
	if encoded, _ := json.Marshal(list); string(encoded) != `{"worlds":[{"id":"survival","operators":[]},{"id":"creative","operators":[{"edition":"bedrock","name":"Sample Player"}]}]}` {
		t.Fatalf("list %s", encoded)
	}
	w.shared.getErr = errors.New("throttled")
	if _, err := w.worlds.Operators(context.Background()); err == nil {
		t.Fatal("an unreadable record must fail the list")
	}
}

func TestAnOperatorMustBeOnTheAllowlist(t *testing.T) {
	w := newWorlds(t)
	w.allowlistHolds(t, listedSample)
	_, err := w.worlds.ChangeOperator(context.Background(), OperatorRequest{World: "survival", Operation: "add", Edition: "java", Name: "Steve"})
	if !errors.Is(err, ErrNotAllowlisted) || w.operatorsRecord() != "{}" {
		t.Fatalf("err %v, record %s", err, w.operatorsRecord())
	}
	// A gamertag on the allowlist is not the Java account of the same name.
	_, err = w.worlds.ChangeOperator(context.Background(), OperatorRequest{World: "survival", Operation: "add", Edition: "java", Name: "Sample_Player"})
	if !errors.Is(err, ErrNotAllowlisted) {
		t.Fatalf("err %v", err)
	}
}

func TestChangeOperatorChangesTheRecordAndTheRunningWorld(t *testing.T) {
	w := newWorlds(t)
	w.allowlistHolds(t, listedSteve, listedSample)
	w.running(w.survival)
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}, operated("survival", "java", "add", "updated")}
	// The name is matched whatever its case, and the record keeps the allowlist's account.
	result, err := w.worlds.ChangeOperator(context.Background(), OperatorRequest{World: "survival", Operation: "add", Edition: "java", Name: "steve"})
	if err != nil || result != (OperatorResult{World: "survival", Operation: "add", Operators: "updated"}) {
		t.Fatalf("result %+v err %v", result, err)
	}
	if w.operatorsRecord() != `{"survival":[{"edition":"java","uuid":"069a79f4-44e9-4726-a5be-fca90e38aaf5","name":"Steve"}]}` {
		t.Fatalf("record %s", w.operatorsRecord())
	}
	if len(w.survival.ssm.commands) != 1 || len(w.creative.ssm.commands) != 0 || w.survival.ssm.limits[0] != "240" {
		t.Fatalf("commands %q and %q", w.survival.ssm.commands, w.creative.ssm.commands)
	}
	script := w.survival.ssm.commands[0]
	encoded, _ := json.Marshal(steve)
	for _, want := range []string{
		"set -eu",
		`player="$(mktemp)"`,
		`trap 'rm -f "$player"' EXIT`,
		"printf '%s' '" + base64.StdEncoding.EncodeToString(encoded) + "' | base64 -d > \"$player\"",
		remoteCommand + ` operator survival add --edition java --player-file "$player"`,
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("script lacks %q:\n%s", want, script)
		}
	}
	if strings.Contains(script, steve.UUID) || strings.Contains(script, "Steve") {
		t.Fatal("the identity must travel encoded, never as shell text")
	}

	// Asked again, the record and the server already hold the player.
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{operated("survival", "java", "add", "unchanged")}
	result, err = w.worlds.ChangeOperator(context.Background(), OperatorRequest{World: "survival", Operation: "add", Edition: "java", Name: "Steve"})
	if err != nil || result.Operators != "unchanged" {
		t.Fatalf("result %+v err %v", result, err)
	}

	// A removal takes the player off the record, leaving no empty list behind, and off the server.
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{operated("survival", "java", "remove", "updated")}
	result, err = w.worlds.ChangeOperator(context.Background(), OperatorRequest{World: "survival", Operation: "remove", Edition: "java", Name: "STEVE"})
	if err != nil || result.Operators != "updated" || w.operatorsRecord() != "{}" {
		t.Fatalf("result %+v err %v record %s", result, err, w.operatorsRecord())
	}
	if last := w.survival.ssm.commands[len(w.survival.ssm.commands)-1]; !strings.Contains(last, remoteCommand+` operator survival remove --edition java --player-file "$player"`) {
		t.Fatalf("last command %s", last)
	}

	// Removing someone who is no operator there changes nothing and sends nothing.
	sent := len(w.survival.ssm.commands)
	result, err = w.worlds.ChangeOperator(context.Background(), OperatorRequest{World: "survival", Operation: "remove", Edition: "java", Name: "Steve"})
	if err != nil || result.Operators != "unchanged" || len(w.survival.ssm.commands) != sent {
		t.Fatalf("result %+v err %v", result, err)
	}
}

func TestChangeOperatorWorksWithTheWorldStopped(t *testing.T) {
	w := newWorlds(t)
	w.allowlistHolds(t, listedSteve, listedSample)
	result, err := w.worlds.ChangeOperator(context.Background(), OperatorRequest{World: "creative", Operation: "add", Edition: "bedrock", Name: "sample player"})
	if err != nil || result.Operators != "updated" || len(w.creative.ssm.commands) != 0 {
		t.Fatalf("result %+v err %v commands %q", result, err, w.creative.ssm.commands)
	}
	if w.operatorsRecord() != `{"creative":[{"edition":"bedrock","uuid":"00000000-0000-0000-0009-01eed05d1009","name":"Sample Player"}]}` {
		t.Fatalf("record %s", w.operatorsRecord())
	}
}

func TestChangeOperatorFailsWhileTheWorldIsStartingButKeepsTheChange(t *testing.T) {
	w := newWorlds(t)
	w.allowlistHolds(t, listedSteve)
	// A host launched a minute ago whose game does not answer yet is starting.
	w.survival.ec2.states, w.survival.ec2.launch = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}, now.Add(-time.Minute)
	_, err := w.worlds.ChangeOperator(context.Background(), OperatorRequest{World: "survival", Operation: "add", Edition: "java", Name: "Steve"})
	if err == nil || !strings.Contains(err.Error(), "world survival: the world is starting") || !strings.Contains(w.operatorsRecord(), steve.UUID) {
		t.Fatalf("err %v record %s", err, w.operatorsRecord())
	}
	if len(w.survival.ssm.commands) != 0 {
		t.Fatal("nothing may be sent to a world whose game does not answer yet")
	}
}

func TestChangeOperatorReportsAnAdapterRefusal(t *testing.T) {
	for name, answer := range map[string]ssm.GetCommandInvocationOutput{
		"a refusal":           {Status: ssmtypes.CommandInvocationStatusSuccess, ResponseCode: 1, StandardOutputContent: aws.String(`{"error": "operator changes require a running, ready server"}`)},
		"another instance":    operated("creative", "java", "add", "updated"),
		"another operation":   operated("survival", "java", "remove", "updated"),
		"an unknown verdict":  operated("survival", "java", "add", "maybe"),
		"a failed invocation": {Status: ssmtypes.CommandInvocationStatusFailed},
	} {
		w := newWorlds(t)
		w.allowlistHolds(t, listedSteve)
		w.running(w.survival)
		w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{answer}
		if _, err := w.worlds.ChangeOperator(context.Background(), OperatorRequest{World: "survival", Operation: "add", Edition: "java", Name: "Steve"}); err == nil {
			t.Fatalf("%s: must fail", name)
		} else if strings.Contains(err.Error(), "Steve") {
			t.Fatalf("%s: the error names the player: %v", name, err)
		}
	}
}

func TestChangeOperatorRejectsBadRequests(t *testing.T) {
	w := newWorlds(t)
	w.allowlistHolds(t, listedSteve)
	for _, request := range []OperatorRequest{
		{World: "survival", Operation: "ban", Edition: "java", Name: "Steve"},
		{World: "survival", Operation: "add", Edition: "pocket", Name: "Steve"},
		{World: "survival", Operation: "add", Edition: "java", Name: "Steve Jobs"},
		{World: "survival", Operation: "add", Edition: "bedrock", Name: "op Admin;"},
	} {
		if _, err := w.worlds.ChangeOperator(context.Background(), request); !errors.Is(err, ErrBadAllowlistRequest) {
			t.Fatalf("%+v: err %v", request, err)
		}
	}
	if _, err := w.worlds.ChangeOperator(context.Background(), OperatorRequest{World: "nether", Operation: "add", Edition: "java", Name: "Steve"}); !errors.Is(err, ErrUnknownWorld) {
		t.Fatalf("err %v", err)
	}
	if w.operatorsRecord() != "{}" {
		t.Fatalf("record %s", w.operatorsRecord())
	}
}

func TestLeavingTheAllowlistEndsBeingAnOperatorEverywhere(t *testing.T) {
	w := allowlistRig(t)
	w.allowlistHolds(t, listedSteve, listedSample)
	w.shared.params[operatorsName] = fmt.Sprintf(`{"survival":[{"edition":"java","uuid":"%s","name":"Steve"}],"creative":[{"edition":"java","uuid":"%s","name":"Steve"},{"edition":"bedrock","uuid":"%s","name":"Sample Player"}]}`, steve.UUID, steve.UUID, sample.UUID)
	// Survival runs: the player leaves its allowlist and its operators. Creative is stopped, so its next start
	// writes the record.
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("survival", "remove", "updated"), operated("survival", "java", "remove", "updated")}
	result, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "remove", Edition: "java", Name: "Steve"})
	if err != nil || result.Allowlist != "updated" {
		t.Fatalf("result %+v err %v", result, err)
	}
	if w.operatorsRecord() != `{"creative":[{"edition":"bedrock","uuid":"00000000-0000-0000-0009-01eed05d1009","name":"Sample Player"}]}` {
		t.Fatalf("record %s", w.operatorsRecord())
	}
	if len(w.survival.ssm.commands) != 2 || !strings.Contains(w.survival.ssm.commands[1], remoteCommand+` operator survival remove --edition java --player-file "$player"`) || len(w.creative.ssm.commands) != 0 {
		t.Fatalf("commands %q and %q", w.survival.ssm.commands, w.creative.ssm.commands)
	}
}

func TestEachStartCarriesItsWorldsOperatorsAsTheServersOwnFile(t *testing.T) {
	w := newWorlds(t)
	w.allowlistHolds(t, listedSteve, listedSample)
	for _, request := range []OperatorRequest{
		{World: "creative", Operation: "add", Edition: "java", Name: "Steve"},
		{World: "creative", Operation: "add", Edition: "bedrock", Name: "Sample Player"},
		{World: "survival", Operation: "add", Edition: "java", Name: "Steve"},
	} {
		if _, err := w.worlds.ChangeOperator(context.Background(), request); err != nil {
			t.Fatal(err)
		}
	}
	// ops.json's own format. Floodgate writes a Bedrock player as a dot and the gamertag with underscores.
	ops := `[{"uuid":"069a79f4-44e9-4726-a5be-fca90e38aaf5","name":"Steve","level":4,"bypassesPlayerLimit":false},{"uuid":"00000000-0000-0000-0009-01eed05d1009","name":".Sample_Player","level":4,"bypassesPlayerLimit":false}]`
	r := w.creative
	if err := finishStart(t, r); err != nil {
		t.Fatal(err)
	}
	if want := "printf '%s' '" + base64.StdEncoding.EncodeToString([]byte(ops)) + "' | base64 -d > \"$ops\""; len(r.ssm.commands) != 1 || !strings.Contains(r.ssm.commands[0], want) {
		t.Fatalf("commands %q", r.ssm.commands)
	}
	if strings.Contains(r.ssm.commands[0], "Steve") {
		t.Fatal("the list must travel encoded, never as shell text")
	}
	// A world nobody operates starts with an empty list, which leaves its server no operator.
	empty, err := w.worlds.opsFile(context.Background(), "nether")
	if err != nil || string(empty) != "[]" {
		t.Fatalf("%s %v", empty, err)
	}
}

func TestAStartFailsWhenItsOperatorsCannotBeRead(t *testing.T) {
	w := newWorlds(t)
	w.shared.params[operatorsName] = "not json"
	if err := finishStart(t, w.survival); err == nil || len(w.survival.ssm.commands) != 0 {
		t.Fatalf("err %v commands %q", err, w.survival.ssm.commands)
	}
}

func TestAllowlistPlayersListsTheRecordWithoutUUIDs(t *testing.T) {
	w := newWorlds(t)
	w.allowlistHolds(t, listedSteve, listedSample)
	players, err := w.worlds.AllowlistPlayers(context.Background())
	if encoded, _ := json.Marshal(players); err != nil || string(encoded) != `[{"edition":"java","name":"Steve"},{"edition":"bedrock","name":"Sample Player"}]` {
		t.Fatalf("players %s err %v", encoded, err)
	}
	w.allowlistHolds(t)
	if players, err := w.worlds.AllowlistPlayers(context.Background()); err != nil || players == nil || len(players) != 0 {
		t.Fatalf("players %v err %v", players, err)
	}
}
