// Command launcher is the Minecraft Launcher's backend: it answers The Lobby's site with each world's status,
// starts or stops a world when a Member or the operator's workflow asks, switches a world's mods, and restores
// an archive or changes the allowlist when the operator's workflow asks.
//
// On Lambda it serves the site through its Function URL, which only the site's role and the operator's
// control role may call (the-lobby ADR 0006, ADR 0006 here), and finishes each start, stop or restore in a
// second, asynchronous run of itself. Anywhere else it listens on a local port with the caller's own AWS
// credentials and finishes them in the background.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/aws/aws-lambda-go/events"
	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	awslambda "github.com/aws/aws-sdk-go-v2/service/lambda"
	lambdatypes "github.com/aws/aws-sdk-go-v2/service/lambda/types"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	"github.com/awslabs/aws-lambda-go-api-proxy/core"
	"github.com/awslabs/aws-lambda-go-api-proxy/httpadapter"
)

// The tasks of the asynchronous runs that finish a start and a stop.
const (
	finishStartTask = "finish-start"
	finishStopTask  = "finish-stop"
)

// task is the payload of the asynchronous run that finishes one world's start or stop.
type task struct {
	Task  string `json:"launcher_task"`
	World string `json:"world"`
}

// run finishes the start or stop the task names, for the world it names.
func (t task) run(ctx context.Context, worlds *Worlds, log *slog.Logger) error {
	launcher, ok := worlds.Launchers[t.World]
	switch {
	case !ok:
		return errors.New("launcher task for an unknown world")
	case t.Task == finishStartTask:
		return finish(ctx, "finish start", t.World, launcher.FinishStart, log)
	case t.Task == finishStopTask:
		return finish(ctx, "finish stop", t.World, launcher.FinishStop, log)
	}
	return errors.New("unknown launcher task")
}

// followUpMargin leaves the follow-up run time to record a failure before Lambda ends it.
const followUpMargin = 20 * time.Second

// Environment is what the function's environment supplies: each world's Config, in the order the pinned server
// content lists the worlds, the zone and token their DNS records share, and the two parameters the worlds share.
type Environment struct {
	Worlds             []Config
	DNS                DNSPointer
	ModsParameter      string
	AllowlistParameter string
}

func loadConfig(getenv func(string) string, manifest *Manifest) (Environment, error) {
	env := Environment{
		DNS:                DNSPointer{ZoneID: getenv("CLOUDFLARE_ZONE_ID"), TokenParameter: getenv("CLOUDFLARE_TOKEN_PARAMETER")},
		ModsParameter:      getenv("MODS_PARAMETER"),
		AllowlistParameter: getenv("ALLOWLIST_PARAMETER"),
	}
	port, err := strconv.Atoi(getenv("BEDROCK_PORT"))
	if err != nil || port < 1 || port > 65535 {
		return Environment{}, errors.New("BEDROCK_PORT must be a port number")
	}
	if !cloudflareID.MatchString(env.DNS.ZoneID) || !parameterName.MatchString(env.DNS.TokenParameter) {
		return Environment{}, errors.New("valid CLOUDFLARE_ZONE_ID and CLOUDFLARE_TOKEN_PARAMETER are required")
	}
	if !parameterName.MatchString(env.ModsParameter) || !parameterName.MatchString(env.AllowlistParameter) {
		return Environment{}, errors.New("MODS_PARAMETER and ALLOWLIST_PARAMETER must be parameter names")
	}
	// WORLDS is OpenTofu's: each world's host, hostname and state parameters under the world's id. The idle
	// stop gets the same value.
	var worlds map[string]struct {
		HostID                 string `json:"host_id"`
		Hostname               string `json:"hostname"`
		LauncherStateParameter string `json:"launcher_state_parameter"`
		IdleStopStateParameter string `json:"idle_stop_state_parameter"`
	}
	mismatch := errors.New("WORLDS must hold exactly the worlds of the pinned server content")
	if json.Unmarshal([]byte(getenv("WORLDS")), &worlds) != nil || len(worlds) != len(manifest.Worlds) {
		return Environment{}, mismatch
	}
	for _, world := range manifest.Worlds {
		settings, ok := worlds[world.ID]
		switch {
		case !ok:
			return Environment{}, mismatch
		case settings.HostID == "" || !parameterName.MatchString(settings.LauncherStateParameter):
			return Environment{}, fmt.Errorf("WORLDS needs a host_id and a launcher_state_parameter for %s", world.ID)
		case len(settings.Hostname) > 253 || !hostname.MatchString(settings.Hostname):
			return Environment{}, fmt.Errorf("WORLDS needs a valid hostname for %s", world.ID)
		case settings.IdleStopStateParameter != "" && !parameterName.MatchString(settings.IdleStopStateParameter):
			return Environment{}, fmt.Errorf("WORLDS needs a parameter name, or nothing, as the idle_stop_state_parameter of %s", world.ID)
		}
		env.Worlds = append(env.Worlds, Config{
			HostID:                 settings.HostID,
			Instance:               world.ID,
			ServerName:             world.Name,
			PublicHostname:         settings.Hostname,
			BedrockPort:            port,
			StateParameter:         settings.LauncherStateParameter,
			IdleStopStateParameter: settings.IdleStopStateParameter,
		})
	}
	return env, nil
}

