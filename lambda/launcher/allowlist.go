package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf16"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

// The family has one allowlist of record, kept in a parameter. An allowlist change is made on that record, which
// every start carries to its world, and on each world whose game is running now, with the host adapter's
// allowlist action. The player's verified identity is looked up first, so a typo never reaches the record or a
// server.
const (
	// allowlistTimeout bounds the whole change on the running worlds, which are changed side by side: SSM
	// delivery, the adapter's execution limit and margin. The caller waits for the answer.
	allowlistTimeout = 6 * time.Minute
	// allowlistExecutionSeconds is the adapter's execution limit on the host. It covers every bounded step, so
	// a slow but working Crafty never cuts off a change already sent: the adapter install, up to four Crafty
	// API calls of up to 30 seconds each (the server list, its stats, then the console line, or a name-only
	// removal's reload and list), the 60-second verification, and margin.
	allowlistExecutionSeconds = 240
)

var javaName = regexp.MustCompile(`^[A-Za-z0-9_]{3,16}$`)

// AllowlistRequest is the body of POST /server/allowlist.
type AllowlistRequest struct {
	Operation string `json:"operation"` // add or remove
	Edition   string `json:"edition"`   // java or bedrock
	Name      string `json:"name"`      // a Java account name or a Bedrock gamertag
}

// AllowlistResult is the answer to an allowlist change, which carries no player identity.
type AllowlistResult struct {
	Edition   string `json:"edition"`
	Operation string `json:"operation"`
	Allowlist string `json:"allowlist"` // updated or unchanged
	// Worlds lists the running worlds the change also reached. Any other world gets the list at its next start.
	Worlds []string `json:"worlds"`
}

// Player is the verified identity the adapter's --player-file carries: the server's UUID for the account, in
// canonical lowercase form, and the account's name. A Java removal of a name Mojang no longer knows carries
// the name alone, and the adapter removes the entry the allowlist holds under it.
type Player struct {
	UUID string `json:"uuid,omitempty"`
	Name string `json:"name"`
}

