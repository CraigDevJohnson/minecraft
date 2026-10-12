package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

const allowlistOutput = `{"instance": "family", "edition": "java", "operation": "add", "allowlist": "updated"}`

var steve = Player{UUID: "069a79f4-44e9-4726-a5be-fca90e38aaf5", Name: "Steve"}

// allowlistRig is a ready server whose adapter answers the one allowlist command it gets.
func allowlistRig(t *testing.T) (*rig, *[]string) {
	t.Helper()
	r := newRig(ec2types.InstanceStateNameRunning, time.Hour)
	r.gameUp = true
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{{Status: ssmtypes.CommandInvocationStatusInProgress}, success(allowlistOutput)}
	looked := []string{}
	r.launcher.LookupPlayer = func(_ context.Context, edition, name string) (Player, error) {
		looked = append(looked, edition+":"+name)
		if name == "Nobody" {
			return Player{}, ErrPlayerNotFound
		}
		if name == "Linked Player" {
			return Player{}, ErrLinkedAccount
		}
		if name == "Outage" {
			return Player{}, errors.New("Mojang answered 503")
		}
		return steve, nil
	}
	return r, &looked
}

func TestAllowlistRunsTheAdapterWithThePlayersIdentity(t *testing.T) {
	r, looked := allowlistRig(t)
	result, err := r.launcher.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"})
	if err != nil || result != (AllowlistResult{Instance: "family", Edition: "java", Operation: "add", Allowlist: "updated"}) {
		t.Fatalf("result=%+v err=%v", result, err)
	}
	if len(*looked) != 1 || (*looked)[0] != "java:Steve" {
		t.Fatalf("looked up %v", *looked)
	}
	if len(r.ssm.commands) != 1 {
		t.Fatalf("commands %v", r.ssm.commands)
	}
	script := r.ssm.commands[0]
	encoded, _ := json.Marshal(steve)
	for _, want := range []string{
		"set -eu",
		`player="$(mktemp)"`,
		`trap 'rm -f "$player"' EXIT`,
		"printf '%s' '" + base64.StdEncoding.EncodeToString(encoded) + "' | base64 -d > \"$player\"",
		remoteCommand + ` allowlist family add --edition java --player-file "$player"`,
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("script lacks %q:\n%s", want, script)
		}
	}
	if strings.Contains(script, steve.UUID) || strings.Contains(script, "Steve") {
		t.Fatal("the identity must travel encoded, never as shell text")
	}
}

func TestAllowlistRefusesUnlessTheServerIsReady(t *testing.T) {
	for _, tc := range []struct {
		name  string
		rig   func(t *testing.T) *rig
		state string
	}{
		{"stopped", func(t *testing.T) *rig {
			r, _ := allowlistRig(t)
			r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameStopped}
			return r
		}, Stopped},
		{"game down", func(t *testing.T) *rig { r, _ := allowlistRig(t); r.gameUp = false; return r }, Maintenance},
		{"stopping", func(t *testing.T) *rig {
			r, _ := allowlistRig(t)
			r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameStopping}
			return r
		}, Stopping},
	} {
		r := tc.rig(t)
		_, err := r.launcher.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"})
		var notReady ErrServerNotReady
		if !errors.As(err, &notReady) || notReady.State != tc.state {
			t.Fatalf("%s: err=%v", tc.name, err)
		}
		if len(r.ssm.commands) != 0 {
			t.Fatalf("%s: nothing may be sent to a server that is not ready", tc.name)
		}
	}
}

func TestAllowlistNeverSendsAnUnknownPlayer(t *testing.T) {
	r, _ := allowlistRig(t)
	if _, err := r.launcher.Allowlist(context.Background(), AllowlistRequest{Operation: "remove", Edition: "bedrock", Name: "Nobody"}); !errors.Is(err, ErrPlayerNotFound) {
		t.Fatalf("err=%v", err)
	}
	if _, err := r.launcher.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Outage"}); err == nil || errors.Is(err, ErrPlayerNotFound) || !strings.Contains(err.Error(), "look up player") {
		t.Fatalf("err=%v", err)
	}
	for _, operation := range []string{"add", "remove"} {
		if _, err := r.launcher.Allowlist(context.Background(), AllowlistRequest{Operation: operation, Edition: "bedrock", Name: "Linked Player"}); !errors.Is(err, ErrLinkedAccount) {
			t.Fatalf("%s: err=%v; a linked gamertag's own entry would not change who may join", operation, err)
		}
	}
	if len(r.ssm.commands) != 0 {
		t.Fatal("a failed lookup must send nothing")
	}
}

func TestAllowlistRejectsBadRequestsBeforeLookingAnythingUp(t *testing.T) {
	r, looked := allowlistRig(t)
	for _, request := range []AllowlistRequest{
		{Operation: "ban", Edition: "java", Name: "Steve"},
		{Operation: "add", Edition: "pocket", Name: "Steve"},
		{Operation: "add", Edition: "java", Name: "St"},
		{Operation: "add", Edition: "java", Name: "Steve;whitelist off"},
		{Operation: "add", Edition: "bedrock", Name: " Sample Player"},
		{Operation: "add", Edition: "bedrock", Name: "Copper\"Tops"},
		{Operation: "add", Edition: "bedrock", Name: "SeventeenCharacters"},
		{Operation: "add", Edition: "bedrock", Name: ""},
	} {
		if _, err := r.launcher.Allowlist(context.Background(), request); !errors.Is(err, ErrBadAllowlistRequest) {
			t.Fatalf("%+v: err=%v", request, err)
		}
	}
	if len(*looked) != 0 || len(r.ssm.commands) != 0 {
		t.Fatal("a bad request must neither look up nor send anything")
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
		{"another change", success(`{"instance": "family", "edition": "java", "operation": "remove", "allowlist": "updated"}`)},
		{"another instance", success(`{"instance": "other", "edition": "java", "operation": "add", "allowlist": "updated"}`)},
		{"no verdict", success(`{"instance": "family", "edition": "java", "operation": "add"}`)},
		{"not json", success("allowlist: updated")},
	} {
		r, _ := allowlistRig(t)
		r.ssm.invocation = []ssm.GetCommandInvocationOutput{tc.answer}
		result, err := r.launcher.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"})
		if err == nil || result != (AllowlistResult{}) {
			t.Fatalf("%s: result=%+v err=%v", tc.name, result, err)
		}
	}
}

func TestAllowlistNeedsAServer(t *testing.T) {
	r, _ := allowlistRig(t)
	r.launcher.Config.HostID = ""
	if _, err := r.launcher.Allowlist(context.Background(), AllowlistRequest{Operation: "add", Edition: "java", Name: "Steve"}); !errors.Is(err, ErrNoServer) {
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
			_, _ = w.Write([]byte(`{"xuid": 2535428175563874}`))
		case "/v2/xbox/xuid/Nobody":
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"message": "Unable to find user in our cache. Please try specifying a different gamertag."}`))
		case "/v2/xbox/xuid/Zero":
			_, _ = w.Write([]byte(`{"xuid": 0}`))
		case "/v2/link/bedrock/2535428175563874":
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
	// 2535428175563874 is 0x000901f55fc20c62: the XUID's top 16 bits and low 48 bits make the Floodgate UUID.
	if err != nil || player != (Player{UUID: "00000000-0000-0000-0009-01f55fc20c62", Name: "Sample Player"}) {
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
	for _, tc := range []struct{ edition, name string }{{"java", "Nobody"}, {"bedrock", "Nobody"}, {"bedrock", "Zero"}} {
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