// adapterSource answers the host adapter files: the package's adapter folder on Lambda
// (scripts/package_idle_stop.py), anywhere else the checkout that holds the working directory, found by looking
// upward from it.
func adapterSource(getenv func(string) string) fs.FS {
	if root := getenv("LAMBDA_TASK_ROOT"); root != "" {
		return os.DirFS(filepath.Join(root, "adapter"))
	}
	return checkout{os.DirFS(checkoutRoot())}
}

// checkoutRoot answers the checkout that holds the working directory, or the working directory itself.
func checkoutRoot() string {
	working, err := os.Getwd()
	if err != nil {
		return "."
	}
	for directory := working; ; {
		if _, err := os.Stat(filepath.Join(directory, "scripts", "minecraft_crafty.py")); err == nil {
			return directory
		}
		parent := filepath.Dir(directory)
		if parent == directory {
			return working
		}
		directory = parent
	}
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	source := adapterSource(os.Getenv)
	adapter, err := LoadHostAdapter(source)
	if err != nil {
		log.Error("host adapter", "err", err)
		os.Exit(1)
	}
	manifest, err := LoadManifest(source)
	if err != nil {
		log.Error("pinned server content", "err", err)
		os.Exit(1)
	}
	env, err := loadConfig(os.Getenv, manifest)
	if err != nil {
		log.Error("configuration", "err", err)
		os.Exit(1)
	}
	awsConfig, err := config.LoadDefaultConfig(context.Background())
	if err != nil {
		log.Error("load AWS configuration", "err", err)
		os.Exit(1)
	}
	ec2Client := ec2.NewFromConfig(awsConfig)
	ssmClient := ssm.NewFromConfig(awsConfig)
	worlds := &Worlds{
		Manifest:           manifest,
		Launchers:          map[string]*Launcher{},
		SSM:                ssmClient,
		ModsParameter:      env.ModsParameter,
		AllowlistParameter: env.AllowlistParameter,
		LookupPlayer:       (&PlayerLookup{Client: lookupClient()}).Lookup,
	}
	for _, cfg := range env.Worlds {
		// Each world has its own DNS record, in the one zone, changed with the one token.
		dns := env.DNS
		dns.Hostname, dns.Parameters, dns.Client = cfg.PublicHostname, ssmClient, cloudflareClient()
		worlds.Add(&Launcher{
			Config:   cfg,
			EC2:      ec2Client,
			SSM:      ssmClient,
			Ping:     PingPlayers,
			PointDNS: dns.Point,
			Adapter:  adapter,
			Now:      time.Now,
		})
	}
	server := &Server{Launcher: worlds, Log: log}

	if name := os.Getenv("AWS_LAMBDA_FUNCTION_NAME"); os.Getenv("AWS_LAMBDA_RUNTIME_API") != "" && name != "" {
		invoker := awslambda.NewFromConfig(awsConfig)
		worlds.HandOff(func(ctx context.Context, t task) error {
			payload, _ := json.Marshal(t)
			_, err := invoker.Invoke(ctx, &awslambda.InvokeInput{
				FunctionName:   aws.String(name),
				InvocationType: lambdatypes.InvocationTypeEvent,
				Payload:        payload,
			})
			return err
		})
		server.StartFailed = func() { writeMetric(os.Stdout, name, startFailureMetric, time.Now()) }
		server.StopFailed = func() { writeMetric(os.Stdout, name, stopFailureMetric, time.Now()) }
		server.Operator = signedOperator(RoleNames(os.Getenv("SITE_ROLE_ARNS")))
		proxy := httpadapter.NewV2(server.Handler())
		lambda.Start(func(ctx context.Context, raw json.RawMessage) (any, error) {
			var t task
			if json.Unmarshal(raw, &t) == nil && t.Task != "" {
				return nil, t.run(ctx, worlds, log)
			}
			var request events.APIGatewayV2HTTPRequest
			if err := json.Unmarshal(raw, &request); err != nil {
				return nil, errors.New("unrecognized event")
			}
			return proxy.ProxyWithContext(ctx, request)
		})
		return
	}

	worlds.HandOff(func(_ context.Context, t task) error {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
			defer cancel()
			_ = t.run(ctx, worlds, log)
		}()
		return nil
	})
	addr := os.Getenv("LAUNCHER_ADDR")
	if addr == "" {
		addr = "127.0.0.1:8082"
	}
	srv := &http.Server{Addr: addr, Handler: server.Handler(), ReadHeaderTimeout: 5 * time.Second}
	log.Info("listening", "addr", "http://"+addr)
	if err := srv.ListenAndServe(); err != nil {
		log.Error("serve", "err", err)
		os.Exit(1)
	}
}

