package main

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"
)

func TestModsListsEveryWorldAndItsModsInOrder(t *testing.T) {
	w := newWorlds(t)
	list, err := w.worlds.Mods(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// Worlds in the order the page shows them, each world's mods in order of their ids, and with no switch made
	// every mod at the manifest's default.
	fawe := `{"id":"fawe","name":"FastAsyncWorldEdit","version":"2.16.2","summary":"Bulk building tools.","bedrock":"Commands and the wand work.",`
	gsit := `{"id":"gsit","name":"GSit","version":"3.8.0","summary":"Sit on stairs and slabs.","bedrock":"Sitting works with a click.",`
	want := `{"worlds":[` +
		`{"id":"survival","name":"Survival","mods":[` + gsit + `"default":true,"enabled":true}]},` +
		`{"id":"creative","name":"Creative","mods":[` + fawe + `"default":true,"enabled":true},` + gsit + `"default":false,"enabled":false}]}]}`
	if encoded, _ := json.Marshal(list); string(encoded) != want {
		t.Fatalf("%s", encoded)
	}
}

func TestASwitchKeepsOnlyWhatDiffersFromTheManifest(t *testing.T) {
	w := newWorlds(t)
	for _, step := range []struct {
		world, mod string
		enabled    bool
		kept       string
	}{
		{"creative", "fawe", false, `{"creative":{"fawe":false}}`},
		{"creative", "gsit", true, `{"creative":{"fawe":false,"gsit":true}}`},
		{"survival", "gsit", false, `{"creative":{"fawe":false,"gsit":true},"survival":{"gsit":false}}`},
		// Back at the manifest's default no switch is kept, so a later change of that default reaches the world.
		{"creative", "fawe", true, `{"creative":{"gsit":true},"survival":{"gsit":false}}`},
		{"creative", "gsit", false, `{"survival":{"gsit":false}}`},
		{"survival", "gsit", true, `{}`},
		{"survival", "gsit", true, `{}`},
	} {
		list, err := w.worlds.SwitchMod(context.Background(), step.world, step.mod, step.enabled)
		if err != nil || w.shared.params[modsName] != step.kept {
			t.Fatalf("%+v: kept %s err=%v", step, w.shared.params[modsName], err)
		}
		// The answer is the whole list as it now stands.
		at := slices.IndexFunc(list.Worlds, func(world WorldMods) bool { return world.ID == step.world })
		if len(list.Worlds) != 2 || at < 0 {
			t.Fatalf("%+v: answered %+v", step, list)
		}
		for _, mod := range list.Worlds[at].Mods {
			if mod.ID == step.mod && mod.Enabled != step.enabled {
				t.Fatalf("%+v: answered %+v", step, mod)
			}
		}
	}
}

func TestASwitchOfAnUnknownWorldOrModIsRefused(t *testing.T) {
	w := newWorlds(t)
	w.shared.writable = nil // any write panics
	if _, err := w.worlds.SwitchMod(context.Background(), "nether", "fawe", false); !errors.Is(err, ErrUnknownWorld) {
		t.Fatalf("err=%v", err)
	}
	// A mod is switched only for a world that lists it, even when the manifest pins it for another.
	for _, mod := range []string{"fawe", "worldedit", "", "gsit --force"} {
		if _, err := w.worlds.SwitchMod(context.Background(), "survival", mod, true); !errors.Is(err, ErrUnknownMod) {
			t.Fatalf("%q: err=%v", mod, err)
		}
	}
}

func TestAStartNamesOnlyTheModsItsWorldLists(t *testing.T) {
	w := newWorlds(t)
	// Whatever the parameter holds, the host is sent only ids the manifest lists for that world.
	w.shared.params[modsName] = `{"creative":{"fawe":false,"gsit":true,"worldedit":true,"gsit --force":true},"survival":{"fawe":true},"nether":{"gsit":true}}`
	for world, want := range map[string]string{"creative": "gsit", "survival": "gsit"} {
		if on, err := w.worlds.modsOn(context.Background(), world); err != nil || strings.Join(on, ",") != want {
			t.Fatalf("%s: on=%v err=%v", world, on, err)
		}
	}
	list, err := w.worlds.Mods(context.Background())
	if err != nil || len(list.Worlds) != 2 || len(list.Worlds[0].Mods) != 1 || len(list.Worlds[1].Mods) != 2 {
		t.Fatalf("list=%+v err=%v; an id the manifest does not list is not shown", list, err)
	}
	if err := finishStart(t, w.creative); err != nil {
		t.Fatal(err)
	}
	if len(w.creative.ssm.commands) != 1 || !strings.Contains(w.creative.ssm.commands[0], remoteCommand+` start creative --mods=gsit --allowlist-file "$allowlist"`) {
		t.Fatalf("commands %q", w.creative.ssm.commands)
	}

	// The ids go in order, and a world with every mod off still says so, with an empty list.
	w = newWorlds(t)
	w.shared.params[modsName] = `{"creative":{"gsit":true},"survival":{"gsit":false}}`
	for world, r := range map[string]*rig{"creative": w.creative, "survival": w.survival} {
		if err := finishStart(t, r); err != nil {
			t.Fatal(err)
		}
		want := map[string]string{"creative": "fawe,gsit", "survival": ""}[world]
		if len(r.ssm.commands) != 1 || !strings.Contains(r.ssm.commands[0], remoteCommand+" start "+world+" --mods="+want+` --allowlist-file "$allowlist"`) {
			t.Fatalf("%s: commands %q", world, r.ssm.commands)
		}
	}
}

func TestUnreadableModsFailTheListTheSwitchAndTheStart(t *testing.T) {
	for name, spoil := range map[string]func(w *worldsRig){
		"AWS does not answer":        func(w *worldsRig) { w.shared.getErr = errors.New("throttled") },
		"the parameter is missing":   func(w *worldsRig) { delete(w.shared.params, modsName) },
		"the parameter holds text":   func(w *worldsRig) { w.shared.params[modsName] = "fawe" },
		"the parameter holds a list": func(w *worldsRig) { w.shared.params[modsName] = `["fawe"]` },
	} {
		w := newWorlds(t)
		spoil(w)
		if _, err := w.worlds.Mods(context.Background()); err == nil {
			t.Fatalf("%s: the list must fail", name)
		}
		if _, err := w.worlds.SwitchMod(context.Background(), "creative", "fawe", false); err == nil {
			t.Fatalf("%s: the switch must fail", name)
		}
		// A start that cannot tell which mods are on must not guess: the game would run mods a Member switched off.
		if err := finishStart(t, w.creative); err == nil || len(w.creative.ssm.commands) != 0 || w.creative.session(t).Phase != Failed {
			t.Fatalf("%s: err=%v commands=%q session=%+v", name, err, w.creative.ssm.commands, w.creative.session(t))
		}
	}
	// A switch that could not be kept is not answered as made.
	w := newWorlds(t)
	w.shared.putErr = errors.New("throttled")
	if _, err := w.worlds.SwitchMod(context.Background(), "creative", "fawe", false); err == nil || w.shared.params[modsName] != "{}" {
		t.Fatalf("err=%v kept %s", err, w.shared.params[modsName])
	}
}
