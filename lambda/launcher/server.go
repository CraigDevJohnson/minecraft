package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"sync"
)

// MemberHeader names the Member the site acts for. Only the site can reach this function (the-lobby ADR 0006),
// so the header is trusted as who asked, and logged.
const MemberHeader = "X-Lobby-Member"

// Server is the web side the site calls: GET /server and POST /server/start.
type Server struct {
	Launcher interface {
		Status(context.Context) (Status, error)
		Start(context.Context) (Status, bool, error)
	}
	Log *slog.Logger

	mu        sync.Mutex
	lastKnown string
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /server", s.status)
	mux.HandleFunc("POST /server/start", s.start)
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
		}
		writeJSON(w, http.StatusAccepted, s.remember(status))
		return
	}
	if err != nil {
		// The site shows anything but 200, 202 and 404 as a status it can't confirm.
		s.Log.Error("start", "member", member, "err", err)
		writeJSON(w, http.StatusBadGateway, map[string]string{"code": "start_unconfirmed"})
		return
	}
	s.Log.Info("start unchanged", "member", member, "state", status.State)
	writeJSON(w, http.StatusOK, s.remember(status))
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
