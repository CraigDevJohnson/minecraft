package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

var (
	steve = Player{UUID: "069a79f4-44e9-4726-a5be-fca90e38aaf5", Name: "Steve"}
	// sample is a Bedrock player as the lookup answers: the Floodgate UUID and the gamertag itself.
	sample = Player{UUID: "00000000-0000-0000-0009-01eed05d1009", Name: "Sample Player"}
	// renamed is the entry the record still holds for a Java account whose name Mojang no longer knows.
	renamed = Player{UUID: "853c80ef-3c37-49fd-aa49-938b674adae6", Name: "Old_Name"}
)

// lookedUp stands in for Mojang and GeyserMC, which find a name whatever its case.
func lookedUp(name string) (Player, error) {
	switch strings.ToLower(name) {
	case "nobody", "old_name":
		return Player{}, ErrPlayerNotFound
	case "linked player":
		return Player{}, ErrLinkedAccount
	case "outage":
		return Player{}, errors.New("Mojang answered 503")
	case "sample player":
		return sample, nil
	}
	return steve, nil
}

// allowlisted is the adapter's answer to an allowlist change on a world's server.
func allowlisted(world, operation, verdict string) ssm.GetCommandInvocationOutput {
	return success(`{"instance": "` + world + `", "edition": "java", "operation": "` + operation + `", "allowlist": "` + verdict + `"}`)
}

// allowlistRig has the survival world ready, its adapter answering the one allowlist command it gets, and the
// creative world stopped.
func allowlistRig(t *testing.T) *worldsRig {
	t.Helper()
	w := newWorlds(t)
	w.running(w.survival)
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}, allowlisted("survival", "add", "updated")}
	return w
}

// running has a world's host up for an hour with its game answering.
func (w *worldsRig) running(r *rig) {
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}
	r.ec2.launch = now.Add(-time.Hour)
	r.gameUp = true
}

// record answers the allowlist of record as the parameter holds it.
func (w *worldsRig) record() string { return w.shared.params[allowlistName] }

func TestAllowlistChangesTheRecordAndEachRunningWorld(t *testing.T) {
	w := allowlistRig(t)
	result, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"})
	if err != nil || result.Edition != "java" || result.Operation != "add" || result.Allowlist != "updated" || !slices.Equal(result.Worlds, []string{"survival"}) {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	if len(w.looked) != 1 || w.looked[0] != "java:Steve" {
		t.Fatalf("looked up %v", w.looked)
	}
	// The record holds the account the lookup found, which is what every later start carries.
	if w.record() != `[{"edition":"java","uuid":"069a79f4-44e9-4726-a5be-fca90e38aaf5","name":"Steve"}]` {
		t.Fatalf("record %s", w.record())
	}
	if len(w.survival.ssm.commands) != 1 || len(w.creative.ssm.commands) != 0 {
		t.Fatalf("commands %v and %v; only a running world is changed live", w.survival.ssm.commands, w.creative.ssm.commands)
	}
	// Four Crafty calls of up to 30 seconds, the 60-second verification and the install must all fit.
	if len(w.survival.ssm.limits) != 1 || w.survival.ssm.limits[0] != "240" {
		t.Fatalf("execution limits %v", w.survival.ssm.limits)
	}
	script := w.survival.ssm.commands[0]
	encoded, _ := json.Marshal(steve)
	for _, want := range []string{
		"set -eu",
		`player="$(mktemp)"`,
		`trap 'rm -f "$player"' EXIT`,
		"printf '%s' '" + base64.StdEncoding.EncodeToString(encoded) + "' | base64 -d > \"$player\"",
		remoteCommand + ` allowlist survival add --edition java --player-file "$player"`,
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("script lacks %q:\n%s", want, script)
		}
	}
	if strings.Contains(script, steve.UUID) || strings.Contains(script, "Steve") {
		t.Fatal("the identity must travel encoded, never as shell text")
	}
}

