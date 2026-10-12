// Command launcher is the Minecraft Launcher's backend: it answers The Lobby's site with the family server's
// status, and starts or stops the server when a Member or the operator's workflow asks.
//
// On Lambda it serves the site through its Function URL, which only the site's role and the operator's
// control role may call (the-lobby ADR 0006, ADR 0006 here), and finishes each start or stop in a second,
// asynchronous run of itself. Anywhere else it listens on a local port with the caller's own AWS credentials
// and finishes them in the background.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
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

// The payloads of the asynchronous runs that finish a start and a stop.
const (
	finishStartTask = "finish-start"
	finishStopTask  = "finish-stop"
)

// followUpMargin leaves the follow-up run time to record a failure before Lambda ends it.
const followUpMargin = 20 * time.Second

func loadConfig(getenv func(string) string) (Config, *DNSPointer, error) {
	cfg := Config{
		HostID:                 getenv("HOST_ID"),
		Instance:               getenv("INSTANCE"),
		ServerName:             getenv("SERVER_NAME"),
		PublicHostname:         getenv("PUBLIC_HOSTNAME"),
		StateParameter:         getenv("STATE_PARAMETER"),
		IdleStopStateParameter: getenv("IDLE_STOP_STATE_PARAMETER"),
	}
	port, err := strconv.Atoi(getenv("BEDROCK_PORT"))
	if err != nil || port < 1 || port > 65535 {
		return Config{}, nil, errors.New("BEDROCK_PORT must be a port number")
	}
	cfg.BedrockPort = port
	if cfg.HostID == "" || cfg.Instance == "" || cfg.ServerName == "" || !parameterName.MatchString(cfg.StateParameter) {
		return Config{}, nil, errors.New("HOST_ID, INSTANCE, SERVER_NAME and STATE_PARAMETER are required")
	}
	if cfg.IdleStopStateParameter != "" && !parameterName.MatchString(cfg.IdleStopStateParameter) {
		return Config{}, nil, errors.New("IDLE_STOP_STATE_PARAMETER must be a parameter name")
	}
	dns := &DNSPointer{Hostname: cfg.PublicHostname, ZoneID: getenv("CLOUDFLARE_ZONE_ID"), TokenParameter: getenv("CLOUDFLARE_TOKEN_PARAMETER")}
	if len(dns.Hostname) > 253 || !hostname.MatchString(dns.Hostname) || !cloudflareID.MatchString(dns.ZoneID) || !parameterName.MatchString(dns.TokenParameter) {
		return Config{}, nil, errors.New("valid PUBLIC_HOSTNAME, CLOUDFLARE_ZONE_ID and CLOUDFLARE_TOKEN_PARAMETER are required")
	}
	return cfg, dns, nil
}

// adapterSource names the folder holding the host adapter files: the package's adapter folder on Lambda
// (scripts/package_idle_stop.py), the repository's scripts folder anywhere else.
func adapterSource(getenv func(string) string) string {
	if root := getenv("LAMBDA_TASK_ROOT"); root != "" {
		return filepath.Join(root, "adapter")
	}
	return filepath.Join("..", "..", "scripts")
}

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	cfg, dns, err := loadConfig(os.Getenv)
	if err != nil {
		log.Error("configuration", "err", err)
		os.Exit(1)
	}
	awsConfig, err := config.LoadDefaultConfig(context.Background())
	if err != nil {
		log.Error("load AWS configuration", "err", err)
		os.Exit(1)
	}
	adapter, err := LoadHostAdapter(os.DirFS(adapterSource(os.Getenv)))
	if err != nil {
		log.Error("host adapter", "err", err)
		os.Exit(1)
	}
	ssmClient := ssm.NewFromConfig(awsConfig)
	dns.Parameters = ssmClient
	dns.Client = cloudflareClient()
	launcher := &Launcher{
		Config:   cfg,
		EC2:      ec2.NewFromConfig(awsConfig),
		SSM:      ssmClient,
		Ping:     PingPlayers,
		PointDNS: dns.Point,
		Adapter:  adapter,
		Now:      time.Now,
	}
	launcher.LookupPlayer = (&PlayerLookup{Client: lookupClient()}).Lookup
	server := &Server{Launcher: launcher, Log: log}

	if name := os.Getenv("AWS_LAMBDA_FUNCTION_NAME"); os.Getenv("AWS_LAMBDA_RUNTIME_API") != "" && name != "" {
		invoker := awslambda.NewFromConfig(awsConfig)
		invokeSelf := func(task string) func(ctx context.Context) error {
			payload, _ := json.Marshal(map[string]string{"launcher_task": task})
			return func(ctx context.Context) error {
				_, err := invoker.Invoke(ctx, &awslambda.InvokeInput{
					FunctionName:   aws.String(name),
					InvocationType: lambdatypes.InvocationTypeEvent,
					Payload:        payload,
				})
				return err
			}
		}
		launcher.FollowUp = invokeSelf(finishStartTask)
		launcher.FollowUpStop = invokeSelf(finishStopTask)
		server.StartFailed = func() { writeMetric(os.Stdout, name, startFailureMetric, time.Now()) }
		server.StopFailed = func() { writeMetric(os.Stdout, name, stopFailureMetric, time.Now()) }
		server.Operator = signedOperator(RoleNames(os.Getenv("SITE_ROLE_ARNS")))
		proxy := httpadapter.NewV2(server.Handler())
		lambda.Start(func(ctx context.Context, raw json.RawMessage) (any, error) {
			var task struct {
				Task string `json:"launcher_task"`
			}
			if json.Unmarshal(raw, &task) == nil && task.Task != "" {
				switch task.Task {
				case finishStartTask:
					return nil, finish(ctx, "finish start", launcher.FinishStart, log)
				case finishStopTask:
					return nil, finish(ctx, "finish stop", launcher.FinishStop, log)
				}
				return nil, errors.New("unknown launcher task")
			}
			var request events.APIGatewayV2HTTPRequest
			if err := json.Unmarshal(raw, &request); err != nil {
				return nil, errors.New("unrecognized event")
			}
			return proxy.ProxyWithContext(ctx, request)
		})
		return
	}

	inBackground := func(name string, run func(context.Context) error) func(context.Context) error {
		return func(context.Context) error {
			go func() {
				ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
				defer cancel()
				_ = finish(ctx, name, run, log)
			}()
			return nil
		}
	}
	launcher.FollowUp = inBackground("finish start", launcher.FinishStart)
	launcher.FollowUpStop = inBackground("finish stop", launcher.FinishStop)
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

// finish runs a follow-up with time left to record a failure before Lambda ends the run.
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

func finish(ctx context.Context, name string, run func(context.Context) error, log *slog.Logger) error {
	if deadline, ok := ctx.Deadline(); ok {
		var cancel context.CancelFunc
		ctx, cancel = context.WithDeadline(ctx, deadline.Add(-followUpMargin))
		defer cancel()
	}
	err := run(ctx)
	if err != nil {
		log.Error(name, "err", err)
	} else {
		log.Info(name, "outcome", "done")
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
