// Command idlestop is a scheduled Lambda that drains and stops the Minecraft host after it has been idle.
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

func loadConfig(getenv func(string) string) (Config, error) {
	minutes, err := strconv.Atoi(getenv("IDLE_MINUTES"))
	if err != nil || minutes < 10 || minutes > 240 {
		return Config{}, errors.New("IDLE_MINUTES must be between 10 and 240")
	}
	instances, err := ParseInstances(getenv("INSTANCES"))
	if err != nil {
		return Config{}, err
	}
	cfg := Config{
		HostID:         getenv("HOST_ID"),
		StateParameter: getenv("STATE_PARAMETER"),
		GameAddress:    getenv("GAME_ADDRESS"),
		IdleAfter:      time.Duration(minutes) * time.Minute,
		Instances:      instances,
	}
	if cfg.HostID == "" || cfg.StateParameter == "" || cfg.GameAddress == "" {
		return Config{}, errors.New("HOST_ID, STATE_PARAMETER and GAME_ADDRESS are required")
	}
	return cfg, nil
}

func main() {
	cfg, err := loadConfig(os.Getenv)
	if err != nil {
		log.Fatal(err)
	}
	awsConfig, err := config.LoadDefaultConfig(context.Background())
	if err != nil {
		log.Fatal(fmt.Errorf("load AWS configuration: %w", err))
	}
	controller := &Controller{
		Config: cfg,
		EC2:    ec2.NewFromConfig(awsConfig),
		SSM:    ssm.NewFromConfig(awsConfig),
		Ping:   PingPlayers,
		Now:    time.Now,
	}
	lambda.Start(func(ctx context.Context) (Result, error) {
		result, err := controller.Run(ctx)
		line, _ := json.Marshal(result)
		log.Print(string(line))
		return result, err
	})
}
