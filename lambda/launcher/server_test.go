package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"slices"
	"strings"
	"testing"
	"time"
)

// fakeLauncher answers for two worlds. Each answers status, unless statuses holds a status of its own for it,
// and every start, stop and restore records the world it was asked for.
type fakeLauncher struct {
	status   Status
	statuses map[string]Status
	started  bool
	stopped  bool
	err      error
	starts   int
	stops    int
	forced   bool
	// asked lists the world of each start, stop and restore.
	asked       []string
	allowlist   AllowlistResult
	allowlisted []AllowlistRequest
	restored    []string
	mods        ModsList
	switched    []string
	// reading, when set, hears the world of each status read, and the read then waits for release.
	reading chan string
	release chan struct{}
}

func (f *fakeLauncher) Worlds() []string { return []string{"survival", "creative"} }

func (f *fakeLauncher) Status(_ context.Context, world string) (Status, error) {
	if f.reading != nil {
		f.reading <- world
		<-f.release
	}
	return f.answer(world)
}

// answer is a world's status, or the refusal of a world the launcher does not have.
func (f *fakeLauncher) answer(world string) (Status, error) {
	if !slices.Contains(f.Worlds(), world) {
		return Status{}, ErrUnknownWorld
	}
	if status, ok := f.statuses[world]; ok {
		return status, f.err
	}
	return f.status, f.err
}

// act answers a start, stop or restore of a world; did says whether the launcher takes it.
func (f *fakeLauncher) act(world string, did bool) (Status, bool, error) {
	f.asked = append(f.asked, world)
	status, err := f.answer(world)
	return status, did && !errors.Is(err, ErrUnknownWorld), err
}

func (f *fakeLauncher) Start(_ context.Context, world string) (Status, bool, error) {
	f.starts++
	return f.act(world, f.started)
}

func (f *fakeLauncher) Stop(_ context.Context, world string, force bool) (Status, bool, error) {
	f.stops++
	f.forced = force
	return f.act(world, f.stopped)
}

func (f *fakeLauncher) Allowlist(_ context.Context, request AllowlistRequest) (AllowlistResult, error) {
	f.allowlisted = append(f.allowlisted, request)
	return f.allowlist, f.err
}

func (f *fakeLauncher) Restore(_ context.Context, world, archive string) (Status, bool, error) {
	f.restored = append(f.restored, archive)
	return f.act(world, f.started)
}

func (f *fakeLauncher) Mods(context.Context) (ModsList, error) { return f.mods, f.err }