func TestAllowlistReachesEveryRunningWorld(t *testing.T) {
	w := allowlistRig(t)
	w.running(w.creative)
	w.creative.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("creative", "add", "updated")}
	result, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"})
	if err != nil || result.Allowlist != "updated" || !slices.Equal(result.Worlds, []string{"survival", "creative"}) {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	if len(w.creative.ssm.commands) != 1 || !strings.Contains(w.creative.ssm.commands[0], remoteCommand+` allowlist creative add --edition java --player-file "$player"`) {
		t.Fatalf("creative's commands %q", w.creative.ssm.commands)
	}
	// Asked again, the record and both servers already hold the player.
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("survival", "add", "unchanged")}
	w.creative.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("creative", "add", "unchanged")}
	before := w.record()
	result, err = w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"})
	if err != nil || result.Allowlist != "unchanged" || len(result.Worlds) != 2 || w.record() != before {
		t.Fatalf("result=%+v err=%v record=%s", result, err, w.record())
	}
	// A removal takes the player off the record and off both servers.
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("survival", "remove", "updated")}
	w.creative.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("creative", "remove", "updated")}
	result, err = w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "remove", Edition: "java", Name: "Steve"})
	if err != nil || result.Allowlist != "updated" || len(result.Worlds) != 2 || w.record() != "[]" {
		t.Fatalf("result=%+v err=%v record=%s", result, err, w.record())
	}
}

func TestAllowlistWorksWithEveryWorldStopped(t *testing.T) {
	for _, tc := range []struct {
		name string
		rig  func(r *rig)
	}{
		{"stopped", func(r *rig) {}},
		{"game down", func(r *rig) {
			r.ec2.states, r.ec2.launch = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}, now.Add(-time.Hour)
		}},
		{"stopping", func(r *rig) { r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameStopping} }},
	} {
		w := newWorlds(t)
		tc.rig(w.survival)
		result, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "bedrock", Name: "Sample Player"})
		if err != nil || result.Allowlist != "updated" || result.Worlds == nil || len(result.Worlds) != 0 {
			t.Fatalf("%s: result=%+v err=%v", tc.name, result, err)
		}
		if w.record() != `[{"edition":"bedrock","uuid":"00000000-0000-0000-0009-01eed05d1009","name":"Sample Player"}]` {
			t.Fatalf("%s: record %s", tc.name, w.record())
		}
		if len(w.survival.ssm.commands) != 0 || len(w.creative.ssm.commands) != 0 {
			t.Fatalf("%s: nothing may be sent to a world that is not ready; its next start carries the list", tc.name)
		}
	}
}

func TestEachStartCarriesTheAllowlistAsTheServersOwnFile(t *testing.T) {
	w := newWorlds(t)
	for _, request := range []AllowlistRequest{{Operation: "add", Edition: "java", Name: "Steve"}, {Operation: "add", Edition: "bedrock", Name: "Sample Player"}} {
		if _, err := w.worlds.Allowlist(context.Background(), request); err != nil {
			t.Fatal(err)
		}
	}
	// whitelist.json's own format. Floodgate writes a Bedrock player as a dot and the gamertag with underscores.
	whitelist := `[{"uuid":"069a79f4-44e9-4726-a5be-fca90e38aaf5","name":"Steve"},{"uuid":"00000000-0000-0000-0009-01eed05d1009","name":".Sample_Player"}]`
	r := w.creative
	if err := finishStart(t, r); err != nil {
		t.Fatal(err)
	}
	want := strings.Join([]string{
		"set -eu",
		`allowlist="$(mktemp)"`,
		`trap 'rm -f "$allowlist"' EXIT`,
		"printf '%s' '" + base64.StdEncoding.EncodeToString([]byte(whitelist)) + "' | base64 -d > \"$allowlist\"",
		remoteCommand + ` start creative --mods=fawe --allowlist-file "$allowlist"`,
	}, "\n")
	if len(r.ssm.commands) != 1 || r.ssm.commands[0] != want {
		t.Fatalf("commands %q", r.ssm.commands)
	}
	if strings.Contains(r.ssm.commands[0], "Steve") || strings.Contains(r.ssm.commands[0], steve.UUID) {
		t.Fatal("the list must travel encoded, never as shell text")
	}
}

