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
// POST /server/stop, POST /server/allowlist and POST /server/restore.
type Server struct {
	Launcher interface {
		Status(context.Context) (Status, error)
		Start(context.Context) (Status, bool, error)
		Stop(ctx context.Context, force bool) (Status, bool, error)
		Allowlist(context.Context, AllowlistRequest) (AllowlistResult, error)
		Restore(ctx context.Context, archive string) (Status, bool, error)
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
	lastKnown string
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
	return mux
}

func (s *Server) status(w http.ResponseWriter, r *http.Request) {
	status, err := s.Launcher.Status(r.Context())
	if errors.Is(err, ErrNoServer) {
		writeJSON(w, http.StatusNotFound, map[string]string{"code": "no_server"})
		return
	}
	if err != nil {
		s.Log.Error("status", "err", err)
	}
	writeJSON(w, http.StatusOK, s.remember(status))
}

func (s *Server) start(w http.ResponseWriter, r *http.Request) {
	member := r.Header.Get(MemberHeader)
	status, started, err := s.Launcher.Start(r.Context())
	if errors.Is(err, ErrNoServer) {
		writeJSON(w, http.StatusNotFound, map[string]string{"code": "no_server"})
		return
	}
	if started {
		s.Log.Info("start", "member", member)
		if err != nil {
			s.Log.Error("start follow-up", "member", member, "err", err)
			s.countFailure()
		}
		writeJSON(w, http.StatusAccepted, s.remember(status))
		return
	}
	if err != nil {
		// The site shows anything but 200, 202 and 404 as a status it can't confirm.
		s.Log.Error("start", "member", member, "err", err)
		s.countFailure()
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "start_unconfirmed"})
		return
	}
	s.Log.Info("start unchanged", "member", member, "state", status.State)
	writeJSON(w, http.StatusOK, s.remember(status))
}

// stop drains and stops the server. The body may carry {"force": true}, which only an operator may send, to
// stop over players or a game that does not answer.
func (s *Server) stop(w http.ResponseWriter, r *http.Request) {
	member := r.Header.Get(MemberHeader)
	var request struct {
		Force bool `json:"force"`
	}
	body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<10))
	if len(bytes.TrimSpace(body)) > 0 && json.Unmarshal(body, &request) != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
		return
	}
	if request.Force && !s.operator(r) {
		s.Log.Info("stop refused", "member", member, "reason", "force_not_allowed")
		writeJSON(w, http.StatusForbidden, map[string]string{"code": "forbidden"})
		return
	}
	status, stopped, err := s.Launcher.Stop(r.Context(), request.Force)
	var online ErrPlayersOnline
	switch {
	case errors.Is(err, ErrNoServer):
		writeJSON(w, http.StatusNotFound, map[string]string{"code": "no_server"})
	case stopped:
		s.Log.Info("stop", "member", member, "force", request.Force)
		if err != nil {
			s.Log.Error("stop follow-up", "member", member, "err", err)
			s.countStopFailure()
		}
		writeJSON(w, http.StatusAccepted, s.remember(status))
	case errors.Is(err, ErrStarting):
		s.Log.Info("stop refused", "member", member, "reason", "starting")
		writeJSON(w, http.StatusConflict, map[string]string{"code": "starting"})
	case errors.As(err, &online):
		s.Log.Info("stop refused", "member", member, "reason", "players_online", "players", online.Players)
		writeJSON(w, http.StatusConflict, map[string]any{"code": "players_online", "players": online.Players})
	case errors.Is(err, ErrPlayersUnknown):
		s.Log.Info("stop refused", "member", member, "reason", "players_unknown")
		writeJSON(w, http.StatusConflict, map[string]string{"code": "players_unknown"})
	case err != nil:
		// The site shows anything but 200, 202, 404 and 409 as a status it can't confirm.
		s.Log.Error("stop", "member", member, "err", err)
		s.countStopFailure()
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "stop_unconfirmed"})
	default:
		s.Log.Info("stop unchanged", "member", member, "state", status.State)
		writeJSON(w, http.StatusOK, s.remember(status))
	}
}

// allowlist adds or removes one player on the running server for an operator, never a site. The player's
// name is not logged; the adapter's answer carries none either.
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
	var notReady ErrServerNotReady
	switch {
	case errors.Is(err, ErrNoServer):
		writeJSON(w, http.StatusNotFound, map[string]string{"code": "no_server"})
	case errors.Is(err, ErrBadAllowlistRequest):
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
	case errors.As(err, &notReady):
		s.Log.Info("allowlist refused", "member", member, "reason", "not_ready", "state", notReady.State)
		writeJSON(w, http.StatusConflict, map[string]string{"code": "not_ready", "state": notReady.State})
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
		s.Log.Info("allowlist", "member", member, "edition", result.Edition, "operation", result.Operation, "result", result.Allowlist)
		writeJSON(w, http.StatusOK, result)
	}
}

// restore puts back the archive an operator names, never a site, in place of the stopped server's world, then
// starts the game. The body is {"archive": "20261016T031500Z.tar.gz"}, the archive's name in S3, which is not
// private and is logged.
func (s *Server) restore(w http.ResponseWriter, r *http.Request) {
	member := r.Header.Get(MemberHeader)
	if !s.operator(r) {
		s.Log.Info("restore refused", "member", member, "reason", "not_an_operator")
		writeJSON(w, http.StatusForbidden, map[string]string{"code": "forbidden"})
		return
	}
	var request struct {
		Archive string `json:"archive"`
	}
	body, _ := io.ReadAll(io.LimitReader(r.Body, 1<<10))
	if json.Unmarshal(body, &request) != nil || !archiveName.MatchString(request.Archive) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
		return
	}
	status, started, err := s.Launcher.Restore(r.Context(), request.Archive)
	switch {
	case errors.Is(err, ErrNoServer):
		writeJSON(w, http.StatusNotFound, map[string]string{"code": "no_server"})
	case errors.Is(err, ErrBadRestoreRequest):
		writeJSON(w, http.StatusBadRequest, map[string]string{"code": "bad_request"})
	case started:
		s.Log.Info("restore", "member", member, "archive", request.Archive)
		if err != nil {
			s.Log.Error("restore follow-up", "member", member, "archive", request.Archive, "err", err)
			s.countFailure()
		}
		writeJSON(w, http.StatusAccepted, s.remember(status))
	case err != nil:
		s.Log.Error("restore", "member", member, "archive", request.Archive, "err", err)
		s.countFailure()
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "restore_unconfirmed"})
	default:
		// Only a stopped server, or one whose start or restore failed, is restored: anything else is stopped first.
		s.Log.Info("restore refused", "member", member, "archive", request.Archive, "reason", "not_stopped", "state", status.State)
		writeJSON(w, http.StatusConflict, map[string]string{"code": "not_stopped", "state": status.State})
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

// remember keeps the last confirmed state of this warm instance, offered as last_known when AWS can't be read.
func (s *Server) remember(status Status) Status {
	s.mu.Lock()
	defer s.mu.Unlock()
	if status.State == Unknown {
		status.LastKnown = s.lastKnown
		return status
	}
	s.lastKnown = status.State
	return status
}

func writeJSON(w http.ResponseWriter, code int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(value)
}
