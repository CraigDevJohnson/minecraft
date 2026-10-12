package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type fakeLauncher struct {
	status      Status
	started     bool
	stopped     bool
	err         error
	starts      int
	stops       int
	forced      bool
	allowlist   AllowlistResult
	allowlisted []AllowlistRequest
}

func (f *fakeLauncher) Status(context.Context) (Status, error) { return f.status, f.err }

func (f *fakeLauncher) Start(context.Context) (Status, bool, error) {
	f.starts++
	return f.status, f.started, f.err
}

func (f *fakeLauncher) Stop(_ context.Context, force bool) (Status, bool, error) {
	f.stops++
	f.forced = force
	return f.status, f.stopped, f.err
}

func (f *fakeLauncher) Allowlist(_ context.Context, request AllowlistRequest) (AllowlistResult, error) {
	f.allowlisted = append(f.allowlisted, request)
	return f.allowlist, f.err
}

func serve(t *testing.T, server *Server, method, path string) (int, map[string]any) {
	t.Helper()
	return serveBody(t, server, method, path, "")
}

func serveBody(t *testing.T, server *Server, method, path, body string) (int, map[string]any) {
	t.Helper()
	request := httptest.NewRequest(method, path, strings.NewReader(body))
	request.Header.Set(MemberHeader, "member@example.com")
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	var answer map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &answer); err != nil {
		t.Fatalf("%s %s: %v", method, path, err)
	}
	if recorder.Header().Get("Content-Type") != "application/json" || recorder.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("headers %v", recorder.Header())
	}
	return recorder.Code, answer
}

func quiet() (*slog.Logger, *bytes.Buffer) {
	var buffer bytes.Buffer
	return slog.New(slog.NewJSONHandler(&buffer, nil)), &buffer
}

func TestGetServer(t *testing.T) {
	log, _ := quiet()
	launcher := &fakeLauncher{status: Status{Name: "Family server", State: Ready, Connection: Connection{Java: &Endpoint{Address: "mc.example.com"}, Bedrock: &Endpoint{Address: "mc.example.com", Port: 19132}}}}
	code, body := serve(t, &Server{Launcher: launcher, Log: log}, "GET", "/server")
	encoded, _ := json.Marshal(body)
	if code != 200 || string(encoded) != `{"connection":{"bedrock":{"address":"mc.example.com","port":19132},"java":{"address":"mc.example.com"}},"name":"Family server","state":"ready"}` {
		t.Fatalf("%d %s", code, encoded)
	}
}

func TestNoServerIs404(t *testing.T) {
	log, _ := quiet()
	server := &Server{Launcher: &fakeLauncher{err: ErrNoServer}, Log: log}
	for _, method := range []string{"GET /server", "POST /server/start", "POST /server/stop", "POST /server/allowlist"} {
		parts := strings.Fields(method)
		body := ""
		if parts[1] == "/server/allowlist" {
			body = `{"operation":"add","edition":"java","name":"Steve"}`
		}
		if code, body := serveBody(t, server, parts[0], parts[1], body); code != 404 || body["code"] != "no_server" {
			t.Fatalf("%s: %d %v", method, code, body)
		}
	}
}

func TestUnknownCarriesTheLastKnownState(t *testing.T) {
	log, _ := quiet()
	launcher := &fakeLauncher{status: Status{Name: "Family server", State: Stopped}}
	server := &Server{Launcher: launcher, Log: log}
	serve(t, server, "GET", "/server")
	launcher.status, launcher.err = Status{Name: "Family server", State: Unknown}, errors.New("throttled")
	code, body := serve(t, server, "GET", "/server")
	if code != 200 || body["state"] != Unknown || body["last_known"] != Stopped {
		t.Fatalf("%d %v", code, body)
	}
	// A cold instance knows nothing to offer.
	code, body = serve(t, &Server{Launcher: launcher, Log: log}, "GET", "/server")
	if _, ok := body["last_known"]; code != 200 || ok {
		t.Fatalf("%d %v", code, body)
	}
}

