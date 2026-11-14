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
// POST /server/stop, GET and POST /server/allowlist, GET /server/archives, POST /server/restore, GET and
// POST /server/operators, GET /mods and POST /mods.
type Server struct {
	Launcher interface {
		// Worlds answers the worlds' ids in the order the page shows them. The first is the default.
		Worlds() []string
		Status(ctx context.Context, world string) (Status, error)
		Start(ctx context.Context, world string) (Status, bool, error)
		Stop(ctx context.Context, world string, force bool) (Status, bool, error)
		AllowlistPlayers(context.Context) ([]PlayerName, error)
		Allowlist(context.Context, AllowlistRequest) (AllowlistResult, error)
		Archives(ctx context.Context, world string) (ArchiveList, error)
		Restore(ctx context.Context, world, archive string) (Status, bool, error)
		Operators(context.Context) (OperatorsList, error)
		ChangeOperator(context.Context, OperatorRequest) (OperatorResult, error)
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
	// may force a stop. Nil admits nobody.
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
	mux.HandleFunc("GET /server/allowlist", s.allowlistPlayers)
	mux.HandleFunc("POST /server/allowlist", s.allowlist)
	mux.HandleFunc("GET /server/archives", s.archives)
	mux.HandleFunc("POST /server/restore", s.restore)
	mux.HandleFunc("GET /server/operators", s.operators)
	mux.HandleFunc("POST /server/operators", s.changeOperator)
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

// allowlistPlayers lists the family's allowlist, for a site or an operator. A site shows it only to a Member with
// its minecraft-admin grant. No name is logged.
func (s *Server) allowlistPlayers(w http.ResponseWriter, r *http.Request) {
	players, err := s.Launcher.AllowlistPlayers(r.Context())
	if err != nil {
		s.Log.Error("allowlist list", "err", err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "allowlist_unconfirmed"})
		return
	}
	writeJSON(w, http.StatusOK, map[string][]PlayerName{"players": players})
}

// allowlist adds or removes one player on the family's allowlist, and on each running world, for a site or an
// operator. It takes no world. The player's name is not logged; the answer carries none either.
func (s *Server) allowlist(w http.ResponseWriter, r *http.Request) {
	member := r.Header.Get(MemberHeader)
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

// archives lists one world's archives, newest first, for a site or an operator: GET /server/archives?world=<id>,
// the first world when it names none.
func (s *Server) archives(w http.ResponseWriter, r *http.Request) {
	world := s.world(r.URL.Query().Get("world"))
	list, err := s.Launcher.Archives(r.Context(), world)
	switch {
	case errors.Is(err, ErrUnknownWorld):
		writeJSON(w, http.StatusConflict, map[string]string{"code": "unknown_world"})
	case err != nil:
		s.Log.Error("archives", "world", world, "err", err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "archives_unconfirmed"})
	default:
		writeJSON(w, http.StatusOK, list)
	}
}

// restore puts back the archive a site or an operator names in place of a stopped world, then starts its game.
// The body is {"archive": "20261016T031500Z.tar.gz"}, the archive's name in S3, which is not private and is
// logged, and may carry {"world": "creative"} beside it.
func (s *Server) restore(w http.ResponseWriter, r *http.Request) {
	member := r.Header.Get(MemberHeader)
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

// operators lists each world's operators, for a site or an operator. A site shows them only to a Member with its
// minecraft-admin grant. No name is logged.
func (s *Server) operators(w http.ResponseWriter, r *http.Request) {
	list, err := s.Launcher.Operators(r.Context())
	if err != nil {
		s.Log.Error("operators", "err", err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "operators_unconfirmed"})
		return
	}
	writeJSON(w, http.StatusOK, list)
}

// changeOperator makes a player on the allowlist an operator of one world, or no longer one, for a site or an
// operator. The body is {"world": "survival", "operation": "add", "edition": "java", "name": "Steve"}. The
// player's name is not logged; the answer carries none either.
func (s *Server) changeOperator(w http.ResponseWriter, r *http.Request) {
	var request OperatorRequest
	body, _ := io.ReadAll(io.LimitReader(r.Body, 4<<10))
	if json.Unmarshal(body, &request) != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
		return
	}
	result, err := s.Launcher.ChangeOperator(r.Context(), request)
	switch {
	case errors.Is(err, ErrBadAllowlistRequest):
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
	case errors.Is(err, ErrUnknownWorld):
		writeJSON(w, http.StatusConflict, map[string]string{"code": "unknown_world"})
	case errors.Is(err, ErrNotAllowlisted):
		s.Log.Info("operator refused", "world", request.World, "edition", request.Edition, "operation", request.Operation, "reason", "not_allowlisted")
		writeJSON(w, http.StatusConflict, map[string]string{"code": "not_allowlisted"})
	case err != nil:
		s.Log.Error("operator", "world", request.World, "edition", request.Edition, "operation", request.Operation, "err", err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "operators_failed"})
	default:
		s.Log.Info("operator", "world", result.World, "edition", request.Edition, "operation", result.Operation, "result", result.Operators)
		writeJSON(w, http.StatusOK, result)
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
