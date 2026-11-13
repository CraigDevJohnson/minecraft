package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"regexp"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	"github.com/aws/smithy-go"
)

// identifier is the host adapter's rule for a world's or a mod's id (scripts/minecraft_crafty.py). The ids go
// unquoted into the commands this function runs on the host.
var identifier = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)

// Manifest is what the launcher reads of the pinned server content it carries for the host adapter
// (runtime/crafty/server.json): the worlds, in the order the page shows them, and the mods they may run.
type Manifest struct {
	Mods   map[string]Mod `json:"mods"`
	Worlds []World        `json:"worlds"`
}

// Mod is what the mods page shows of one pinned mod.
type Mod struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	Summary string `json:"summary"`
}

// World is one world of the manifest: its id, which also names the one instance its host runs, its display
// name, and each mod it may run with that mod's default, on or off.
type World struct {
	ID   string          `json:"id"`
	Name string          `json:"name"`
	Mods map[string]bool `json:"mods"`
}

// LoadManifest reads the worlds and the mods from the pinned server content among the adapter files in fsys,
// and refuses content this function could not show or send to a host.
func LoadManifest(fsys fs.FS) (*Manifest, error) {
	content, err := fs.ReadFile(fsys, "server.json")
	if err != nil {
		return nil, fmt.Errorf("read the pinned server content: %w", err)
	}
	var manifest Manifest
	if err := json.Unmarshal(content, &manifest); err != nil {
		return nil, fmt.Errorf("read the pinned server content: %w", err)
	}
	if len(manifest.Worlds) == 0 {
		return nil, errors.New("the pinned server content lists no world")
	}
	for id, mod := range manifest.Mods {
		if !identifier.MatchString(id) || mod.Name == "" || mod.Version == "" || mod.Summary == "" {
			return nil, fmt.Errorf("the mod %q needs a valid id, a name, a version and a summary", id)
		}
	}
	seen := map[string]bool{}
	for _, world := range manifest.Worlds {
		if !identifier.MatchString(world.ID) || world.Name == "" || seen[world.ID] {
			return nil, fmt.Errorf("the world %q needs a valid id of its own and a name", world.ID)
		}
		seen[world.ID] = true
		for mod := range world.Mods {
			if _, pinned := manifest.Mods[mod]; !pinned {
				return nil, fmt.Errorf("the world %s lists the mod %q, which the pinned server content does not have", world.ID, mod)
			}
		}
	}
	return &manifest, nil
}

func (m *Manifest) world(id string) (World, bool) {
	for _, world := range m.Worlds {
		if world.ID == id {
			return world, true
		}
	}
	return World{}, false
}

// ErrUnknownWorld means the request names a world the pinned server content does not have.
var ErrUnknownWorld = errors.New("unknown world")

// Worlds is every world's Launcher. Each world has its own host, hostname and record, so two worlds starting
// or stopping at once share nothing. What the worlds do share is kept here: the mods a Member switches for a
// world's next start, and the family's allowlist, which every start carries to its host.
type Worlds struct {
	Manifest *Manifest
	// Launchers holds each world's Launcher under the world's id.
	Launchers map[string]*Launcher
	SSM       SSMAPI
	// ModsParameter holds the Members' switches, and AllowlistParameter the allowlist of record.
	ModsParameter      string
	AllowlistParameter string
	// LookupPlayer resolves a Java name or Bedrock gamertag to the account the server knows.
	LookupPlayer func(ctx context.Context, edition, name string) (Player, error)
}

// Add lists a world's Launcher under its world's id and gives it what the worlds share.
func (w *Worlds) Add(launcher *Launcher) {
	id := launcher.Config.Instance
	launcher.Mods = func(ctx context.Context) ([]string, error) { return w.modsOn(ctx, id) }
	launcher.Whitelist = w.whitelist
	w.Launchers[id] = launcher
}

// HandOff gives every world's Launcher its follow-ups: send runs a task outside the request, and the task
// names the world, so the run that finishes a start or stop finishes that world's.
func (w *Worlds) HandOff(send func(ctx context.Context, t task) error) {
	for id, launcher := range w.Launchers {
		launcher.FollowUp = func(ctx context.Context) error { return send(ctx, task{Task: finishStartTask, World: id}) }
		launcher.FollowUpStop = func(ctx context.Context) error { return send(ctx, task{Task: finishStopTask, World: id}) }
	}
}

// Worlds answers the worlds' ids in the order the page shows them. The first is the one a request that names
// no world means.
func (w *Worlds) Worlds() []string {
	ids := make([]string, len(w.Manifest.Worlds))
	for i, world := range w.Manifest.Worlds {
		ids[i] = world.ID
	}
	return ids
}

// Status reports one world as players see it.
func (w *Worlds) Status(ctx context.Context, world string) (Status, error) {
	launcher, ok := w.Launchers[world]
	if !ok {
		return Status{}, ErrUnknownWorld
	}
	return launcher.Status(ctx)
}

// Start starts one world's host and game.
func (w *Worlds) Start(ctx context.Context, world string) (Status, bool, error) {
	launcher, ok := w.Launchers[world]
	if !ok {
		return Status{}, false, ErrUnknownWorld
	}
	return launcher.Start(ctx)
}

// Stop drains and stops one world's host.
func (w *Worlds) Stop(ctx context.Context, world string, force bool) (Status, bool, error) {
	launcher, ok := w.Launchers[world]
	if !ok {
		return Status{}, false, ErrUnknownWorld
	}
	return launcher.Stop(ctx, force)
}

// Restore puts one of a world's own archives back in place of it, then starts its game.
func (w *Worlds) Restore(ctx context.Context, world, archive string) (Status, bool, error) {
	launcher, ok := w.Launchers[world]
	if !ok {
		return Status{}, false, ErrUnknownWorld
	}
	return launcher.Restore(ctx, archive)
}

// read decodes the JSON one of the worlds' shared parameters holds into value.
func (w *Worlds) read(ctx context.Context, name, what string, value any) error {
	output, err := w.SSM.GetParameter(ctx, &ssm.GetParameterInput{Name: aws.String(name)})
	if err != nil {
		return fmt.Errorf("read the %s: %w", what, err)
	}
	if output.Parameter == nil || json.Unmarshal([]byte(aws.ToString(output.Parameter.Value)), value) != nil {
		return fmt.Errorf("read the %s: its parameter does not hold one", what)
	}
	return nil
}

// write stores value as the JSON of one of the worlds' shared parameters.
func (w *Worlds) write(ctx context.Context, name, what string, value any) error {
	encoded, err := json.Marshal(value)
	if err != nil {
		return fmt.Errorf("encode the %s", what)
	}
	_, err = w.SSM.PutParameter(ctx, &ssm.PutParameterInput{Name: aws.String(name), Value: aws.String(string(encoded)), Overwrite: aws.Bool(true)})
	// Only the refusal's code is kept. AWS may repeat a refused value in its message, and the allowlist holds
	// players' names, which are never logged.
	var refused smithy.APIError
	switch {
	case errors.As(err, &refused):
		return fmt.Errorf("write the %s: %s", what, refused.ErrorCode())
	case err != nil:
		return fmt.Errorf("write the %s: AWS did not answer", what)
	}
	return nil
}