// Listed is one entry of the allowlist of record: the account a lookup found, under the edition it was asked for.
type Listed struct {
	Edition string `json:"edition"`
	Player
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

// Allowlist adds or removes one player on the allowlist of record, then on each world whose game is running.
// It works with every world stopped. When a running world cannot be changed the record keeps the change and the
// request fails, so asking again finishes it there.
func (w *Worlds) Allowlist(ctx context.Context, request AllowlistRequest) (AllowlistResult, error) {
	if err := ValidateAllowlistRequest(request); err != nil {
		return AllowlistResult{}, err
	}
	player, err := w.LookupPlayer(ctx, request.Edition, request.Name)
	byName := false
	switch {
	case errors.Is(err, ErrPlayerNotFound) && request.Operation == "remove" && request.Edition == "java":
		// Mojang no longer knows the name, as after a rename, but the record or a running world may still hold
		// it: each removes the entry it holds under that name.
		player, byName = Player{Name: request.Name}, true
	case errors.Is(err, ErrPlayerNotFound) || errors.Is(err, ErrLinkedAccount):
		return AllowlistResult{}, err
	case err != nil:
		return AllowlistResult{}, fmt.Errorf("look up player: %w", err)
	}
	changed, err := w.changeRecord(ctx, request, player, byName)
	if err != nil {
		return AllowlistResult{}, err
	}
	ctx, cancel := context.WithTimeout(ctx, allowlistTimeout)
	defer cancel()
	ids := w.Worlds()
	verdicts, errs := make([]string, len(ids)), make([]error, len(ids))
	var running sync.WaitGroup
	for i, id := range ids {
		running.Go(func() { verdicts[i], errs[i] = w.Launchers[id].allowlistLive(ctx, request, player, byName) })
	}
	running.Wait()
	result := AllowlistResult{Edition: request.Edition, Operation: request.Operation, Allowlist: "unchanged", Worlds: []string{}}
	var failed error
	for i, id := range ids {
		if errs[i] != nil {
			failed = errors.Join(failed, fmt.Errorf("world %s: %w", id, errs[i]))
		}
		if verdicts[i] != "" {
			result.Worlds = append(result.Worlds, id)
		}
		changed = changed || verdicts[i] == "updated"
	}
	switch {
	case failed != nil:
		return AllowlistResult{}, failed
	case byName && !changed:
		// Neither the record nor a running world holds the name: the same answer as Mojang not knowing it.
		return AllowlistResult{}, ErrPlayerNotFound
	case changed:
		result.Allowlist = "updated"
	}
	return result, nil
}

// changeRecord makes the change on the allowlist of record and reports whether the list changed. A removal by
// name alone removes the Java entry the record holds under that name.
func (w *Worlds) changeRecord(ctx context.Context, request AllowlistRequest, player Player, byName bool) (bool, error) {
	var list []Listed
	if err := w.read(ctx, w.AllowlistParameter, "allowlist", &list); err != nil {
		return false, err
	}
	at := slices.IndexFunc(list, func(entry Listed) bool {
		if byName {
			return entry.Edition == "java" && strings.EqualFold(entry.Name, player.Name)
		}
		return entry.UUID == player.UUID
	})
	switch {
	case request.Operation == "add" && at < 0:
		list = append(list, Listed{Edition: request.Edition, Player: player})
	case request.Operation == "remove" && at >= 0:
		list = slices.Delete(list, at, at+1)
	default:
		return false, nil
	}
	return true, w.write(ctx, w.AllowlistParameter, "allowlist", list)
}

// whitelist answers the allowlist of record as the whitelist.json a start writes over the stopped server's own:
// each account's UUID and name. A Bedrock player's name is the one Floodgate writes, a dot and the gamertag with
// its spaces as underscores.
func (w *Worlds) whitelist(ctx context.Context) ([]byte, error) {
	var list []Listed
	if err := w.read(ctx, w.AllowlistParameter, "allowlist", &list); err != nil {
		return nil, err
	}
	entries := make([]Player, 0, len(list))
	for _, entry := range list {
		if entry.Edition == "bedrock" {
			entry.Name = "." + strings.ReplaceAll(entry.Name, " ", "_")
		}
		entries = append(entries, entry.Player)
	}
	return json.Marshal(entries)
}

// allowlistLive runs the adapter's allowlist action on this world's server while its game answers, and reports
// what became of the server's own list: updated or unchanged. A world in any other state is left alone and
// reports nothing, since its next start carries the list.
func (l *Launcher) allowlistLive(ctx context.Context, request AllowlistRequest, player Player, byName bool) (string, error) {
	h, err := l.describeHost(ctx)
	if err != nil {
		return "", err
	}
	session, err := l.loadSession(ctx)
	if err != nil {
		return "", err
	}
	state, err := l.decide(ctx, h, session)
	if err != nil || state != Ready {
		return "", err
	}
	id, err := l.sendCommand(ctx, l.onHost(allowlistScript(l.Config.Instance, request, player)), "minecraft launcher allowlist", allowlistExecutionSeconds)
	if err != nil {
		return "", fmt.Errorf("send allowlist change: %w", err)
	}
	for {
		invocation, err := l.SSM.GetCommandInvocation(ctx, &ssm.GetCommandInvocationInput{CommandId: aws.String(id), InstanceId: aws.String(l.Config.HostID)})
		var missing *ssmtypes.InvocationDoesNotExist
		switch {
		case errors.As(err, &missing):
		case err != nil:
			return "", fmt.Errorf("follow allowlist change: %w", err)
		case invocation.Status == ssmtypes.CommandInvocationStatusSuccess:
			return checkAllowlisted(aws.ToString(invocation.StandardOutputContent), invocation.ResponseCode, l.Config.Instance, request, byName)
		case invocation.Status == ssmtypes.CommandInvocationStatusPending, invocation.Status == ssmtypes.CommandInvocationStatusInProgress, invocation.Status == ssmtypes.CommandInvocationStatusDelayed:
		default:
			return "", fmt.Errorf("allowlist change %s ended with status %s%s", id, invocation.Status, adapterError(aws.ToString(invocation.StandardOutputContent)))
		}
		if err := l.pause(ctx, "the allowlist change did not finish"); err != nil {
			return "", err
		}
	}
}

// allowlistScript writes the player's identity to a private file on the host (mktemp creates it with mode
// 0600), runs the adapter on it, and removes it. The identity travels base64-encoded, so the script carries
// only characters the single quotes keep literal; the instance, operation and edition were validated. It runs
// under `set -eu` after the adapter's installation (onHost).
func allowlistScript(instance string, request AllowlistRequest, player Player) string {
	encoded, _ := json.Marshal(player)
	return strings.Join([]string{
		`player="$(mktemp)"`,
		`trap 'rm -f "$player"' EXIT`,
		"printf '%s' '" + base64.StdEncoding.EncodeToString(encoded) + "' | base64 -d > \"$player\"",
		remoteCommand + " allowlist " + instance + " " + request.Operation + " --edition " + request.Edition + ` --player-file "$player"`,
	}, "\n")
}

// checkAllowlisted reads the adapter's answer, which must report the change asked for, and answers what became
// of the server's list: updated or unchanged. A removal by name alone may also learn that the server holds no
// such name, which leaves its list unchanged.
func checkAllowlisted(output string, code int32, instance string, request AllowlistRequest, byName bool) (string, error) {
	if code != 0 {
		return "", errors.New("the host's adapter refused the allowlist change" + adapterError(output))
	}
	var result struct {
		Instance  string `json:"instance"`
		Edition   string `json:"edition"`
		Operation string `json:"operation"`
		Allowlist string `json:"allowlist"`
	}
	if json.Unmarshal([]byte(output), &result) != nil || result.Instance != instance || result.Edition != request.Edition || result.Operation != request.Operation {
		return "", errors.New("the host's adapter did not confirm the allowlist change")
	}
	switch {
	case result.Allowlist == "updated" || result.Allowlist == "unchanged":
		return result.Allowlist, nil
	case result.Allowlist == "not-listed" && byName:
		return "unchanged", nil
	}
	return "", errors.New("the host's adapter did not confirm the allowlist change")
}

// adapterError answers the adapter's own diagnostic, which is fixed text that names no player, for the log.
func adapterError(output string) string {
	var answer struct {
		Error string `json:"error"`
	}
	if json.Unmarshal([]byte(output), &answer) != nil || answer.Error == "" || len(answer.Error) > 200 {
		return ""
	}
	return ": " + answer.Error
}