func TestAStartFailsWhenItsAllowlistCannotBeRead(t *testing.T) {
	w := newWorlds(t)
	w.shared.params[allowlistName] = "Steve"
	r := w.survival
	if err := finishStart(t, r); err == nil || strings.Contains(err.Error(), "Steve") {
		t.Fatalf("err=%v; a start never opens the game to a list it could not read, and never repeats what it read", err)
	}
	if session := r.session(t); session.Phase != Failed || len(r.ssm.commands) != 0 {
		t.Fatalf("session %+v commands %v", session, r.ssm.commands)
	}
}

func TestAllowlistNeverRecordsOrSendsAnUnknownPlayer(t *testing.T) {
	w := allowlistRig(t)
	if _, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "remove", Edition: "bedrock", Name: "Nobody"}); !errors.Is(err, ErrPlayerNotFound) {
		t.Fatalf("err=%v", err)
	}
	if _, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Outage"}); err == nil || errors.Is(err, ErrPlayerNotFound) || !strings.Contains(err.Error(), "look up player") {
		t.Fatalf("err=%v", err)
	}
	for _, operation := range []string{"add", "remove"} {
		if _, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: operation, Edition: "bedrock", Name: "Linked Player"}); !errors.Is(err, ErrLinkedAccount) {
			t.Fatalf("%s: err=%v; a linked gamertag's own entry would not change who may join", operation, err)
		}
	}
	if len(w.survival.ssm.commands) != 0 || w.record() != "[]" {
		t.Fatal("a failed lookup must record and send nothing")
	}
}

func TestAllowlistRemovesARenamedJavaPlayerByTheNameTheRecordHolds(t *testing.T) {
	removal := AllowlistRequest{Operation: "remove", Edition: "java", Name: "old_name"}
	held, _ := json.Marshal([]Listed{{Edition: "java", Player: steve}, {Edition: "java", Player: renamed}})
	w := allowlistRig(t)
	w.shared.params[allowlistName] = string(held)
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("survival", "remove", "updated")}
	result, err := w.worlds.Allowlist(context.Background(), removal)
	if err != nil || result.Allowlist != "updated" || !slices.Equal(result.Worlds, []string{"survival"}) {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	if w.record() != `[{"edition":"java","uuid":"069a79f4-44e9-4726-a5be-fca90e38aaf5","name":"Steve"}]` {
		t.Fatalf("record %s", w.record())
	}
	encoded := base64.StdEncoding.EncodeToString([]byte(`{"name":"old_name"}`))
	if len(w.survival.ssm.commands) != 1 || !strings.Contains(w.survival.ssm.commands[0], "printf '%s' '"+encoded+"'") {
		t.Fatalf("the adapter must get the name alone, with no UUID: %v", w.survival.ssm.commands)
	}

	// A running server that holds no entry under that name leaves the record's removal standing.
	w = allowlistRig(t)
	w.shared.params[allowlistName] = string(held)
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("survival", "remove", "not-listed")}
	if result, err := w.worlds.Allowlist(context.Background(), removal); err != nil || result.Allowlist != "updated" {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	// A removal whose first try reached the record but not the server is finished by asking again.
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("survival", "remove", "updated")}
	if result, err := w.worlds.Allowlist(context.Background(), removal); err != nil || result.Allowlist != "updated" {
		t.Fatalf("result=%+v err=%v", result, err)
	}

	// Neither the record nor a running server holding the name is the same answer as Mojang not knowing it.
	w = allowlistRig(t)
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("survival", "remove", "not-listed")}
	if _, err := w.worlds.Allowlist(context.Background(), removal); !errors.Is(err, ErrPlayerNotFound) {
		t.Fatalf("err=%v", err)
	}
	w = newWorlds(t)
	if _, err := w.worlds.Allowlist(context.Background(), removal); !errors.Is(err, ErrPlayerNotFound) {
		t.Fatalf("with every world stopped: err=%v", err)
	}

	// An add needs the account's UUID, and the adapter's not-listed answers only a removal by name.
	w = allowlistRig(t)
	if _, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Old_Name"}); !errors.Is(err, ErrPlayerNotFound) || len(w.survival.ssm.commands) != 0 || w.record() != "[]" {
		t.Fatalf("err=%v commands=%v", err, w.survival.ssm.commands)
	}
	w = allowlistRig(t)
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("survival", "add", "not-listed")}
	if _, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"}); err == nil || errors.Is(err, ErrPlayerNotFound) {
		t.Fatalf("err=%v", err)
	}
}

