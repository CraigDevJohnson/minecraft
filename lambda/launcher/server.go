package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
)

// MemberHeader names the Member the site acts for. Only the site can reach this function (the-lobby ADR 0006),
// so the header is trusted as who asked, and logged.
const MemberHeader = "X-Lobby-Member"

// Server is the web side the site and the operator's workflow call: GET /server, POST /server/start,
// POST /server/stop, POST /server/allowlist, POST /server/restore, GET /mods and POST /mods.
type Server struct {
	Launcher interface {
		// Worlds answers the worlds' ids in the order the page shows them. The first is the default.
		Worlds() []string
		Status(ctx context.Context, world string) (Status, error)
		Start(ctx context.Context, world string) (Status, bool, error)
		Stop(ctx context.Context, world string, force bool) (Status, bool, error)
		Allowlist(context.Context, AllowlistRequest) (AllowlistResult, error)
		Restore(ctx context.Context, world, archive string) (Status, bool, error)
		Mods(context.Context) (ModsList, error)
		SwitchMod(ctx context.Context, world, mod string, enabled bool) (ModsList, error)
	}
	Log *slog.Logger
	// StartFailed and StopFailed count a request that failed or could not hand off its follow-up, for the
	// alarm; a restore counts as a start. The request still answers the caller normally, so Lambda's own error
	// count never sees it.
	StartFailed func()
	StopFailed  func()
	// Operator reports whether the request came from an operator rather than a Lobby site: only an operator
	// may change the allowlist, restore an archive or force a stop. Nil admits nobody.
	Operator func(*http.Request) bool

	mu        sync.Mutex
	lastKnown map[string]string
}

// ServerStatus is the answer to GET /server: every world in the order the page shows them, and the first
// world's status once more at the top, so a page that reads no worlds keeps working.
type ServerStatus struct {
	Status
	Worlds []WorldStatus `json:"worlds"`
}

type WorldStatus struct {
	ID string `json:"id"`
	Status
}

// OperatorCaller reports whether the IAM caller a Function URL request was signed by is an operator. The URL's
// resource policy admits the sites' roles and the server control role; anyone else who can call it holds an
// administrator's own IAM grant. So every signed caller except a site's role is an operator, and an unsigned
// request, or a function that does not know its sites, admits nobody.
func OperatorCaller(userARN string, siteRoles map[string]bool) bool {
	if userARN == "" || len(siteRoles) == 0 {
		return false
	}
	if _, rest, ok := strings.Cut(userARN, ":assumed-role/"); ok {
		role, _, _ := strings.Cut(rest, "/")
		return role != "" && !siteRoles[role]
	}
	return true
}

// RoleNames turns a comma-separated list of role ARNs into the role names an assumed-role caller carries.
func RoleNames(arns string) map[string]bool {
	names := map[string]bool{}
	for _, arn := range strings.Split(arns, ",") {
		if _, path, ok := strings.Cut(strings.TrimSpace(arn), ":role/"); ok {
			if name := path[strings.LastIndex(path, "/")+1:]; name != "" {
				names[name] = true
			}
		}
	}
	return names
}

func (s *Server) operator(r *http.Request) bool {
	return s.Operator != nil && s.Operator(r)
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /server", s.status)
	mux.HandleFunc("POST /server/start", s.start)
	mux.HandleFunc("POST /server/stop", s.stop)
	mux.HandleFunc("POST /server/allowlist", s.allowlist)
	mux.HandleFunc("POST /server/restore", s.restore)
	mux.HandleFunc("GET /mods", s.mods)
	mux.HandleFunc("POST /mods", s.switchMod)
	return mux
}

// world answers the world a request names, or the first world when it names none, which is what a site that
// knows nothing of worlds asks for.
func (s *Server) world(named string) string {
	if named == "" {
		return s.Launcher.Worlds()[0]
	}
	return named
}

// optionalBody decodes a request's JSON body into request and reports whether it could. No body at all is fine.
func optionalBody(r *http.Request, request any) bool {
	body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<10))
	return len(bytes.TrimSpace(body)) == 0 || json.Unmarshal(body, request) == nil
}

