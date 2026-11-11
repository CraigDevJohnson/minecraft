package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-lambda-go/events"
	"github.com/awslabs/aws-lambda-go-api-proxy/httpadapter"
)

// worldsValue is WORLDS as OpenTofu writes it for the two worlds of testContent.
const worldsValue = `{
	"survival": {"host_id": "i-0123456789abcdef0", "hostname": "survival.mc.example.com", "launcher_state_parameter": "/minecraft-test/launcher/worlds/survival/state", "idle_stop_state_parameter": "/minecraft-test/idle-stop/worlds/survival/state"},
	"creative": {"host_id": "i-0fedcba9876543210", "hostname": "creative.mc.example.com", "launcher_state_parameter": "/minecraft-test/launcher/worlds/creative/state", "idle_stop_state_parameter": ""}
}`

func environment(overrides map[string]string) func(string) string {
	values := map[string]string{
		"WORLDS":                     worldsValue,
		"MODS_PARAMETER":             "/minecraft-test/launcher/mods",
		"ALLOWLIST_PARAMETER":        "/minecraft-test/launcher/allowlist",
		"BEDROCK_PORT":               "19132",
		"CLOUDFLARE_ZONE_ID":         "0123456789abcdef0123456789abcdef",
		"CLOUDFLARE_TOKEN_PARAMETER": "/minecraft/launcher/cloudflare-token",
	}
	for key, value := range overrides {
		values[key] = value
	}
	return func(key string) string { return values[key] }
}

func TestLoadConfig(t *testing.T) {
	manifest, err := LoadManifest(content(testContent))
	if err != nil {
		t.Fatal(err)
	}
	env, err := loadConfig(environment(nil), manifest)
	if err != nil || env.ModsParameter != modsName || env.AllowlistParameter != allowlistName || env.DNS.TokenParameter != "/minecraft/launcher/cloudflare-token" || env.DNS.ZoneID == "" {
		t.Fatalf("env=%+v err=%v", env, err)
	}
	// The worlds come in the manifest's order, each named by the manifest and placed by WORLDS. A world without an
	// idle stop has no state to read.
	want := []Config{
		{HostID: hostID, Instance: "survival", ServerName: "Survival", PublicHostname: "survival.mc.example.com", BedrockPort: 19132, StateParameter: stateName, IdleStopStateParameter: idleStateName},
		{HostID: creativeHostID, Instance: "creative", ServerName: "Creative", PublicHostname: "creative.mc.example.com", BedrockPort: 19132, StateParameter: "/minecraft-test/launcher/worlds/creative/state"},
	}
	if !slices.Equal(env.Worlds, want) {
		t.Fatalf("worlds %+v", env.Worlds)
	}
	for key, value := range map[string]string{
		"WORLDS":                     "",
		"MODS_PARAMETER":             "",
		"ALLOWLIST_PARAMETER":        "allowlist",
		"BEDROCK_PORT":               "70000",
		"CLOUDFLARE_ZONE_ID":         "zone",
		"CLOUDFLARE_TOKEN_PARAMETER": "arn:aws:secretsmanager:us-west-2:111122223333:secret:token-abcdef",
	} {
		if _, err := loadConfig(environment(map[string]string{key: value}), manifest); err == nil {
			t.Fatalf("%s=%q must be refused", key, value)
		}
	}
	// WORLDS must hold exactly the manifest's worlds: a world without a host could never start, and a host
	// without a world would never be shown or stopped.
	survival := `"survival": {"host_id": "i-0123456789abcdef0", "hostname": "survival.mc.example.com", "launcher_state_parameter": "/minecraft-test/launcher/worlds/survival/state"}`
	creative := func(settings string) string { return `{` + survival + `, "creative": {` + settings + `}}` }
	whole := `"host_id": "i-0fedcba9876543210", "hostname": "creative.mc.example.com", "launcher_state_parameter": "/minecraft-test/launcher/worlds/creative/state"`
	if _, err := loadConfig(environment(map[string]string{"WORLDS": creative(whole)}), manifest); err != nil {
		t.Fatal(err)
	}
	for name, value := range map[string]string{
		"not JSON":                   "survival,creative",
		"a world too few":            `{` + survival + `}`,
		"a world too many":           `{` + survival + `, "creative": {` + whole + `}, "adventure": {` + whole + `}}`,
		"another world":              `{` + survival + `, "adventure": {` + whole + `}}`,
		"no host":                    creative(`"hostname": "creative.mc.example.com", "launcher_state_parameter": "/minecraft-test/launcher/worlds/creative/state"`),
		"no record":                  creative(`"host_id": "i-0fedcba9876543210", "hostname": "creative.mc.example.com"`),
		"a record that is no name":   creative(`"host_id": "i-0fedcba9876543210", "hostname": "creative.mc.example.com", "launcher_state_parameter": "launcher-state"`),
		"no hostname":                creative(`"host_id": "i-0fedcba9876543210", "launcher_state_parameter": "/minecraft-test/launcher/worlds/creative/state"`),
		"a hostname in capitals":     creative(`"host_id": "i-0fedcba9876543210", "hostname": "Creative.MC.Example.com", "launcher_state_parameter": "/minecraft-test/launcher/worlds/creative/state"`),
		"an idle stop state no name": creative(whole + `, "idle_stop_state_parameter": "state"`),
	} {
		if _, err := loadConfig(environment(map[string]string{"WORLDS": value}), manifest); err == nil {
			t.Fatalf("WORLDS with %s must be refused", name)
		}
	}
}