func (f *fakeLauncher) SwitchMod(_ context.Context, world, mod string, enabled bool) (ModsList, error) {
	f.switched = append(f.switched, fmt.Sprint(world, " ", mod, " ", enabled))
	return f.mods, f.err
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

// reachable is a world's status with the addresses a page shows.
func reachable(world, name, state string) Status {
	host := world + ".mc.example.com"
	return Status{Name: name, State: state, Connection: Connection{Java: &Endpoint{Address: host}, Bedrock: &Endpoint{Address: host, Port: 19132}}}
}

func TestGetServer(t *testing.T) {
	log, _ := quiet()
	launcher := &fakeLauncher{statuses: map[string]Status{
		"survival": reachable("survival", "Survival", Stopped),
		"creative": reachable("creative", "Creative", Ready),
	}}
	server := &Server{Launcher: launcher, Log: log}
	survival := `"connection":{"bedrock":{"address":"survival.mc.example.com","port":19132},"java":{"address":"survival.mc.example.com"}},`
	creative := `"connection":{"bedrock":{"address":"creative.mc.example.com","port":19132},"java":{"address":"creative.mc.example.com"}},`
	// Every world in the order the page shows them, and the first once more at the top for a page that reads no
	// worlds. Several may be ready at once.
	code, body := serve(t, server, "GET", "/server")
	encoded, _ := json.Marshal(body)
	want := `{` + survival + `"name":"Survival","state":"stopped","worlds":[` +
		`{` + survival + `"id":"survival","name":"Survival","state":"stopped"},` +
		`{` + creative + `"id":"creative","name":"Creative","state":"ready"}]}`
	if code != 200 || string(encoded) != want {
		t.Fatalf("%d %s", code, encoded)
	}
	// One world asked for by id answers in the shape a start or stop answers.
	code, body = serve(t, server, "GET", "/server?world=creative")
	encoded, _ = json.Marshal(body)
	if code != 200 || string(encoded) != `{`+creative+`"name":"Creative","state":"ready"}` {
		t.Fatalf("%d %s", code, encoded)
	}
	if code, body := serve(t, server, "GET", "/server?world=nether"); code != 409 || body["code"] != "unknown_world" {
		t.Fatalf("%d %v", code, body)
	}
}

func TestGetServerReadsTheWorldsSideBySide(t *testing.T) {
	log, _ := quiet()
	launcher := &fakeLauncher{status: Status{State: Stopped}, reading: make(chan string), release: make(chan struct{})}
	server := &Server{Launcher: launcher, Log: log}
	answered := make(chan int)
	go func() {
		recorder := httptest.NewRecorder()
		server.Handler().ServeHTTP(recorder, httptest.NewRequest("GET", "/server", nil))
		answered <- recorder.Code
	}()
	// Both reads are under way before either answers: the site waits seven seconds for the whole answer, and one
	// world whose game does not answer takes four of them.
	var reading []string
	for range launcher.Worlds() {
		select {
		case world := <-launcher.reading:
			reading = append(reading, world)
		case <-time.After(5 * time.Second):
			t.Fatalf("only %v was read while the first read waited", reading)
		}
	}
	close(launcher.release)
	if code := <-answered; code != 200 {
		t.Fatalf("answered %d", code)
	}
}

func TestNoServerIs404(t *testing.T) {
	log, _ := quiet()
	server := &Server{Launcher: &fakeLauncher{err: ErrNoServer}, Log: log, Operator: anyone}
	for _, method := range []string{"GET /server", "GET /server?world=creative", "POST /server/start", "POST /server/stop", "POST /server/restore"} {
		parts := strings.Fields(method)
		body := map[string]string{"/server/restore": `{"archive":"20261016T031500Z.tar.gz"}`}[parts[1]]
		if code, body := serveBody(t, server, parts[0], parts[1], body); code != 404 || body["code"] != "no_server" {
			t.Fatalf("%s: %d %v", method, code, body)
		}
	}
}

func TestUnknownCarriesEachWorldsLastKnownState(t *testing.T) {
	log, logged := quiet()
	launcher := &fakeLauncher{statuses: map[string]Status{"survival": {Name: "Survival", State: Stopped}, "creative": {Name: "Creative", State: Ready}}}
	server := &Server{Launcher: launcher, Log: log}
	serve(t, server, "GET", "/server")
	launcher.statuses, launcher.err = map[string]Status{"survival": {Name: "Survival", State: Unknown}, "creative": {Name: "Creative", State: Unknown}}, errors.New("throttled")
	code, body := serve(t, server, "GET", "/server")
	worlds, _ := body["worlds"].([]any)
	if code != 200 || body["state"] != Unknown || body["last_known"] != Stopped || len(worlds) != 2 {
		t.Fatalf("%d %v", code, body)
	}
	if creative := worlds[1].(map[string]any); creative["state"] != Unknown || creative["last_known"] != Ready {
		t.Fatalf("creative %v; a world's last known state is its own", creative)
	}
	if !strings.Contains(logged.String(), `"world":"survival"`) || !strings.Contains(logged.String(), `"world":"creative"`) {
		t.Fatalf("a failed read must be logged with its world: %s", logged.String())
	}
	if code, body := serve(t, server, "GET", "/server?world=creative"); code != 200 || body["last_known"] != Ready {
		t.Fatalf("%d %v", code, body)
	}
	// A cold instance knows nothing to offer.
	code, body = serve(t, &Server{Launcher: launcher, Log: log}, "GET", "/server")
	if _, ok := body["last_known"]; code != 200 || ok {
		t.Fatalf("%d %v", code, body)
	}
}

func TestARequestNamesItsWorldOrMeansTheFirst(t *testing.T) {
	archive := `"archive":"20261016T031500Z.tar.gz"`
	for _, route := range []string{"/server/start", "/server/stop", "/server/restore"} {
		// The archive is the one field a restore must carry; a start or stop ignores it.
		for body, want := range map[string]string{
			`{` + archive + `}`:                                  "survival",
			`{"world":"", ` + archive + `}`:                      "survival",
			`{"world":"survival", ` + archive + `}`:              "survival",
			`{"world":"creative", ` + archive + `}`:              "creative",
			`{"world":"creative","force":true, ` + archive + `}`: "creative",
		} {
			log, logged := quiet()
			launcher := &fakeLauncher{status: Status{State: Starting}, started: true, stopped: true}
			code, answer := serveBody(t, &Server{Launcher: launcher, Log: log, Operator: anyone}, "POST", route, body)
			if code != 202 || !slices.Equal(launcher.asked, []string{want}) {
				t.Fatalf("%s %s: %d %v asked %v", route, body, code, answer, launcher.asked)
			}
			if !strings.Contains(logged.String(), `"world":"`+want+`"`) {
				t.Fatalf("%s %s: the world was not logged: %s", route, body, logged.String())
			}
		}
		log, _ := quiet()
		launcher := &fakeLauncher{status: Status{State: Starting}, started: true, stopped: true}
		lost := 0
		server := &Server{Launcher: launcher, Log: log, Operator: anyone, StartFailed: func() { lost++ }, StopFailed: func() { lost++ }}
		if code, answer := serveBody(t, server, "POST", route, `{"world":"nether", `+archive+`}`); code != 409 || answer["code"] != "unknown_world" || lost != 0 {
			t.Fatalf("%s: %d %v (counted %d failures)", route, code, answer, lost)
		}
	}
	// Today's site sends a start and a stop no body at all.
	for _, route := range []string{"/server/start", "/server/stop"} {
		log, _ := quiet()
		launcher := &fakeLauncher{status: Status{State: Starting}, started: true, stopped: true}
		if code, answer := serve(t, &Server{Launcher: launcher, Log: log}, "POST", route); code != 202 || !slices.Equal(launcher.asked, []string{"survival"}) {
			t.Fatalf("%s: %d %v asked %v", route, code, answer, launcher.asked)
		}
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
		launcher := &fakeLauncher{status: Status{Name: "Survival", State: Starting}, started: tc.started, err: tc.err}
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
	for _, route := range [][2]string{{"POST", "/server"}, {"GET", "/server/start"}, {"GET", "/server/stop"}, {"GET", "/server/allowlist"}, {"GET", "/server/restore"}, {"GET", "/"}, {"DELETE", "/server"}, {"PUT", "/mods"}, {"DELETE", "/mods"}, {"GET", "/mods/creative"}, {"POST", "/mods/creative"}} {
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
		launcher := &fakeLauncher{status: Status{Name: "Survival", State: Stopping}, stopped: tc.stopped, err: tc.err}
		lost := 0
		code, body := serveBody(t, &Server{Launcher: launcher, Log: log, StopFailed: func() { lost++ }, Operator: anyone}, "POST", "/server/stop", tc.body)
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

func TestStartAndStopRefuseABodyTheyCannotRead(t *testing.T) {
	log, _ := quiet()
	launcher := &fakeLauncher{}
	if code, body := serveBody(t, &Server{Launcher: launcher, Log: log}, "POST", "/server/stop", "force"); code != 400 || body["code"] != "bad_request" || launcher.stops != 0 {
		t.Fatalf("%d %v stops=%d", code, body, launcher.stops)
	}
	if code, body := serveBody(t, &Server{Launcher: launcher, Log: log}, "POST", "/server/start", "creative"); code != 400 || body["code"] != "bad_request" || launcher.starts != 0 {
		t.Fatalf("%d %v starts=%d; a start that names its world badly must not start the first", code, body, launcher.starts)
	}
}

func TestAllowlistAnswers(t *testing.T) {
	log, _ := quiet()
	request := `{"operation":"add","edition":"java","name":"Steve"}`
	updated := AllowlistResult{Edition: "java", Operation: "add", Allowlist: "updated", Worlds: []string{"creative"}}
	for _, tc := range []struct {
		name     string
		launcher *fakeLauncher
		body     string
		code     int
		answer   string
		sent     int
	}{
		{"updated", &fakeLauncher{allowlist: updated}, request, 200, "updated", 1},
		{"unknown player", &fakeLauncher{err: ErrPlayerNotFound}, request, 404, "player_not_found", 1},
		{"linked gamertag", &fakeLauncher{err: ErrLinkedAccount}, request, 409, "linked_account", 1},
		{"adapter failure", &fakeLauncher{err: errors.New("the host's adapter refused the allowlist change")}, request, 502, "allowlist_failed", 1},
		{"bad operation", &fakeLauncher{allowlist: updated}, `{"operation":"ban","edition":"java","name":"Steve"}`, 400, "bad_request", 0},
		{"bad edition", &fakeLauncher{allowlist: updated}, `{"operation":"add","edition":"pocket","name":"Steve"}`, 400, "bad_request", 0},
		{"bad java name", &fakeLauncher{allowlist: updated}, `{"operation":"add","edition":"java","name":"Steve Jobs"}`, 400, "bad_request", 0},
		{"bad gamertag", &fakeLauncher{allowlist: updated}, `{"operation":"add","edition":"bedrock","name":"Steve\"; rm -rf /"}`, 400, "bad_request", 0},
		{"no body", &fakeLauncher{allowlist: updated}, "", 400, "bad_request", 0},
	} {
		code, body := serveBody(t, &Server{Launcher: tc.launcher, Log: log, Operator: anyone}, "POST", "/server/allowlist", tc.body)
		if code != tc.code || len(tc.launcher.allowlisted) != tc.sent {
			t.Fatalf("%s: %d %v (sent %d)", tc.name, code, body, len(tc.launcher.allowlisted))
		}
		if tc.code == 200 {
			// The answer names the running worlds the change reached, and no player.
			if encoded, _ := json.Marshal(body); string(encoded) != `{"allowlist":"updated","edition":"java","operation":"add","worlds":["creative"]}` {
				t.Fatalf("%s: %s", tc.name, encoded)
			}
			continue
		}
		if body["code"] != tc.answer {
			t.Fatalf("%s: %v", tc.name, body)
		}
	}
}

func TestAllowlistWithEveryWorldStoppedAnswersNoWorlds(t *testing.T) {
	log, logged := quiet()
	launcher := &fakeLauncher{allowlist: AllowlistResult{Edition: "java", Operation: "add", Allowlist: "updated", Worlds: []string{}}}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest("POST", "/server/allowlist", strings.NewReader(`{"operation":"add","edition":"java","name":"Steve"}`))
	(&Server{Launcher: launcher, Log: log, Operator: anyone}).Handler().ServeHTTP(recorder, request)
	if recorder.Code != 200 || strings.TrimSpace(recorder.Body.String()) != `{"edition":"java","operation":"add","allowlist":"updated","worlds":[]}` {
		t.Fatalf("%d %s", recorder.Code, recorder.Body.String())
	}
	if strings.Contains(logged.String(), "Steve") {
		t.Fatalf("the player's name must not be logged: %s", logged.String())
	}
}

func TestAllowlistPassesTheRequestThrough(t *testing.T) {
	log, _ := quiet()
	launcher := &fakeLauncher{allowlist: AllowlistResult{Edition: "bedrock", Operation: "remove", Allowlist: "unchanged", Worlds: []string{}}}
	code, body := serveBody(t, &Server{Launcher: launcher, Log: log, Operator: anyone}, "POST", "/server/allowlist", `{"operation":"remove","edition":"bedrock","name":"Sample Player"}`)
	if code != 200 || body["allowlist"] != "unchanged" {
		t.Fatalf("%d %v", code, body)
	}
	if want := (AllowlistRequest{Operation: "remove", Edition: "bedrock", Name: "Sample Player"}); len(launcher.allowlisted) != 1 || launcher.allowlisted[0] != want {
		t.Fatalf("sent %+v", launcher.allowlisted)
	}
}

func TestRestoreAnswers(t *testing.T) {
	request := `{"archive":"20261016T031500Z.tar.gz"}`
	for _, tc := range []struct {
		name     string
		launcher *fakeLauncher
		body     string
		code     int
		answer   string
		sent     int
		lost     int
	}{
		{"accepted", &fakeLauncher{status: Status{State: Starting}, started: true}, request, 202, "starting", 1, 0},
		{"accepted without its follow-up", &fakeLauncher{status: Status{State: Starting}, started: true, err: errors.New("throttled")}, request, 202, "starting", 1, 1},
		{"ready", &fakeLauncher{status: Status{State: Ready}}, request, 409, "not_stopped", 1, 0},
		{"a restore under way", &fakeLauncher{status: Status{State: Starting}}, request, 409, "not_stopped", 1, 0},
		{"unconfirmed", &fakeLauncher{status: Status{State: Unknown}, err: errors.New("EC2 refused")}, request, 502, "restore_unconfirmed", 1, 1},
		{"refused by the launcher's own check", &fakeLauncher{err: ErrBadRestoreRequest}, request, 400, "bad_request", 1, 0},
		{"no body", &fakeLauncher{}, "", 400, "bad_request", 0, 0},
		{"not JSON", &fakeLauncher{}, "20261016T031500Z.tar.gz", 400, "bad_request", 0, 0},
		{"no archive", &fakeLauncher{}, `{"key":"instances/survival/crafty/20261016T031500Z.tar.gz"}`, 400, "bad_request", 0, 0},
		{"an S3 key", &fakeLauncher{}, `{"archive":"instances/survival/crafty/20261016T031500Z.tar.gz"}`, 400, "bad_request", 0, 0},
		{"a path out", &fakeLauncher{}, `{"archive":"../other/crafty/20261016T031500Z.tar.gz"}`, 400, "bad_request", 0, 0},
		{"shell text", &fakeLauncher{}, `{"archive":"20261016T031500Z.tar.gz'; reboot; '"}`, 400, "bad_request", 0, 0},
		{"a trailing newline", &fakeLauncher{}, `{"archive":"20261016T031500Z.tar.gz\n"}`, 400, "bad_request", 0, 0},
		{"its checksum", &fakeLauncher{}, `{"archive":"20261016T031500Z.tar.gz.sha256"}`, 400, "bad_request", 0, 0},
		{"no time zone", &fakeLauncher{}, `{"archive":"20261016T031500.tar.gz"}`, 400, "bad_request", 0, 0},
		{"over 1 KiB", &fakeLauncher{}, `{"archive":"20261016T031500Z.tar.gz","padding":"` + strings.Repeat("x", 1<<10) + `"}`, 400, "bad_request", 0, 0},
	} {
		log, logged := quiet()
		lost := 0
		code, body := serveBody(t, &Server{Launcher: tc.launcher, Log: log, StartFailed: func() { lost++ }, Operator: anyone}, "POST", "/server/restore", tc.body)
		if code != tc.code || len(tc.launcher.restored) != tc.sent || lost != tc.lost {
			t.Fatalf("%s: %d %v (sent %d, counted %d failures)", tc.name, code, body, len(tc.launcher.restored), lost)
		}
		if got := map[bool]any{true: body["state"], false: body["code"]}[code == 202]; got != tc.answer {
			t.Fatalf("%s: %v", tc.name, body)
		}
		if tc.answer == "not_stopped" && body["state"] != tc.launcher.status.State {
			t.Fatalf("%s: a refusal names the state, got %v", tc.name, body)
		}
		if tc.sent == 1 && tc.launcher.restored[0] != "20261016T031500Z.tar.gz" {
			t.Fatalf("%s: sent %v", tc.name, tc.launcher.restored)
		}
		if tc.code != 400 {
			if !strings.Contains(logged.String(), "member@example.com") || !strings.Contains(logged.String(), "20261016T031500Z.tar.gz") {
				t.Fatalf("%s: the Member who asked and the archive were not logged: %s", tc.name, logged.String())
			}
		}
	}
}

func TestOnlyAnOperatorMayRestore(t *testing.T) {
	for name, operator := range map[string]func(*http.Request) bool{
		"a site's role":   func(*http.Request) bool { return false },
		"no caller check": nil,
	} {
		log, _ := quiet()
		launcher := &fakeLauncher{status: Status{State: Starting}, started: true}
		server := &Server{Launcher: launcher, Log: log, Operator: operator}
		// The caller is checked before the body is read: a site learns nothing about what a body must hold.
		for _, body := range []string{`{"archive":"20261016T031500Z.tar.gz"}`, "not json"} {
			if code, answer := serveBody(t, server, "POST", "/server/restore", body); code != 403 || answer["code"] != "forbidden" || len(launcher.restored) != 0 {
				t.Fatalf("%s: restore answered %d %v (sent %d)", name, code, answer, len(launcher.restored))
			}
		}
	}
}

func TestModsAnswers(t *testing.T) {
	list := ModsList{Worlds: []WorldMods{{ID: "creative", Name: "Creative", Mods: []ModSwitch{{
		ID:      "fawe",
		Mod:     Mod{Name: "FastAsyncWorldEdit", Version: "2.16.2", Summary: "Bulk building tools.", Bedrock: "Commands and the wand work."},
		Default: true,
	}}}}}
	shape := `{"worlds":[{"id":"creative","name":"Creative","mods":[{"id":"fawe","name":"FastAsyncWorldEdit","version":"2.16.2","summary":"Bulk building tools.","bedrock":"Commands and the wand work.","default":true,"enabled":false}]}]}`
	// A site may use both routes: it shows the page only to a Member with its minecraft-admin grant.
	for name, operator := range map[string]func(*http.Request) bool{
		"a site's role":   func(*http.Request) bool { return false },
		"no caller check": nil,
		"an operator":     anyone,
	} {
		log, logged := quiet()
		launcher := &fakeLauncher{mods: list}
		server := &Server{Launcher: launcher, Log: log, Operator: operator}
		for _, route := range [][3]string{{"GET", "/mods", ""}, {"POST", "/mods", `{"world":"creative","mod":"fawe","enabled":false}`}} {
			recorder := httptest.NewRecorder()
			request := httptest.NewRequest(route[0], route[1], strings.NewReader(route[2]))
			request.Header.Set(MemberHeader, "member@example.com")
			server.Handler().ServeHTTP(recorder, request)
			if recorder.Code != 200 || strings.TrimSpace(recorder.Body.String()) != shape {
				t.Fatalf("%s: %s %s answered %d %s", name, route[0], route[1], recorder.Code, recorder.Body.String())
			}
		}
		if !slices.Equal(launcher.switched, []string{"creative fawe false"}) {
			t.Fatalf("%s: switched %v", name, launcher.switched)
		}
		// A switch is logged as its world, mod and new setting. The launcher reads no identity header for it.
		for _, want := range []string{`"msg":"mod switch"`, `"world":"creative"`, `"mod":"fawe"`, `"enabled":false`} {
			if !strings.Contains(logged.String(), want) {
				t.Fatalf("%s: the log lacks %s: %s", name, want, logged.String())
			}
		}
		if strings.Contains(logged.String(), "member@example.com") {
			t.Fatalf("%s: a switch must not log who asked: %s", name, logged.String())
		}
	}
}

func TestModsRefusals(t *testing.T) {
	for _, tc := range []struct {
		name     string
		method   string
		body     string
		err      error
		code     int
		answer   string
		switched int
	}{
		{"an unknown world", "POST", `{"world":"nether","mod":"fawe","enabled":true}`, ErrUnknownWorld, 409, "unknown_world", 1},
		{"a mod the world does not list", "POST", `{"world":"survival","mod":"fawe","enabled":true}`, ErrUnknownMod, 409, "unknown_mod", 1},
		{"a switch that could not be kept", "POST", `{"world":"creative","mod":"fawe","enabled":true}`, errors.New("write the mods: ThrottlingException"), 502, "mods_unconfirmed", 1},
		{"a list that could not be read", "GET", "", errors.New("read the mods: throttled"), 502, "mods_unconfirmed", 0},
		{"no setting", "POST", `{"world":"creative","mod":"fawe"}`, nil, 400, "bad_request", 0},
		{"a setting that is not on or off", "POST", `{"world":"creative","mod":"fawe","enabled":"no"}`, nil, 400, "bad_request", 0},
		{"no body", "POST", "", nil, 400, "bad_request", 0},
		{"not JSON", "POST", "fawe off", nil, 400, "bad_request", 0},
		{"over 1 KiB", "POST", `{"world":"creative","mod":"fawe","enabled":true,"padding":"` + strings.Repeat("x", 1<<10) + `"}`, nil, 400, "bad_request", 0},
	} {
		log, _ := quiet()
		launcher := &fakeLauncher{err: tc.err}
		code, body := serveBody(t, &Server{Launcher: launcher, Log: log}, tc.method, "/mods", tc.body)
		if code != tc.code || body["code"] != tc.answer || len(launcher.switched) != tc.switched {
			t.Fatalf("%s: %d %v (switched %v)", tc.name, code, body, launcher.switched)
		}
	}
}

// anyone stands in for an operator's signed request.
func anyone(*http.Request) bool { return true }

func TestASiteCannotForceAStopOrChangeTheAllowlist(t *testing.T) {
	for name, operator := range map[string]func(*http.Request) bool{
		"a site's role":   func(*http.Request) bool { return false },
		"no caller check": nil,
	} {
		log, _ := quiet()
		launcher := &fakeLauncher{status: Status{Name: "Survival", State: Stopping}, stopped: true}
		server := &Server{Launcher: launcher, Log: log, Operator: operator}
		if code, body := serveBody(t, server, "POST", "/server/allowlist", `{"operation":"add","edition":"java","name":"Steve"}`); code != 403 || body["code"] != "forbidden" || len(launcher.allowlisted) != 0 {
			t.Fatalf("%s: allowlist answered %d %v (sent %d)", name, code, body, len(launcher.allowlisted))
		}
		if code, body := serveBody(t, server, "POST", "/server/stop", `{"force": true}`); code != 403 || body["code"] != "forbidden" || launcher.stops != 0 {
			t.Fatalf("%s: a forced stop answered %d %v (stops %d)", name, code, body, launcher.stops)
		}
		if code, body := serveBody(t, server, "POST", "/server/stop", ""); code != 202 || launcher.stops != 1 || launcher.forced {
			t.Fatalf("%s: a plain stop answered %d %v (stops %d, forced %v)", name, code, body, launcher.stops, launcher.forced)
		}
	}
}

func TestOperatorCallerIsAnySignedCallerButASite(t *testing.T) {
	sites := RoleNames("arn:aws:iam::111122223333:role/site-dev-server, arn:aws:iam::111122223333:role/lobby/site-prod-server")
	if len(sites) != 2 || !sites["site-dev-server"] || !sites["site-prod-server"] {
		t.Fatalf("site roles %v", sites)
	}
	for _, tc := range []struct {
		arn   string
		sites map[string]bool
		want  bool
	}{
		{"arn:aws:sts::111122223333:assumed-role/site-dev-server/lobby", sites, false},
		{"arn:aws:sts::111122223333:assumed-role/site-prod-server/lobby", sites, false},
		{"arn:aws:sts::111122223333:assumed-role/minecraft-server-control/minecraft-server-stop-1", sites, true},
		{"arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_WorkloadsAdmin_0123456789abcdef/craig", sites, true},
		{"arn:aws:iam::111122223333:user/operator", sites, true},
		{"arn:aws:sts::111122223333:assumed-role//x", sites, false},
		{"", sites, false},
		{"arn:aws:sts::111122223333:assumed-role/minecraft-server-control/x", map[string]bool{}, false},
	} {
		if got := OperatorCaller(tc.arn, tc.sites); got != tc.want {
			t.Fatalf("%q with sites %v: %v, want %v", tc.arn, tc.sites, got, tc.want)
		}
	}
}