// status answers every world, or with ?world=<id> that world alone in the shape a start or stop answers. The
// worlds are read side by side, so the slowest one decides how long the answer takes, not their sum: the site
// waits seven seconds, and a world whose game does not answer takes four.
func (s *Server) status(w http.ResponseWriter, r *http.Request) {
	ids := s.Launcher.Worlds()
	one := r.URL.Query().Get("world")
	if one != "" {
		ids = []string{one}
	}
	statuses, errs := make([]Status, len(ids)), make([]error, len(ids))
	var reads sync.WaitGroup
	for i, id := range ids {
		reads.Go(func() { statuses[i], errs[i] = s.Launcher.Status(r.Context(), id) })
	}
	reads.Wait()
	worlds := make([]WorldStatus, len(ids))
	for i, id := range ids {
		switch {
		case errors.Is(errs[i], ErrUnknownWorld):
			writeJSON(w, http.StatusConflict, map[string]string{"code": "unknown_world"})
			return
		case errors.Is(errs[i], ErrNoServer):
			writeJSON(w, http.StatusNotFound, map[string]string{"code": "no_server"})
			return
		case errs[i] != nil:
			s.Log.Error("status", "world", id, "err", errs[i])
		}
		worlds[i] = WorldStatus{ID: id, Status: s.remember(id, statuses[i])}
	}
	if one != "" {
		writeJSON(w, http.StatusOK, worlds[0].Status)
		return
	}
	writeJSON(w, http.StatusOK, ServerStatus{Status: worlds[0].Status, Worlds: worlds})
}

// start starts a world. The body may carry {"world": "creative"}.
func (s *Server) start(w http.ResponseWriter, r *http.Request) {
	member := r.Header.Get(MemberHeader)
	var request struct {
		World string `json:"world"`
	}
	if !optionalBody(r, &request) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
		return
	}
	world := s.world(request.World)
	status, started, err := s.Launcher.Start(r.Context(), world)
	if errors.Is(err, ErrUnknownWorld) {
		s.Log.Info("start refused", "member", member, "world", world, "reason", "unknown_world")
		writeJSON(w, http.StatusConflict, map[string]string{"code": "unknown_world"})
		return
	}
	if errors.Is(err, ErrNoServer) {
		writeJSON(w, http.StatusNotFound, map[string]string{"code": "no_server"})
		return
	}
	if started {
		s.Log.Info("start", "member", member, "world", world)
		if err != nil {
			s.Log.Error("start follow-up", "member", member, "world", world, "err", err)
			s.countFailure()
		}
		writeJSON(w, http.StatusAccepted, s.remember(world, status))
		return
	}
	if err != nil {
		// The site shows anything but 200, 202 and 404 as a status it can't confirm.
		s.Log.Error("start", "member", member, "world", world, "err", err)
		s.countFailure()
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "start_unconfirmed"})
		return
	}
	s.Log.Info("start unchanged", "member", member, "world", world, "state", status.State)
	writeJSON(w, http.StatusOK, s.remember(world, status))
}

