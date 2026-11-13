package main

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"testing/fstest"
	"time"

	ec2types "github.com/aws/aws-sdk-go-v2/service/ec2/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
)

const (
	creativeHostID = "i-0fedcba9876543210"
	modsName       = "/minecraft-test/launcher/mods"
	allowlistName  = "/minecraft-test/launcher/allowlist"
	// testContent is pinned server content with two worlds. Creative lists one mod that is on by default and one
	// that is off.
	testContent = `{
		"schema_version": 2,
		"mods": {
			"fawe": {"name": "FastAsyncWorldEdit", "version": "2.16.2", "summary": "Bulk building tools."},
			"gsit": {"name": "GSit", "version": "3.8.0", "summary": "Sit on stairs and slabs."}
		},
		"worlds": [
			{"id": "survival", "name": "Survival", "mods": {"gsit": true}},
			{"id": "creative", "name": "Creative", "mods": {"gsit": false, "fawe": true}}
		]
	}`
)

func content(text string) fstest.MapFS {
	return fstest.MapFS{"server.json": &fstest.MapFile{Data: []byte(text)}}
}

// worldsRig is the two worlds of testContent, each on a rig of its own, with the parameters the worlds share.
type worldsRig struct {
	worlds   *Worlds
	survival *rig
	creative *rig
	shared   *fakeSSM
	// looked are the player lookups made, and sent the follow-ups handed off.
	looked []string
	sent   []task
}

// newWorlds has every host stopped, no mod switched and nobody on the allowlist.
func newWorlds(t *testing.T) *worldsRig {
	t.Helper()
	manifest, err := LoadManifest(content(testContent))
	if err != nil {
		t.Fatal(err)
	}
	w := &worldsRig{
		survival: newRig(ec2types.InstanceStateNameStopped, 0),
		creative: newWorldRig("creative", "Creative", creativeHostID, ec2types.InstanceStateNameStopped, 0),
		shared:   &fakeSSM{writable: []string{modsName, allowlistName}, params: map[string]string{modsName: "{}", allowlistName: "[]"}},
	}
	w.worlds = &Worlds{
		Manifest:           manifest,
		Launchers:          map[string]*Launcher{},
		SSM:                w.shared,
		ModsParameter:      modsName,
		AllowlistParameter: allowlistName,
		LookupPlayer: func(_ context.Context, edition, name string) (Player, error) {
			w.looked = append(w.looked, edition+":"+name)
			return lookedUp(name)
		},
	}
	w.worlds.Add(w.survival.launcher)
	w.worlds.Add(w.creative.launcher)
	w.worlds.HandOff(func(_ context.Context, t task) error {
		w.sent = append(w.sent, t)
		return nil
	})
	return w
}

// finishStart runs the follow-up of a start of r's world on a host that has come up, with the adapter answering
// that the game is ready.
func finishStart(t *testing.T, r *rig) error {
	t.Helper()
	r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNameRunning}
	r.ec2.launch = now.Add(30 * time.Second)
	r.ssm.online = []bool{true}
	r.ssm.invocation = []ssm.GetCommandInvocationOutput{success(strings.Replace(readyOutput, "survival", r.launcher.Config.Instance, 1))}
	r.setSession(t, Session{RequestedAt: now, Phase: Starting})
	return r.launcher.FinishStart(context.Background())
}

func TestTheRepositoryManifestLoads(t *testing.T) {
	manifest, err := LoadManifest(checkout{os.DirFS(filepath.Join("..", ".."))})
	if err != nil {
		t.Fatal(err)
	}
	// The first world is the one a request that names none means, and the one a page without worlds shows.
	ids := (&Worlds{Manifest: manifest}).Worlds()
	if strings.Join(ids, ",") != "survival,creative,adventure" {
		t.Fatalf("worlds %v", ids)
	}
}