func TestAllowlistPassesOnTheAdaptersReason(t *testing.T) {
	w := allowlistRig(t)
	w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusFailed, ResponseCode: 1, StandardOutputContent: aws.String(`{"error": "the server is draining"}` + "\n")}}
	_, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"})
	if err == nil || !strings.HasPrefix(err.Error(), "world survival: ") || !strings.HasSuffix(err.Error(), "ended with status Failed: the server is draining") {
		t.Fatalf("err=%v", err)
	}
	for _, output := range []string{"", "Traceback (most recent call last):", `{"error": "` + strings.Repeat("x", 201) + `"}`} {
		if adapterError(output) != "" {
			t.Fatalf("%q must add nothing to the error", output)
		}
	}
}

func TestAllowlistRejectsBadRequestsBeforeLookingAnythingUp(t *testing.T) {
	w := allowlistRig(t)
	for _, request := range []AllowlistRequest{
		{Operation: "ban", Edition: "java", Name: "Steve"},
		{Operation: "add", Edition: "pocket", Name: "Steve"},
		{Operation: "add", Edition: "java", Name: "St"},
		{Operation: "add", Edition: "java", Name: "Steve;whitelist off"},
		{Operation: "add", Edition: "bedrock", Name: " Sample Player"},
		{Operation: "add", Edition: "bedrock", Name: "Sample\"Player"},
		{Operation: "add", Edition: "bedrock", Name: "SeventeenCharacters"},
		{Operation: "add", Edition: "bedrock", Name: ""},
	} {
		if _, err := w.worlds.Allowlist(context.Background(), request); !errors.Is(err, ErrBadAllowlistRequest) {
			t.Fatalf("%+v: err=%v", request, err)
		}
	}
	if len(w.looked) != 0 || len(w.survival.ssm.commands) != 0 || w.record() != "[]" {
		t.Fatal("a bad request must neither look up, record nor send anything")
	}
	for _, name := range []string{"Sample Player", "Player4Ever", "Underscore_", "Ångström 42"} {
		if err := ValidateAllowlistRequest(AllowlistRequest{Operation: "remove", Edition: "bedrock", Name: name}); err != nil {
			t.Fatalf("%q must be a valid gamertag: %v", name, err)
		}
	}
}

func TestAllowlistReportsAnAdapterRefusalOrAnUnconfirmedChange(t *testing.T) {
	for _, tc := range []struct {
		name   string
		answer ssm.GetCommandInvocationOutput
	}{
		{"refused", ssm.GetCommandInvocationOutput{Status: ssmtypes.CommandInvocationStatusSuccess, ResponseCode: 2, StandardOutputContent: aws.String("")}},
		{"failed", ssm.GetCommandInvocationOutput{Status: ssmtypes.CommandInvocationStatusFailed}},
		{"another change", allowlisted("survival", "remove", "updated")},
		{"another world", allowlisted("creative", "add", "updated")},
		{"no verdict", success(`{"instance": "survival", "edition": "java", "operation": "add"}`)},
		{"not json", success("allowlist: updated")},
	} {
		w := allowlistRig(t)
		w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{tc.answer}
		result, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"})
		if err == nil || result.Allowlist != "" || result.Worlds != nil {
			t.Fatalf("%s: result=%+v err=%v", tc.name, result, err)
		}
		// The record keeps the change, so the same request finishes it on the running world.
		w.survival.ssm.invocation = []ssm.GetCommandInvocationOutput{allowlisted("survival", "add", "updated")}
		result, err = w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"})
		if err != nil || result.Allowlist != "updated" || !slices.Equal(result.Worlds, []string{"survival"}) {
			t.Fatalf("%s, asked again: result=%+v err=%v", tc.name, result, err)
		}
	}
}

