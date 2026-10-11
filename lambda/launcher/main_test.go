package main

import "testing"

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
