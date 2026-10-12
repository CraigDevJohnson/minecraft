package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func environment(overrides map[string]string) func(string) string {
	values := map[string]string{
		"HOST_ID":                    "i-0123456789abcdef0",
		"INSTANCE":                   "family",
		"SERVER_NAME":                "Family server",
		"PUBLIC_HOSTNAME":            "mc.example.com",
		"BEDROCK_PORT":               "19132",
		"STATE_PARAMETER":            "/minecraft-test/launcher/state",
		"CLOUDFLARE_ZONE_ID":         "0123456789abcdef0123456789abcdef",
		"CLOUDFLARE_TOKEN_PARAMETER": "/minecraft/launcher/cloudflare-token",
	}
	for key, value := range overrides {
		values[key] = value
	}
	return func(key string) string { return values[key] }
}

func TestLoadConfig(t *testing.T) {
	cfg, dns, err := loadConfig(environment(nil))
	if err != nil || cfg.BedrockPort != 19132 || cfg.IdleStopStateParameter != "" || dns.TokenParameter != "/minecraft/launcher/cloudflare-token" || dns.Hostname != "mc.example.com" {
		t.Fatalf("cfg=%+v dns=%+v err=%v", cfg, dns, err)
	}
	if cfg, _, err := loadConfig(environment(map[string]string{"IDLE_STOP_STATE_PARAMETER": "/minecraft-test/idle-stop/state"})); err != nil || cfg.IdleStopStateParameter == "" {
		t.Fatalf("cfg=%+v err=%v", cfg, err)
	}
	for key, value := range map[string]string{
		"HOST_ID":                    "",
		"INSTANCE":                   "",
		"SERVER_NAME":                "",
		"BEDROCK_PORT":               "70000",
		"STATE_PARAMETER":            "launcher-state",
		"PUBLIC_HOSTNAME":            "MC.Example.com",
		"CLOUDFLARE_ZONE_ID":         "zone",
		"CLOUDFLARE_TOKEN_PARAMETER": "arn:aws:secretsmanager:us-west-2:111122223333:secret:token-abcdef",
		"IDLE_STOP_STATE_PARAMETER":  "state",
	} {
		if _, _, err := loadConfig(environment(map[string]string{key: value})); err == nil {
			t.Fatalf("%s=%q must be refused", key, value)
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