func TestWriteMetricIsEmbeddedMetricFormat(t *testing.T) {
	var buffer bytes.Buffer
	writeMetric(&buffer, "minecraft-test-dev-launcher", startFailureMetric, time.UnixMilli(1760198400000))
	var stops bytes.Buffer
	writeMetric(&stops, "minecraft-test-dev-launcher", stopFailureMetric, time.UnixMilli(1760198400000))
	if !strings.Contains(stops.String(), `"StopFailures":1`) || !strings.Contains(stops.String(), `"Name":"StopFailures"`) {
		t.Fatalf("%q", stops.String())
	}
	var line struct {
		AWS struct {
			Timestamp         int64
			CloudWatchMetrics []struct {
				Namespace  string
				Dimensions [][]string
				Metrics    []struct{ Name, Unit string }
			}
		} `json:"_aws"`
		FunctionName  string
		StartFailures int
	}
	if err := json.Unmarshal(buffer.Bytes(), &line); err != nil || !strings.HasSuffix(buffer.String(), "}\n") || strings.Count(buffer.String(), "\n") != 1 {
		t.Fatalf("%q: %v", buffer.String(), err)
	}
	metrics := line.AWS.CloudWatchMetrics
	if line.AWS.Timestamp != 1760198400000 || len(metrics) != 1 || metrics[0].Namespace != "Minecraft/Launcher" ||
		len(metrics[0].Dimensions) != 1 || len(metrics[0].Dimensions[0]) != 1 || metrics[0].Dimensions[0][0] != "FunctionName" ||
		len(metrics[0].Metrics) != 1 || metrics[0].Metrics[0].Name != "StartFailures" || metrics[0].Metrics[0].Unit != "Count" ||
		line.FunctionName != "minecraft-test-dev-launcher" || line.StartFailures != 1 {
		t.Fatalf("%q", buffer.String())
	}
}

func TestTheOperatorCheckReadsTheCallerTheFunctionURLVerified(t *testing.T) {
	operator := signedOperator(RoleNames("arn:aws:iam::111122223333:role/site-dev-server"))
	var admitted []bool
	proxy := httpadapter.NewV2(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		admitted = append(admitted, operator(r))
		w.WriteHeader(http.StatusNoContent)
	}))
	for _, caller := range []string{
		"arn:aws:sts::111122223333:assumed-role/site-dev-server/lobby",
		"arn:aws:sts::111122223333:assumed-role/minecraft-server-control/run",
		"",
	} {
		request := events.APIGatewayV2HTTPRequest{RawPath: "/server/allowlist", RequestContext: events.APIGatewayV2HTTPRequestContext{
			HTTP: events.APIGatewayV2HTTPRequestContextHTTPDescription{Method: "POST", Path: "/server/allowlist"},
		}}
		if caller != "" {
			request.RequestContext.Authorizer = &events.APIGatewayV2HTTPRequestContextAuthorizerDescription{
				IAM: &events.APIGatewayV2HTTPRequestContextAuthorizerIAMDescription{UserARN: caller},
			}
		}
		if _, err := proxy.ProxyWithContext(context.Background(), request); err != nil {
			t.Fatal(err)
		}
	}
	if len(admitted) != 3 || admitted[0] || !admitted[1] || admitted[2] {
		t.Fatalf("admitted %v, want site refused, control role admitted, unsigned refused", admitted)
	}
	local, _ := http.NewRequest("POST", "/server/allowlist", nil)
	if operator(local) {
		t.Fatal("a request without the Function URL's event must not be an operator")
	}
}

func TestTheHostAdapterComesFromThePackageOnLambdaOrTheCheckout(t *testing.T) {
	sum := func(t *testing.T, adapter *HostAdapter, name string) string {
		t.Helper()
		for i, held := range adapter.names {
			if held == name {
				return adapter.sums[i]
			}
		}
		t.Fatalf("the adapter does not carry %s", name)
		return ""
	}
	pinned := func(t *testing.T, path string) string {
		t.Helper()
		content, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		digest := sha256.Sum256(content)
		return hex.EncodeToString(digest[:])
	}

	task := t.TempDir()
	if err := os.Mkdir(filepath.Join(task, "adapter"), 0o755); err != nil {
		t.Fatal(err)
	}
	for _, name := range adapterFiles {
		if err := os.WriteFile(filepath.Join(task, "adapter", name), []byte("packaged "+name), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	lambda := func(name string) string { return map[string]string{"LAMBDA_TASK_ROOT": task}[name] }
	adapter, err := LoadHostAdapter(adapterSource(lambda))
	if err != nil {
		t.Fatal(err)
	}
	if got := sum(t, adapter, "server.json"); got != pinned(t, filepath.Join(task, "adapter", "server.json")) {
		t.Fatal("on Lambda the adapter must be read from the package's adapter folder")
	}

	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	local := func(string) string { return "" }
	for _, directory := range []string{filepath.Join(root, "lambda", "launcher"), root, filepath.Join(root, "docs")} {
		t.Chdir(directory)
		adapter, err := LoadHostAdapter(adapterSource(local))
		if err != nil {
			t.Fatalf("from %s: %v", directory, err)
		}
		if sum(t, adapter, "minecraft_crafty.py") != pinned(t, filepath.Join(root, "scripts", "minecraft_crafty.py")) ||
			sum(t, adapter, "server.json") != pinned(t, filepath.Join(root, "runtime", "crafty", "server.json")) {
			t.Fatalf("from %s the adapter is not the checkout's scripts and pinned server content", directory)
		}
	}
}
