package main

import (
	"context"
	"errors"
	"maps"
	"slices"
)

// A mod is a Paper plugin the manifest pins. Each world lists the mods it may run, each on or off by default,
// and a Member with the site's minecraft-admin grant switches them. The parameter holds only the switches that
// differ from the manifest's defaults, as {"creative":{"fawe":false}}, and a switch applies at that world's next
// start, whose command names the mods that are on. An id the manifest does not list for a world is never sent,
// whatever the parameter holds.

// ErrUnknownMod means the request names a mod the pinned server content does not list for that world.
var ErrUnknownMod = errors.New("unknown mod")

// ModsList is the answer to GET /mods and POST /mods: every world in the order the page shows them, each with
// the mods it lists in order of their ids.
type ModsList struct {
	Worlds []WorldMods `json:"worlds"`
}

type WorldMods struct {
	ID   string      `json:"id"`
	Name string      `json:"name"`
	Mods []ModSwitch `json:"mods"`
}

// ModSwitch is one mod of one world: what the manifest says of it, its default there, and whether it is on.
type ModSwitch struct {
	ID string `json:"id"`
	Mod
	Default bool `json:"default"`
	Enabled bool `json:"enabled"`
}

// Mods lists every world's mods and whether each is on.
func (w *Worlds) Mods(ctx context.Context) (ModsList, error) {
	switches, err := w.switches(ctx)
	if err != nil {
		return ModsList{}, err
	}
	return w.list(switches), nil
}

// SwitchMod turns a mod on or off for a world's next start, and answers the list as it then stands.
func (w *Worlds) SwitchMod(ctx context.Context, id, mod string, enabled bool) (ModsList, error) {
	world, ok := w.Manifest.world(id)
	if !ok {
		return ModsList{}, ErrUnknownWorld
	}
	fallback, listed := world.Mods[mod]
	if !listed {
		return ModsList{}, ErrUnknownMod
	}
	switches, err := w.switches(ctx)
	if err != nil {
		return ModsList{}, err
	}
	if enabled != fallback {
		if switches[id] == nil {
			switches[id] = map[string]bool{}
		}
		switches[id][mod] = enabled
	} else {
		// Back at the manifest's default: no switch is kept, so a later change of that default reaches this world.
		delete(switches[id], mod)
		if len(switches[id]) == 0 {
			delete(switches, id)
		}
	}
	if err := w.write(ctx, w.ModsParameter, "mods", switches); err != nil {
		return ModsList{}, err
	}
	return w.list(switches), nil
}

// switches reads the Members' switches: for each world, the mods set differently from the manifest's default.
func (w *Worlds) switches(ctx context.Context) (map[string]map[string]bool, error) {
	switches := map[string]map[string]bool{}
	if err := w.read(ctx, w.ModsParameter, "mods", &switches); err != nil {
		return nil, err
	}
	if switches == nil {
		switches = map[string]map[string]bool{}
	}
	return switches, nil
}

// on reports whether a mod the world lists is on: a Member's switch decides, and without one the manifest's
// default.
func (world World) on(switches map[string]map[string]bool, mod string) bool {
	if enabled, switched := switches[world.ID][mod]; switched {
		return enabled
	}
	return world.Mods[mod]
}

func (w *Worlds) list(switches map[string]map[string]bool) ModsList {
	list := ModsList{Worlds: []WorldMods{}}
	for _, world := range w.Manifest.Worlds {
		mods := []ModSwitch{}
		for _, id := range slices.Sorted(maps.Keys(world.Mods)) {
			mods = append(mods, ModSwitch{ID: id, Mod: w.Manifest.Mods[id], Default: world.Mods[id], Enabled: world.on(switches, id)})
		}
		list.Worlds = append(list.Worlds, WorldMods{ID: world.ID, Name: world.Name, Mods: mods})
	}
	return list
}

// modsOn answers the ids of the mods that are on for a world, sorted, for its start command.
func (w *Worlds) modsOn(ctx context.Context, id string) ([]string, error) {
	world, ok := w.Manifest.world(id)
	if !ok {
		return nil, ErrUnknownWorld
	}
	switches, err := w.switches(ctx)
	if err != nil {
		return nil, err
	}
	on := []string{}
	for _, mod := range slices.Sorted(maps.Keys(world.Mods)) {
		if world.on(switches, mod) {
			on = append(on, mod)
		}
	}
	return on, nil
}