func TestStartAnswers(t *testing.T) {
	cases := []struct {
		name    string
		started bool
		err     error
		code    int
	}{
		{"accepted", true, nil, 202},
		{"accepted without its follow-up", true, errors.New("throttled"), 202},
		{"already under way", false, nil, 200},
		{"unconfirmed", false, errors.New("EC2 refused"), 502},
	}
	for _, tc := range cases {
		log, logged := quiet()
		launcher := &fakeLauncher{status: Status{Name: "Family server", State: Starting}, started: tc.started, err: tc.err}
		lost := 0
		code, body := serve(t, &Server{Launcher: launcher, Log: log, StartFailed: func() { lost++ }}, "POST", "/server/start")
		if wantLost := map[bool]int{true: 1, false: 0}[tc.err != nil]; lost != wantLost {
			t.Fatalf("%s: counted %d failed starts", tc.name, lost)
		}
		if code != tc.code || launcher.starts != 1 {
			t.Fatalf("%s: %d %v", tc.name, code, body)
		}
		if code != 502 && body["state"] != Starting {
			t.Fatalf("%s: %v", tc.name, body)
		}
		if !strings.Contains(logged.String(), "member@example.com") {
			t.Fatalf("%s: the Member who asked was not logged", tc.name)
		}
	}
}

func TestOnlyTheContractRoutes(t *testing.T) {
	log, _ := quiet()
	server := &Server{Launcher: &fakeLauncher{}, Log: log}
	for _, route := range [][2]string{{"POST", "/server"}, {"GET", "/server/start"}, {"GET", "/server/stop"}, {"GET", "/server/allowlist"}, {"GET", "/"}, {"DELETE", "/server"}} {
		recorder := httptest.NewRecorder()
		server.Handler().ServeHTTP(recorder, httptest.NewRequest(route[0], route[1], io.NopCloser(strings.NewReader(""))))
		if recorder.Code != http.StatusNotFound && recorder.Code != http.StatusMethodNotAllowed {
			t.Fatalf("%v answered %d", route, recorder.Code)
		}
	}
}

func TestStopAnswers(t *testing.T) {
	cases := []struct {
		name    string
		body    string
		stopped bool
		err     error
		code    int
		answer  string
		force   bool
		lost    int
	}{
		{name: "accepted", stopped: true, code: 202, answer: "stopping"},
		{name: "accepted without its follow-up", stopped: true, err: errors.New("throttled"), code: 202, answer: "stopping", lost: 1},
		{name: "forced by the workflow", body: `{"force": true}`, stopped: true, code: 202, answer: "stopping", force: true},
		{name: "nothing to do", code: 200, answer: "stopping"},
		{name: "players online", err: ErrPlayersOnline{Players: 2}, code: 409, answer: "players_online"},
		{name: "players unknown", err: ErrPlayersUnknown, code: 409, answer: "players_unknown"},
		{name: "a start under way", err: ErrStarting, code: 409, answer: "starting"},
		{name: "unconfirmed", err: errors.New("SSM refused"), code: 502, answer: "stop_unconfirmed", lost: 1},
	}
	for _, tc := range cases {
		log, logged := quiet()
		launcher := &fakeLauncher{status: Status{Name: "Family server", State: Stopping}, stopped: tc.stopped, err: tc.err}
		lost := 0
		code, body := serveBody(t, &Server{Launcher: launcher, Log: log, StopFailed: func() { lost++ }}, "POST", "/server/stop", tc.body)
		if code != tc.code || launcher.stops != 1 || launcher.forced != tc.force || lost != tc.lost {
			t.Fatalf("%s: %d %v stops=%d forced=%v lost=%d", tc.name, code, body, launcher.stops, launcher.forced, lost)
		}
		if got := map[bool]any{true: body["state"], false: body["code"]}[code == 200 || code == 202]; got != tc.answer {
			t.Fatalf("%s: %v", tc.name, body)
		}
		if tc.answer == "players_online" && body["players"] != float64(2) {
			t.Fatalf("%s: %v", tc.name, body)
		}
		if !strings.Contains(logged.String(), "member@example.com") {
			t.Fatalf("%s: the Member who asked was not logged", tc.name)
		}
	}
}