// stop drains and stops a world. The body may carry {"world": "creative"}, and {"force": true}, which only an
// operator may send, to stop over players or a game that does not answer.
func (s *Server) stop(w http.ResponseWriter, r *http.Request) {
	member := r.Header.Get(MemberHeader)
	var request struct {
		World string `json:"world"`
		Force bool   `json:"force"`
	}
	if !optionalBody(r, &request) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
		return
	}
	world := s.world(request.World)
	if request.Force && !s.operator(r) {
		s.Log.Info("stop refused", "member", member, "world", world, "reason", "force_not_allowed")
		writeJSON(w, http.StatusForbidden, map[string]string{"code": "forbidden"})
		return
	}
	status, stopped, err := s.Launcher.Stop(r.Context(), world, request.Force)
	var online ErrPlayersOnline
	switch {
	case errors.Is(err, ErrUnknownWorld):
		s.Log.Info("stop refused", "member", member, "world", world, "reason", "unknown_world")
		writeJSON(w, http.StatusConflict, map[string]string{"code": "unknown_world"})
	case errors.Is(err, ErrNoServer):
		writeJSON(w, http.StatusNotFound, map[string]string{"code": "no_server"})
	case stopped:
		s.Log.Info("stop", "member", member, "world", world, "force", request.Force)
		if err != nil {
			s.Log.Error("stop follow-up", "member", member, "world", world, "err", err)
			s.countStopFailure()
		}
		writeJSON(w, http.StatusAccepted, s.remember(world, status))
	case errors.Is(err, ErrStarting):
		s.Log.Info("stop refused", "member", member, "world", world, "reason", "starting")
		writeJSON(w, http.StatusConflict, map[string]string{"code": "starting"})
	case errors.As(err, &online):
		s.Log.Info("stop refused", "member", member, "world", world, "reason", "players_online", "players", online.Players)
		writeJSON(w, http.StatusConflict, map[string]any{"code": "players_online", "players": online.Players})
	case errors.Is(err, ErrPlayersUnknown):
		s.Log.Info("stop refused", "member", member, "world", world, "reason", "players_unknown")
		writeJSON(w, http.StatusConflict, map[string]string{"code": "players_unknown"})
	case err != nil:
		// The site shows anything but 200, 202, 404 and 409 as a status it can't confirm.
		s.Log.Error("stop", "member", member, "world", world, "err", err)
		s.countStopFailure()
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "stop_unconfirmed"})
	default:
		s.Log.Info("stop unchanged", "member", member, "world", world, "state", status.State)
		writeJSON(w, http.StatusOK, s.remember(world, status))
	}
}

// allowlist adds or removes one player on the family's allowlist, and on each running world, for an operator,
// never a site. It takes no world. The player's name is not logged; the answer carries none either.
func (s *Server) allowlist(w http.ResponseWriter, r *http.Request) {
	member := r.Header.Get(MemberHeader)
	if !s.operator(r) {
		s.Log.Info("allowlist refused", "member", member, "reason", "not_an_operator")
		writeJSON(w, http.StatusForbidden, map[string]string{"code": "forbidden"})
		return
	}
	var request AllowlistRequest
	body, _ := io.ReadAll(io.LimitReader(r.Body, 4<<10))
	if json.Unmarshal(body, &request) != nil || ValidateAllowlistRequest(request) != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
		return
	}
	result, err := s.Launcher.Allowlist(r.Context(), request)
	switch {
	case errors.Is(err, ErrBadAllowlistRequest):
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
	case errors.Is(err, ErrPlayerNotFound):
		s.Log.Info("allowlist refused", "member", member, "reason", "player_not_found", "edition", request.Edition)
		writeJSON(w, http.StatusNotFound, map[string]string{"code": "player_not_found"})
	case errors.Is(err, ErrLinkedAccount):
		s.Log.Info("allowlist refused", "member", member, "reason", "linked_account", "edition", request.Edition)
		writeJSON(w, http.StatusConflict, map[string]string{"code": "linked_account"})
	case err != nil:
		s.Log.Error("allowlist", "member", member, "edition", request.Edition, "operation", request.Operation, "err", err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "allowlist_failed"})
	default:
		s.Log.Info("allowlist", "member", member, "edition", result.Edition, "operation", result.Operation, "result", result.Allowlist, "worlds", result.Worlds)
		writeJSON(w, http.StatusOK, result)
	}
}