// signedOperator reads the IAM caller the Function URL verified from the request's event and admits it as an
// operator unless it is one of the sites' roles. A request without that caller (the local server) admits nobody.
func signedOperator(siteRoles map[string]bool) func(*http.Request) bool {
	return func(r *http.Request) bool {
		request, ok := core.GetAPIGatewayV2ContextFromContext(r.Context())
		if !ok || request.Authorizer == nil || request.Authorizer.IAM == nil {
			return false
		}
		return OperatorCaller(request.Authorizer.IAM.UserARN, siteRoles)
	}
}

// finish runs a world's follow-up with time left to record a failure before Lambda ends the run.
func finish(ctx context.Context, name, world string, run func(context.Context) error, log *slog.Logger) error {
	if deadline, ok := ctx.Deadline(); ok {
		var cancel context.CancelFunc
		ctx, cancel = context.WithDeadline(ctx, deadline.Add(-followUpMargin))
		defer cancel()
	}
	err := run(ctx)
	if err != nil {
		log.Error(name, "world", world, "err", err)
	} else {
		log.Info(name, "world", world, "outcome", "done")
	}
	return err
}

// The custom metrics the launcher's alarm watches with Lambda's own errors (infra/launcher.tf).
const (
	metricNamespace    = "Minecraft/Launcher"
	startFailureMetric = "StartFailures"
	stopFailureMetric  = "StopFailures"
)

// writeMetric records one count of a metric in CloudWatch's embedded metric format. Lambda ships the line
// to the function's logs and CloudWatch turns it into the metric, so the function needs no metrics permission.
func writeMetric(w io.Writer, function, metric string, at time.Time) {
	line, _ := json.Marshal(map[string]any{
		"_aws": map[string]any{
			"Timestamp": at.UnixMilli(),
			"CloudWatchMetrics": []map[string]any{{
				"Namespace":  metricNamespace,
				"Dimensions": [][]string{{"FunctionName"}},
				"Metrics":    []map[string]string{{"Name": metric, "Unit": "Count"}},
			}},
		},
		"FunctionName": function,
		metric:         1,
	})
	fmt.Fprintln(w, string(line))
}