func TestStopRefusesABodyItCannotRead(t *testing.T) {
	log, _ := quiet()
	launcher := &fakeLauncher{}
	if code, body := serveBody(t, &Server{Launcher: launcher, Log: log}, "POST", "/server/stop", "force"); code != 400 || body["code"] != "bad_request" || launcher.stops != 0 {
		t.Fatalf("%d %v stops=%d", code, body, launcher.stops)
	}
}

func TestAllowlistAnswers(t *testing.T) {
	log, _ := quiet()
	request := `{"operation":"add","edition":"java","name":"Steve"}`
	updated := AllowlistResult{Instance: "family", Edition: "java", Operation: "add", Allowlist: "updated"}
	for _, tc := range []struct {
		name     string
		launcher *fakeLauncher
		body     string
		code     int
		answer   string
		sent     int
	}{
		{"updated", &fakeLauncher{allowlist: updated}, request, 200, "updated", 1},
		{"not ready", &fakeLauncher{err: ErrServerNotReady{State: Stopped}}, request, 409, "not_ready", 1},
		{"unknown player", &fakeLauncher{err: ErrPlayerNotFound}, request, 404, "player_not_found", 1},
		{"adapter failure", &fakeLauncher{err: errors.New("the host's adapter refused the allowlist change")}, request, 502, "allowlist_failed", 1},
		{"bad operation", &fakeLauncher{allowlist: updated}, `{"operation":"ban","edition":"java","name":"Steve"}`, 400, "bad_request", 0},
		{"bad edition", &fakeLauncher{allowlist: updated}, `{"operation":"add","edition":"pocket","name":"Steve"}`, 400, "bad_request", 0},
		{"bad java name", &fakeLauncher{allowlist: updated}, `{"operation":"add","edition":"java","name":"Steve Jobs"}`, 400, "bad_request", 0},
		{"bad gamertag", &fakeLauncher{allowlist: updated}, `{"operation":"add","edition":"bedrock","name":"Steve\"; rm -rf /"}`, 400, "bad_request", 0},
		{"no body", &fakeLauncher{allowlist: updated}, "", 400, "bad_request", 0},
	} {
		code, body := serveBody(t, &Server{Launcher: tc.launcher, Log: log}, "POST", "/server/allowlist", tc.body)
		if code != tc.code || len(tc.launcher.allowlisted) != tc.sent {
			t.Fatalf("%s: %d %v (sent %d)", tc.name, code, body, len(tc.launcher.allowlisted))
		}
		if tc.code == 200 {
			if body["allowlist"] != tc.answer || body["instance"] != "family" || body["operation"] != "add" {
				t.Fatalf("%s: %v", tc.name, body)
			}
			continue
		}
		if body["code"] != tc.answer {
			t.Fatalf("%s: %v", tc.name, body)
		}
		if tc.answer == "not_ready" && body["state"] != Stopped {
			t.Fatalf("%s: a refusal names the state, got %v", tc.name, body)
		}
	}
}

func TestAllowlistPassesTheRequestThrough(t *testing.T) {
	log, _ := quiet()
	launcher := &fakeLauncher{allowlist: AllowlistResult{Instance: "family", Edition: "bedrock", Operation: "remove", Allowlist: "unchanged"}}
	code, body := serveBody(t, &Server{Launcher: launcher, Log: log}, "POST", "/server/allowlist", `{"operation":"remove","edition":"bedrock","name":"Sample Player"}`)
	if code != 200 || body["allowlist"] != "unchanged" {
		t.Fatalf("%d %v", code, body)
	}
	if want := (AllowlistRequest{Operation: "remove", Edition: "bedrock", Name: "Sample Player"}); len(launcher.allowlisted) != 1 || launcher.allowlisted[0] != want {
		t.Fatalf("sent %+v", launcher.allowlisted)
	}
}