// restore puts back the archive an operator names, never a site, in place of a stopped world, then starts its
// game. The body is {"archive": "20261016T031500Z.tar.gz"}, the archive's name in S3, which is not private and
// is logged, and may carry {"world": "creative"} beside it.
func (s *Server) restore(w http.ResponseWriter, r *http.Request) {
	member := r.Header.Get(MemberHeader)
	if !s.operator(r) {
		s.Log.Info("restore refused", "member", member, "reason", "not_an_operator")
		writeJSON(w, http.StatusForbidden, map[string]string{"code": "forbidden"})
		return
	}
	var request struct {
		World   string `json:"world"`
		Archive string `json:"archive"`
	}
	body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<10))
	if json.Unmarshal(body, &request) != nil || !archiveName.MatchString(request.Archive) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
		return
	}
	world := s.world(request.World)
	status, started, err := s.Launcher.Restore(r.Context(), world, request.Archive)
	switch {
	case errors.Is(err, ErrUnknownWorld):
		s.Log.Info("restore refused", "member", member, "world", world, "archive", request.Archive, "reason", "unknown_world")
		writeJSON(w, http.StatusConflict, map[string]string{"code": "unknown_world"})
	case errors.Is(err, ErrNoServer):
		writeJSON(w, http.StatusNotFound, map[string]string{"code": "no_server"})
	case errors.Is(err, ErrBadRestoreRequest):
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
	case started:
		s.Log.Info("restore", "member", member, "world", world, "archive", request.Archive)
		if err != nil {
			s.Log.Error("restore follow-up", "member", member, "world", world, "archive", request.Archive, "err", err)
			s.countFailure()
		}
		writeJSON(w, http.StatusAccepted, s.remember(world, status))
	case err != nil:
		s.Log.Error("restore", "member", member, "world", world, "archive", request.Archive, "err", err)
		s.countFailure()
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "restore_unconfirmed"})
	default:
		// Only a stopped world, or one whose start or restore failed, is restored: anything else is stopped first.
		s.Log.Info("restore refused", "member", member, "world", world, "archive", request.Archive, "reason", "not_stopped", "state", status.State)
		writeJSON(w, http.StatusConflict, map[string]string{"code": "not_stopped", "state": status.State})
	}
}

// mods lists every world's mods and whether each is on. A site may ask: it shows the page only to a Member with
// its minecraft-admin grant.
func (s *Server) mods(w http.ResponseWriter, r *http.Request) {
	list, err := s.Launcher.Mods(r.Context())
	if err != nil {
		s.Log.Error("mods", "err", err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "mods_unconfirmed"})
		return
	}
	writeJSON(w, http.StatusOK, list)
}

// switchMod turns one mod on or off for a world's next start, for a site or an operator. The body is
// {"world": "creative", "mod": "fawe", "enabled": false}. No header says who asked, so a switch is logged as its
// world, mod and new setting only.
func (s *Server) switchMod(w http.ResponseWriter, r *http.Request) {
	var request struct {
		World   string `json:"world"`
		Mod     string `json:"mod"`
		Enabled *bool  `json:"enabled"`
	}
	body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<10))
	if json.Unmarshal(body, &request) != nil || request.Enabled == nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
		return
	}
	list, err := s.Launcher.SwitchMod(r.Context(), request.World, request.Mod, *request.Enabled)
	switch {
	case errors.Is(err, ErrUnknownWorld):
		writeJSON(w, http.StatusConflict, map[string]string{"code": "unknown_world"})
	case errors.Is(err, ErrUnknownMod):
		writeJSON(w, http.StatusConflict, map[string]string{"code": "unknown_mod"})
	case err != nil:
		s.Log.Error("mod switch", "world", request.World, "mod", request.Mod, "enabled", *request.Enabled, "err", err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "mods_unconfirmed"})
	default:
		s.Log.Info("mod switch", "world", request.World, "mod", request.Mod, "enabled", *request.Enabled)
		writeJSON(w, http.StatusOK, list)
	}
}

func (s *Server) countFailure() {
	if s.StartFailed != nil {
		s.StartFailed()
	}
}

func (s *Server) countStopFailure() {
	if s.StopFailed != nil {
		s.StopFailed()
	}
}

// remember keeps each world's last confirmed state on this warm instance, offered as last_known when AWS can't
// be read.
func (s *Server) remember(world string, status Status) Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	if status.State == Unknown {
		status.LastKnown = s.lastKnown[world]
		return status
	}
	if s.lastKnown == nil {
		s.lastKnown = map[string]string{}
	}
	s.lastKnown[world] = status.State
	return status
}

func writeJSON(w http.ResponseWriter, code int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(value)
}