func TestLoadManifestRefusesContentItCouldNotShowOrSend(t *testing.T) {
	mods := `"mods": {"gsit": {"name": "GSit", "version": "3.8.0", "summary": "Sit."}}`
	for name, text := range map[string]string{
		"not JSON":                         "survival",
		"no worlds":                        `{` + mods + `, "worlds": []}`,
		"a world without a name":           `{` + mods + `, "worlds": [{"id": "survival"}]}`,
		"a world without an id":            `{` + mods + `, "worlds": [{"name": "Survival"}]}`,
		"two worlds with one id":           `{` + mods + `, "worlds": [{"id": "survival", "name": "Survival"}, {"id": "survival", "name": "Again"}]}`,
		"a world that lists an unpinned":   `{` + mods + `, "worlds": [{"id": "survival", "name": "Survival", "mods": {"fawe": true}}]}`,
		"a mod without its summary":        `{"mods": {"gsit": {"name": "GSit", "version": "3.8.0"}}, "worlds": [{"id": "survival", "name": "Survival"}]}`,
		"a mod without a version":          `{"mods": {"gsit": {"name": "GSit", "summary": "Sit."}}, "worlds": [{"id": "survival", "name": "Survival"}]}`,
		"a mod id that is not an id":       `{"mods": {"GSit": {"name": "GSit", "version": "3.8.0", "summary": "Sit."}}, "worlds": [{"id": "survival", "name": "Survival"}]}`,
		"a mod id that would end the flag": `{"mods": {"gsit --force": {"name": "GSit", "version": "3.8.0", "summary": "Sit."}}, "worlds": [{"id": "survival", "name": "Survival"}]}`,
	} {
		if _, err := LoadManifest(content(text)); err == nil {
			t.Fatalf("%s must be refused", name)
		}
	}
	// A world's id goes unquoted into the commands run on its host: only the adapter's own names pass.
	for _, id := range []string{"Survival", "survival world", "survival;reboot", "-survival", "1survival", "survival'", strings.Repeat("a", 33)} {
		if _, err := LoadManifest(content(`{` + mods + `, "worlds": [{"id": "` + id + `", "name": "Survival"}]}`)); err == nil {
			t.Fatalf("the world id %q must be refused", id)
		}
	}
	for _, id := range []string{"survival", "survival-2", strings.Repeat("a", 32)} {
		if manifest, err := LoadManifest(content(`{` + mods + `, "worlds": [{"id": "` + id + `", "name": "Survival"}]}`)); err != nil || manifest.Worlds[0].ID != id {
			t.Fatalf("the world id %q: %v", id, err)
		}
	}
	if _, err := LoadManifest(fstest.MapFS{}); err == nil {
		t.Fatal("a launcher without the pinned server content must not start")
	}
}

func TestTheWorldsComeInTheManifestsOrder(t *testing.T) {
	w := newWorlds(t)
	if ids := w.worlds.Worlds(); !slices.Equal(ids, []string{"survival", "creative"}) {
		t.Fatalf("worlds %v", ids)
	}
}

func TestAnUnknownWorldIsRefused(t *testing.T) {
	w := newWorlds(t)
	ctx := context.Background()
	if _, err := w.worlds.Status(ctx, "nether"); !errors.Is(err, ErrUnknownWorld) {
		t.Fatalf("status: err=%v", err)
	}
	if _, started, err := w.worlds.Start(ctx, "nether"); !errors.Is(err, ErrUnknownWorld) || started {
		t.Fatalf("start: started=%v err=%v", started, err)
	}
	if _, stopped, err := w.worlds.Stop(ctx, "nether", true); !errors.Is(err, ErrUnknownWorld) || stopped {
		t.Fatalf("stop: stopped=%v err=%v", stopped, err)
	}
	if _, started, err := w.worlds.Restore(ctx, "nether", archiveFile); !errors.Is(err, ErrUnknownWorld) || started {
		t.Fatalf("restore: started=%v err=%v", started, err)
	}
	log, _ := quiet()
	if err := (task{Task: finishStartTask, World: "nether"}).run(ctx, w.worlds, log); err == nil {
		t.Fatal("a follow-up for an unknown world must fail")
	}
	// A follow-up sent before worlds existed names none: it finishes nothing, least of all the first world's start.
	if err := (task{Task: finishStartTask}).run(ctx, w.worlds, log); err == nil {
		t.Fatal("a follow-up that names no world must fail")
	}
	if err := (task{Task: "finish-everything", World: "survival"}).run(ctx, w.worlds, log); err == nil {
		t.Fatal("an unknown task must fail")
	}
	for _, r := range []*rig{w.survival, w.creative} {
		if r.ec2.starts != 0 || r.ec2.stops != 0 || len(r.ssm.commands) != 0 || r.session(t) != (Session{}) {
			t.Fatalf("an unknown world reached %s", r.launcher.Config.Instance)
		}
	}
}

