package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode"
	"unicode/utf16"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

// An allowlist change runs the host adapter's allowlist action on the live server, as
// docs/operations/allowlist.md describes for the workstation, with the player's verified identity looked up
// first so a typo never reaches the server.
const (
	// allowlistTimeout bounds the whole change: SSM delivery, the adapter's own 60-second verification and
	// margin. The caller waits for the answer.
	allowlistTimeout = 3 * time.Minute
	// allowlistExecutionSeconds is the adapter's execution limit on the host.
	allowlistExecutionSeconds = 120
)

var javaName = regexp.MustCompile(`^[A-Za-z0-9_]{3,16}$`)

// AllowlistRequest is the body of POST /server/allowlist.
type AllowlistRequest struct {
	Operation string `json:"operation"` // add or remove
	Edition   string `json:"edition"`   // java or bedrock
	Name      string `json:"name"`      // a Java account name or a Bedrock gamertag
}

// AllowlistResult is the adapter's answer, which carries no player identity.
type AllowlistResult struct {
	Instance  string `json:"instance"`
	Edition   string `json:"edition"`
	Operation string `json:"operation"`
	Allowlist string `json:"allowlist"` // updated or unchanged
}

// Player is the verified identity the adapter's --player-file carries: the server's UUID for the account, in
// canonical lowercase form, and the account's name.
type Player struct {
	UUID string `json:"uuid"`
	Name string `json:"name"`
}

var (
	// ErrBadAllowlistRequest means the request names no valid operation, edition or player.
	ErrBadAllowlistRequest = errors.New("bad allowlist request")
	// ErrPlayerNotFound means no account of that edition has the name.
	ErrPlayerNotFound = errors.New("no such player")
	// ErrLinkedAccount means the gamertag is linked to a Java account, which is the identity the server
	// sees when that player joins: the Java account's name is the one to allowlist.
	ErrLinkedAccount = errors.New("the gamertag is linked to a Java account")
)

// ErrServerNotReady refuses an allowlist change while the game is not answering: the adapter changes the
// allowlist through the live server.
type ErrServerNotReady struct{ State string }

func (e ErrServerNotReady) Error() string { return "the server is " + e.State + ", not ready" }

// ValidateAllowlistRequest applies the adapter's own rules to the request before anything is looked up or
// sent: Java names are 3 to 16 ASCII letters, digits or underscores; Bedrock gamertags are letters, digits,
// spaces or underscores within 16 UTF-16 units, with no leading or trailing space.
func ValidateAllowlistRequest(request AllowlistRequest) error {
	if request.Operation != "add" && request.Operation != "remove" {
		return fmt.Errorf("%w: operation must be add or remove", ErrBadAllowlistRequest)
	}
	switch request.Edition {
	case "java":
		if !javaName.MatchString(request.Name) {
			return fmt.Errorf("%w: a Java name is 3 to 16 letters, digits or underscores", ErrBadAllowlistRequest)
		}
	case "bedrock":
		if !validGamertag(request.Name) {
			return fmt.Errorf("%w: a gamertag is letters, digits, spaces or underscores within 16 characters", ErrBadAllowlistRequest)
		}
	default:
		return fmt.Errorf("%w: edition must be java or bedrock", ErrBadAllowlistRequest)
	}
	return nil
}

func validGamertag(name string) bool {
	if name == "" || name != strings.TrimSpace(name) || len(utf16.Encode([]rune(name))) > 16 {
		return false
	}
	for _, r := range name {
		if !unicode.IsLetter(r) && !unicode.IsNumber(r) && r != ' ' && r != '_' {
			return false
		}
	}
	return true
}

// Allowlist adds or removes one player on the running server and answers the adapter's result.
func (l *Launcher) Allowlist(ctx context.Context, request AllowlistRequest) (AllowlistResult, error) {
	if l.Config.HostID == "" {
		return AllowlistResult{}, ErrNoServer
	}
	if err := ValidateAllowlistRequest(request); err != nil {
		return AllowlistResult{}, err
	}
	h, err := l.describeHost(ctx)
	if err != nil {
		return AllowlistResult{}, err
	}
	session, err := l.loadSession(ctx)
	if err != nil {
		return AllowlistResult{}, err
	}
	state, err := l.decide(ctx, h, session)
	if err != nil {
		return AllowlistResult{}, err
	}
	if state != Ready {
		return AllowlistResult{}, ErrServerNotReady{State: state}
	}
	player, err := l.LookupPlayer(ctx, request.Edition, request.Name)
	if errors.Is(err, ErrPlayerNotFound) || errors.Is(err, ErrLinkedAccount) {
		return AllowlistResult{}, err
	}
	if err != nil {
		return AllowlistResult{}, fmt.Errorf("look up player: %w", err)
	}
	ctx, cancel := context.WithTimeout(ctx, allowlistTimeout)
	defer cancel()
	id, err := l.sendCommand(ctx, allowlistScript(l.Config.Instance, request, player), "minecraft launcher allowlist", allowlistExecutionSeconds)
	if err != nil {
		return AllowlistResult{}, fmt.Errorf("send allowlist change: %w", err)
	}
	for {
		invocation, err := l.SSM.GetCommandInvocation(ctx, &ssm.GetCommandInvocationInput{CommandId: aws.String(id), InstanceId: aws.String(l.Config.HostID)})
		var missing *ssmtypes.InvocationDoesNotExist
		switch {
		case errors.As(err, &missing):
		case err != nil:
			return AllowlistResult{}, fmt.Errorf("follow allowlist change: %w", err)
		case invocation.Status == ssmtypes.CommandInvocationStatusSuccess:
			return checkAllowlisted(aws.ToString(invocation.StandardOutputContent), invocation.ResponseCode, l.Config.Instance, request)
		case invocation.Status == ssmtypes.CommandInvocationStatusPending, invocation.Status == ssmtypes.CommandInvocationStatusInProgress, invocation.Status == ssmtypes.CommandInvocationStatusDelayed:
		default:
			return AllowlistResult{}, fmt.Errorf("allowlist change ended with status %s", invocation.Status)
		}
		if err := l.pause(ctx, "the allowlist change did not finish"); err != nil {
			return AllowlistResult{}, err
		}
	}
}

// allowlistScript writes the player's identity to a private file on the host (mktemp creates it with mode
// 0600), runs the adapter on it, and removes it. The identity travels base64-encoded, so the script carries
// only characters the single quotes keep literal; the instance, operation and edition were validated.
func allowlistScript(instance string, request AllowlistRequest, player Player) string {
	encoded, _ := json.Marshal(player)
	return strings.Join([]string{
		"set -eu",
		`player="$(mktemp)"`,
		`trap 'rm -f "$player"' EXIT`,
		"printf '%s' '" + base64.StdEncoding.EncodeToString(encoded) + "' | base64 -d > \"$player\"",
		remoteCommand + " allowlist " + instance + " " + request.Operation + " --edition " + request.Edition + ` --player-file "$player"`,
	}, "\n")
}

// checkAllowlisted reads the adapter's answer: the change it reports must be the one asked for.
func checkAllowlisted(output string, code int32, instance string, request AllowlistRequest) (AllowlistResult, error) {
	if code != 0 {
		return AllowlistResult{}, errors.New("the host's adapter refused the allowlist change")
	}
	var result AllowlistResult
	if json.Unmarshal([]byte(output), &result) != nil || result.Instance != instance || result.Edition != request.Edition || result.Operation != request.Operation || (result.Allowlist != "updated" && result.Allowlist != "unchanged") {
		return AllowlistResult{}, errors.New("the host's adapter did not confirm the allowlist change")
	}
	return result, nil
}
