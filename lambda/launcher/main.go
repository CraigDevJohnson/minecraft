// Command launcher is the Minecraft Launcher's backend: it answers The Lobby's site with the family server's
// status and starts the server when a Member asks.
//
// On Lambda it serves the site through its Function URL, which only the site's role may call (the-lobby
// ADR 0006), and finishes each start in a second, asynchronous run of itself. Anywhere else it listens on a
// local port with the caller's own AWS credentials and finishes starts in the background.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"os"
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
	"github.com/awslabs/aws-lambda-go-api-proxy/httpadapter"
)

// finishStartTask is the payload of the asynchronous run that finishes a start.
const finishStartTask = "finish-start"

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
	ssmClient := ssm.NewFromConfig(awsConfig)
	dns.Parameters = ssmClient
	dns.Client = cloudflareClient()
	launcher := &Launcher{
		Config:   cfg,
		EC2:      ec2.NewFromConfig(awsConfig),
		SSM:      ssmClient,
		Ping:     PingPlayers,
		PointDNS: dns.Point,
		Now:      time.Now,
	}
	server := &Server{Launcher: launcher, Log: log}

	if name := os.Getenv("AWS_LAMBDA_FUNCTION_NAME"); os.Getenv("AWS_LAMBDA_RUNTIME_API") != "" && name != "" {
		invoker := awslambda.NewFromConfig(awsConfig)
		payload, _ := json.Marshal(map[string]string{"launcher_task": finishStartTask})
		launcher.FollowUp = func(ctx context.Context) error {
			_, err := invoker.Invoke(ctx, &awslambda.InvokeInput{
				FunctionName:   aws.String(name),
				InvocationType: lambdatypes.InvocationTypeEvent,
				Payload:        payload,
			})
			return err
		}
		proxy := httpadapter.NewV2(server.Handler())
		lambda.Start(func(ctx context.Context, raw json.RawMessage) (any, error) {
			var task struct {
				Task string `json:"launcher_task"`
			}
			if json.Unmarshal(raw, &task) == nil && task.Task != "" {
				if task.Task != finishStartTask {
					return nil, errors.New("unknown launcher task")
				}
				return nil, finishStart(ctx, launcher, log)
			}
			var request events.APIGatewayV2HTTPRequest
			if err := json.Unmarshal(raw, &request); err != nil {
				return nil, errors.New("unrecognized event")
			}
			return proxy.ProxyWithContext(ctx, request)
		})
		return
	}

	launcher.FollowUp = func(context.Context) error {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 15*time.Minute)
			defer cancel()
			_ = finishStart(ctx, launcher, log)
		}()
		return nil
	}
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

func finishStart(ctx context.Context, launcher *Launcher, log *slog.Logger) error {
	if deadline, ok := ctx.Deadline(); ok {
		var cancel context.CancelFunc
		ctx, cancel = context.WithDeadline(ctx, deadline.Add(-followUpMargin))
		defer cancel()
	}
	err := launcher.FinishStart(ctx)
	if err != nil {
		log.Error("finish start", "err", err)
	} else {
		log.Info("finish start", "state", Ready)
	}
	return err
}