func TestAllowlistFailsWhileAWorldIsStartingButKeepsTheChange(t *testing.T) {
	w := newWorlds(t)
	w.survival.ec2.states, w.survival.ec2.launch = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}, now.Add(-time.Minute)
	_, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "bedrock", Name: "Sample Player"})
	if err == nil || !strings.Contains(err.Error(), "world survival: the world is starting") {
		t.Fatalf("err=%v; the start already read its list, so the change must fail until the world is ready", err)
	}
	if w.record() != `[{"edition":"bedrock","uuid":"00000000-0000-0000-0009-01eed05d1009","name":"Sample Player"}]` {
		t.Fatalf("record %s; the record keeps the change so asking again finishes it", w.record())
	}
	if len(w.survival.ssm.commands) != 0 {
		t.Fatal("nothing may be sent to a world whose game does not answer yet")
	}
}

func TestAllowlistFailsWhenARunningWorldCannotBeRead(t *testing.T) {
	w := allowlistRig(t)
	w.creative.ec2.err = errors.New("throttled")
	if _, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"}); err == nil || !strings.Contains(err.Error(), "world creative: ") {
		t.Fatalf("err=%v; a world that may be running without the change must fail the request", err)
	}
}

func TestAllowlistNeverPutsThePlayerInAnError(t *testing.T) {
	// Errors are logged, and a refused write may repeat the list it was given.
	w := newWorlds(t)
	w.shared.putErr = errors.New("ValidationException: Value '[{\"name\":\"Steve\"}]' failed to satisfy constraint")
	_, err := w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"})
	if err == nil || strings.Contains(err.Error(), "Steve") {
		t.Fatalf("err=%v", err)
	}
	w = newWorlds(t)
	w.shared.params[allowlistName] = `[{"edition":"java","uuid":"069a79f4-44e9-4726-a5be-fca90e38aaf5","name":"Steve"}`
	_, err = w.worlds.Allowlist(context.Background(), AllowlistRequest{Operation: "remove", Edition: "java", Name: "Steve"})
	if err == nil || strings.Contains(err.Error(), "Steve") {
		t.Fatalf("err=%v", err)
	}
}

// lookupServer plays Mojang and GeyserMC.
func lookupServer(t *testing.T) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/users/profiles/minecraft/Steve":
			_, _ = w.Write([]byte(`{"id":"069a79f444e94726a5befca90e38aaf5","name":"Steve"}`))
		case "/users/profiles/minecraft/steve":
			_, _ = w.Write([]byte(`{"id":"069a79f444e94726a5befca90e38aaf5","name":"Steve"}`))
		case "/users/profiles/minecraft/Nobody":
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"path":"/users/profiles/minecraft/Nobody","errorMessage":"Couldn't find any profile with name Nobody"}`))
		case "/users/profiles/minecraft/Broken":
			_, _ = w.Write([]byte(`{"id":"not-a-uuid","name":"Broken"}`))
		case "/users/profiles/minecraft/Outage":
			w.WriteHeader(http.StatusServiceUnavailable)
		case "/users/profiles/minecraft/Elsewhere":
			http.Redirect(w, r, "/users/profiles/minecraft/Steve", http.StatusFound)
		case "/v2/xbox/xuid/Sample Player":
			_, _ = w.Write([]byte(`{"xuid": 2535400000000009}`))
		case "/v2/xbox/xuid/Nobody":
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"message": "Unable to find user in our cache. Please try specifying a different gamertag."}`))
		case "/v2/xbox/xuid/Uncached":
			w.WriteHeader(http.StatusServiceUnavailable)
			_, _ = w.Write([]byte(`{"message": "Unable to find user in our cache. Please try specifying a different gamertag."}`))
		case "/v2/xbox/xuid/Zero":
			_, _ = w.Write([]byte(`{"xuid": 0}`))
		case "/v2/link/bedrock/2535400000000009":
			_, _ = w.Write([]byte(`{}`))
		case "/v2/xbox/xuid/Linked Player":
			_, _ = w.Write([]byte(`{"xuid": 2535400000000001}`))
		case "/v2/link/bedrock/2535400000000001":
			_, _ = w.Write([]byte(`{"bedrock_id": 2535400000000001, "java_id": "069a79f4-44e9-4726-a5be-fca90e38aaf5", "java_name": "Steve", "last_name_update": 1700000000000}`))
		case "/v2/xbox/xuid/Link Missing":
			_, _ = w.Write([]byte(`{"xuid": 2535400000000002}`))
		case "/v2/link/bedrock/2535400000000002":
			w.WriteHeader(http.StatusNotFound)
		case "/v2/xbox/xuid/Link Outage":
			_, _ = w.Write([]byte(`{"xuid": 2535400000000003}`))
		case "/v2/link/bedrock/2535400000000003":
			w.WriteHeader(http.StatusServiceUnavailable)
		default:
			w.WriteHeader(http.StatusTeapot)
		}
	}))
}

