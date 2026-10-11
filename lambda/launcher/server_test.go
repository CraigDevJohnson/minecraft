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
	status  Status
	started bool
	err     error
	starts  int
}

func (f *fakeLauncher) Status(context.Context) (Status, error) { return f.status, f.err }

func (f *fakeLauncher) Start(context.Context) (Status, bool, error) {
	f.starts++
	return f.status, f.started, f.err
}

func serve(t *testing.T, server *Server, method, path string) (int, map[string]any) {
	t.Helper()
	request := httptest.NewRequest(method, path, nil)
	request.Header.Set(MemberHeader, "member@example.com")
	recorder := httptest.NewRecorder()
	server.Handler().ServeHTTP(recorder, request)
	var body map[string]any
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatalf("%s %s: %v", method, path, err)
	}
	if recorder.Header().Get("Content-Type") != "application/json" || recorder.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("headers %v", recorder.Header())
	}
	return recorder.Code, body
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
	for _, method := range []string{"GET /server", "POST /server/start"} {
		parts := strings.Fields(method)
		if code, body := serve(t, server, parts[0], parts[1]); code != 404 || body["code"] != "no_server" {
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
	for _, route := range [][2]string{{"POST", "/server"}, {"GET", "/server/start"}, {"GET", "/"}, {"DELETE", "/server"}} {
		recorder := httptest.NewRecorder()
		server.Handler().ServeHTTP(recorder, httptest.NewRequest(route[0], route[1], io.NopCloser(strings.NewReader(""))))
		if recorder.Code != http.StatusNotFound && recorder.Code != http.StatusMethodNotAllowed {
			t.Fatalf("%v answered %d", route, recorder.Code)
		}
	}
}
