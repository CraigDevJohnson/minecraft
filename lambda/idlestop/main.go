// Command idlestop is a scheduled Lambda that drains and stops a world's Minecraft host after it has been idle.
// Each invocation checks the one world its event names.
//
// It never stops EC2 directly: it sends the same host drain that `minecraft_host.py stop` uses and calls
// StopInstances only after the drain proves every instance stopped with a completed backup.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/aws/aws-lambda-go/lambda"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/ec2"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
)

// Event is the schedule target's input: the one world this invocation checks.
type Event struct {
	World string `json:"world"`
}

// loadConfig answers each world's configuration under its id. WORLDS is OpenTofu's, the same value the
// launcher gets: each world's host, hostname and state parameters. Each world host runs one instance, named by
// the world id, on port 25565.
func loadConfig(getenv func(string) string) (map[string]Config, error) {
	minutes, err := strconv.Atoi(getenv("IDLE_MINUTES"))
	if err != nil || minutes < 10 || minutes > 240 {
		return nil, errors.New("IDLE_MINUTES must be between 10 and 240")
	}
	zone, token := getenv("CLOUDFLARE_ZONE_ID"), getenv("CLOUDFLARE_TOKEN_PARAMETER")
	if !cloudflareID.MatchString(zone) || !parameterName.MatchString(token) {
		return nil, errors.New("valid zone and token parameter are required")
	}
	var worlds map[string]struct {
		HostID                 string `json:"host_id"`
		Hostname               string `json:"hostname"`
		LauncherStateParameter string `json:"launcher_state_parameter"`
		IdleStopStateParameter string `json:"idle_stop_state_parameter"`
	}
	if json.Unmarshal([]byte(getenv("WORLDS")), &worlds) != nil || len(worlds) == 0 {
		return nil, errors.New("WORLDS must hold at least one world")
	}
	configs := map[string]Config{}
	for id, world := range worlds {
		switch {
		case !instanceID.MatchString(id):
			return nil, errors.New("WORLDS holds an invalid world id")
		case world.HostID == "" || !parameterName.MatchString(world.IdleStopStateParameter):
			return nil, fmt.Errorf("WORLDS needs a host_id and an idle_stop_state_parameter for %s", id)
		case len(world.Hostname) > 253 || !hostname.MatchString(world.Hostname):
			return nil, fmt.Errorf("WORLDS needs a valid hostname for %s", id)
		case world.LauncherStateParameter != "" && !parameterName.MatchString(world.LauncherStateParameter):
			return nil, fmt.Errorf("WORLDS needs a parameter name, or nothing, as the launcher_state_parameter of %s", id)
		}
		configs[id] = Config{
			HostID:                   world.HostID,
			StateParameter:           world.IdleStopStateParameter,
			IdleAfter:                time.Duration(minutes) * time.Minute,
			Instances:                []string{id},
			PublicHostname:           world.Hostname,
			CloudflareZoneID:         zone,
			CloudflareTokenParameter: token,
			LauncherStateParameter:   world.LauncherStateParameter,
		}
	}
	return configs, nil
}

// handle runs one idle check for the world the event names and logs its result under the world's id.
func handle(ctx context.Context, controllers map[string]*Controller, event Event) (Result, error) {
	controller, ok := controllers[event.World]
	if !ok {
		return Result{Action: "error"}, errors.New("the event must name a configured world")
	}
	result, err := controller.Run(ctx)
	line, _ := json.Marshal(struct {
		World string `json:"world"`
		Result
	}{event.World, result})
	log.Print(string(line))
	return result, err
}

func main() {
	configs, err := loadConfig(os.Getenv)
	if err != nil {
		log.Fatal(err)
	}
	awsConfig, err := config.LoadDefaultConfig(context.Background())
	if err != nil {
		log.Fatal(fmt.Errorf("load AWS configuration: %w", err))
	}
	ec2Client, ssmClient := ec2.NewFromConfig(awsConfig), ssm.NewFromConfig(awsConfig)
	controllers := map[string]*Controller{}
	for id, cfg := range configs {
		parker := &DNSParker{Hostname: cfg.PublicHostname, ZoneID: cfg.CloudflareZoneID, TokenParameter: cfg.CloudflareTokenParameter, Parameters: ssmClient, Client: cloudflareClient()}
		controllers[id] = &Controller{
			Config:  cfg,
			EC2:     ec2Client,
			SSM:     ssmClient,
			Ping:    PingPlayers,
			Now:     time.Now,
			ParkDNS: parker.Park,
		}
	}
	lambda.Start(func(ctx context.Context, event Event) (Result, error) {
		return handle(ctx, controllers, event)
	})
}