func TestLookupResolvesJavaAndBedrockPlayers(t *testing.T) {
	server := lookupServer(t)
	defer server.Close()
	lookup := &PlayerLookup{Client: lookupClient(), Mojang: server.URL, Geyser: server.URL}
	player, err := lookup.Lookup(context.Background(), "java", "steve")
	if err != nil || player != steve {
		t.Fatalf("player=%+v err=%v; Mojang's exact name and dashed UUID are the record", player, err)
	}
	player, err = lookup.Lookup(context.Background(), "bedrock", "Sample Player")
	// 2535400000000009 is 0x000901eed05d1009: the XUID's top 16 bits and low 48 bits make the Floodgate UUID.
	if err != nil || player != (Player{UUID: "00000000-0000-0000-0009-01eed05d1009", Name: "Sample Player"}) {
		t.Fatalf("player=%+v err=%v", player, err)
	}
}

func TestLookupRefusesAGamertagLinkedToAJavaAccount(t *testing.T) {
	server := lookupServer(t)
	defer server.Close()
	lookup := &PlayerLookup{Client: lookupClient(), Mojang: server.URL, Geyser: server.URL}
	if _, err := lookup.Lookup(context.Background(), "bedrock", "Linked Player"); !errors.Is(err, ErrLinkedAccount) {
		t.Fatalf("err=%v; Floodgate admits a linked gamertag as its Java account", err)
	}
	// Not knowing whether the gamertag is linked is a lookup failure, never a missing player or a clean answer.
	for _, tc := range []struct{ name, want string }{{"Link Missing", "did not say whether"}, {"Link Outage", "GeyserMC answered 503"}} {
		_, err := lookup.Lookup(context.Background(), "bedrock", tc.name)
		if err == nil || errors.Is(err, ErrPlayerNotFound) || errors.Is(err, ErrLinkedAccount) || !strings.Contains(err.Error(), tc.want) {
			t.Fatalf("%s: err=%v", tc.name, err)
		}
	}
}

func TestLookupTellsAnUnknownPlayerFromAnOutage(t *testing.T) {
	server := lookupServer(t)
	defer server.Close()
	lookup := &PlayerLookup{Client: lookupClient(), Mojang: server.URL, Geyser: server.URL}
	for _, tc := range []struct{ edition, name string }{{"java", "Nobody"}, {"bedrock", "Nobody"}, {"bedrock", "Uncached"}, {"bedrock", "Zero"}} {
		if _, err := lookup.Lookup(context.Background(), tc.edition, tc.name); !errors.Is(err, ErrPlayerNotFound) {
			t.Fatalf("%s %s: err=%v", tc.edition, tc.name, err)
		}
	}
	for _, tc := range []struct{ name, want string }{{"Outage", "Mojang answered 503"}, {"Broken", "unusable profile"}, {"Elsewhere", "Mojang lookup failed"}} {
		_, err := lookup.Lookup(context.Background(), "java", tc.name)
		if err == nil || errors.Is(err, ErrPlayerNotFound) || !strings.Contains(err.Error(), tc.want) {
			t.Fatalf("%s: err=%v", tc.name, err)
		}
	}
	if _, err := lookup.Lookup(context.Background(), "java", "not a name"); !errors.Is(err, ErrBadAllowlistRequest) {
		t.Fatalf("err=%v", err)
	}
	if _, err := lookup.Lookup(context.Background(), "pocket", "Steve"); !errors.Is(err, ErrBadAllowlistRequest) {
		t.Fatalf("err=%v", err)
	}
}