func TestTwoWorldsStartingAtOnceShareNothing(t *testing.T) {
	w := newWorlds(t)
	ctx := context.Background()
	for _, world := range []string{"creative", "survival"} {
		if status, started, err := w.worlds.Start(ctx, world); err != nil || !started || status.State != Starting {
			t.Fatalf("%s: state=%s started=%v err=%v", world, status.State, started, err)
		}
	}
	// Each start is recorded in its own world's parameter, starts its own host, and hands off a follow-up that
	// names its world.
	if !slices.Equal(w.sent, []task{{finishStartTask, "creative"}, {finishStartTask, "survival"}}) {
		t.Fatalf("follow-ups %v", w.sent)
	}
	if payload, _ := json.Marshal(w.sent[0]); string(payload) != `{"launcher_task":"finish-start","world":"creative"}` {
		t.Fatalf("the follow-up's payload %s", payload)
	}
	// The hosts come up at different times and with different addresses.
	w.survival.ec2.ip, w.creative.ec2.ip = "203.0.113.10", "203.0.113.20"
	for world, r := range map[string]*rig{"survival": w.survival, "creative": w.creative} {
		if r.ec2.starts != 1 || r.session(t).Phase != Starting {
			t.Fatalf("%s: starts=%d session=%+v", world, r.ec2.starts, r.session(t))
		}
		r.ec2.states = []ec2types.InstanceStateName{ec2types.InstanceStateNamePending, ec2types.InstanceStateNameRunning}
		r.ec2.launch = now
		r.ssm.online = []bool{true}
		r.ssm.invocation = []ssm.GetCommandInvocationOutput{success(strings.Replace(readyOutput, "survival", world, 1))}
	}
	log, logged := quiet()
	for _, follow := range w.sent {
		if err := follow.run(ctx, w.worlds, log); err != nil {
			t.Fatalf("%+v: %v", follow, err)
		}
	}
	if !strings.Contains(logged.String(), `"world":"creative"`) || !strings.Contains(logged.String(), `"world":"survival"`) {
		t.Fatalf("a follow-up's log line must carry its world: %s", logged.String())
	}
	for world, r := range map[string]*rig{"survival": w.survival, "creative": w.creative} {
		if session := r.session(t); session.Phase != Ready || session.DNS != "current" {
			t.Fatalf("%s: session %+v", world, session)
		}
		// Its own host got its own start, and its own hostname its own host's address.
		if len(r.ssm.commands) != 1 || !strings.Contains(r.ssm.commands[0], remoteCommand+" start "+world+" --mods=") {
			t.Fatalf("%s: commands %q", world, r.ssm.commands)
		}
		if !slices.Equal(r.pointedAt, []string{r.ec2.ip}) || r.launcher.Config.PublicHostname != world+".mc.example.com" {
			t.Fatalf("%s: DNS pointed at %v", world, r.pointedAt)
		}
	}
	// One world stopping leaves the other running.
	w.survival.gameUp, w.creative.gameUp = true, true
	if status, stopped, err := w.worlds.Stop(ctx, "creative", false); err != nil || !stopped || status.State != Stopping {
		t.Fatalf("state=%s stopped=%v err=%v", status.State, stopped, err)
	}
	if last := w.sent[len(w.sent)-1]; last != (task{finishStopTask, "creative"}) {
		t.Fatalf("the stop's follow-up %+v", last)
	}
	if status, err := w.worlds.Status(ctx, "survival"); err != nil || status.State != Ready || w.survival.session(t).Phase != Ready {
		t.Fatalf("survival: state=%s err=%v", status.State, err)
	}
	if status, err := w.worlds.Status(ctx, "creative"); err != nil || status.State != Stopping || status.Name != "Creative" || status.Connection.Java.Address != "creative.mc.example.com" {
		t.Fatalf("creative: status=%+v err=%v", status, err)
	}
}
