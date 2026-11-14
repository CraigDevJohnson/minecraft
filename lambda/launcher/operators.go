package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"strings"
	"sync"
)

// Each world's operators are kept in one parameter every world shares, as {"survival": [{edition, uuid, name}]}:
// accounts from the allowlist of record that the world makes operators. Each start writes its world's list over
// the stopped server's ops.json, so a world has no operator but the ones listed here, and a change reaches a
// running world through the host adapter's operator action, which runs op or deop on its console.

// operatorLevel is what an ops.json entry grants: server.properties' default op-permission-level, which the
// console's op grants too.
const operatorLevel = 4

// ErrNotAllowlisted means the player named is not on the allowlist of record, so cannot be an operator.
var ErrNotAllowlisted = errors.New("the player is not on the allowlist")

// PlayerName is a player as GET /server/allowlist and GET /server/operators show one: the edition and the name an
// admin would type to add them, the Java account name or the Bedrock gamertag as written.
type PlayerName struct {
	Edition string `json:"edition"`
	Name    string `json:"name"`
}

// OperatorsList is the answer to GET /server/operators: every world in the order the page shows them.
type OperatorsList struct {
	Worlds []WorldOperators `json:"worlds"`
}

type WorldOperators struct {
	ID        string       `json:"id"`
	Operators []PlayerName `json:"operators"`
}

// OperatorRequest is the body of POST /server/operators.
type OperatorRequest struct {
	World     string `json:"world"`
	Operation string `json:"operation"` // add or remove
	Edition   string `json:"edition"`   // java or bedrock
	Name      string `json:"name"`
}

// OperatorResult is the answer to an operator change, which carries no player identity.
type OperatorResult struct {
	World     string `json:"world"`
	Operation string `json:"operation"`
	Operators string `json:"operators"` // updated or unchanged
}

// Operators lists each world's operators.
func (w *Worlds) Operators(ctx context.Context) (OperatorsList, error) {
	record, err := w.operators(ctx)
	if err != nil {
		return OperatorsList{}, err
	}
	list := OperatorsList{Worlds: []WorldOperators{}}
	for _, id := range w.Worlds() {
		names := []PlayerName{}
		for _, entry := range record[id] {
			names = append(names, PlayerName{Edition: entry.Edition, Name: entry.Name})
		}
		list.Worlds = append(list.Worlds, WorldOperators{ID: id, Operators: names})
	}
	return list, nil
}

// ChangeOperator makes a player on the allowlist an operator of one world, or no longer one, on the record and
// then on the world's server while its game is running. When the running server cannot be changed the record
// keeps the change and the request fails, so asking again finishes it there; otherwise the next start does.
func (w *Worlds) ChangeOperator(ctx context.Context, request OperatorRequest) (OperatorResult, error) {
	if err := ValidateAllowlistRequest(AllowlistRequest{Operation: request.Operation, Edition: request.Edition, Name: request.Name}); err != nil {
		return OperatorResult{}, err
	}
	launcher, ok := w.Launchers[request.World]
	if !ok {
		return OperatorResult{}, ErrUnknownWorld
	}
	record, err := w.operators(ctx)
	if err != nil {
		return OperatorResult{}, err
	}
	named := func(entry Listed) bool {
		return entry.Edition == request.Edition && strings.EqualFold(entry.Name, request.Name)
	}
	operators, changed := record[request.World], false
	var player Listed
	if request.Operation == "add" {
		var allowlist []Listed
		if err := w.read(ctx, w.AllowlistParameter, "allowlist", &allowlist); err != nil {
			return OperatorResult{}, err
		}
		at := slices.IndexFunc(allowlist, named)
		if at < 0 {
			return OperatorResult{}, ErrNotAllowlisted
		}
		player = allowlist[at]
		if !slices.ContainsFunc(operators, func(entry Listed) bool { return entry.UUID == player.UUID }) {
			operators, changed = append(operators, player), true
		}
	} else if at := slices.IndexFunc(operators, named); at >= 0 {
		player = operators[at]
		operators, changed = slices.Delete(operators, at, at+1), true
	} else {
		// Not an operator on the record, but an earlier removal may have changed the record and failed on the running
		// server, so a player on the allowlist is still taken off it there.
		var allowlist []Listed
		if err := w.read(ctx, w.AllowlistParameter, "allowlist", &allowlist); err != nil {
			return OperatorResult{}, err
		}
		at := slices.IndexFunc(allowlist, named)
		if at < 0 {
			return OperatorResult{World: request.World, Operation: request.Operation, Operators: "unchanged"}, nil
		}
		player = allowlist[at]
	}
	if changed {
		record[request.World] = operators
		if len(operators) == 0 {
			delete(record, request.World)
		}
		if err := w.write(ctx, w.OperatorsParameter, "operators", record); err != nil {
			return OperatorResult{}, err
		}
	}
	ctx, cancel := context.WithTimeout(ctx, allowlistTimeout)
	defer cancel()
	verdict, err := launcher.operatorLive(ctx, request.Operation, player)
	if err != nil {
		return OperatorResult{}, fmt.Errorf("world %s: %w", request.World, err)
	}
	result := OperatorResult{World: request.World, Operation: request.Operation, Operators: "unchanged"}
	if changed || verdict == "updated" {
		result.Operators = "updated"
	}
	return result, nil
}

// operators reads the operators record: for each world, the accounts it makes operators.
func (w *Worlds) operators(ctx context.Context) (map[string][]Listed, error) {
	record := map[string][]Listed{}
	if err := w.read(ctx, w.OperatorsParameter, "operators", &record); err != nil {
		return nil, err
	}
	if record == nil {
		record = map[string][]Listed{}
	}
	return record, nil
}

// dropOperator takes a player leaving the allowlist off every world's operators on the record, and answers the
// entry each world held, so the running ones can be changed too. A removal by name alone matches the Java entry
// held under that name.
func (w *Worlds) dropOperator(ctx context.Context, player Player, byName bool) (map[string]Listed, error) {
	record, err := w.operators(ctx)
	if err != nil {
		return nil, err
	}
	dropped := map[string]Listed{}
	for id, entries := range record {
		at := slices.IndexFunc(entries, func(entry Listed) bool {
			if byName {
				return entry.Edition == "java" && strings.EqualFold(entry.Name, player.Name)
			}
			return entry.UUID == player.UUID
		})
		if at < 0 {
			continue
		}
		dropped[id] = entries[at]
		if record[id] = slices.Delete(entries, at, at+1); len(record[id]) == 0 {
			delete(record, id)
		}
	}
	if len(dropped) == 0 {
		return dropped, nil
	}
	return dropped, w.write(ctx, w.OperatorsParameter, "operators", record)
}

// deopRunning takes a player leaving the allowlist off each world's operators while its game runs, side by side,
// and reports whether a server's list changed. Each world is asked whether or not the record held the player
// there, so a retry finishes a removal an earlier attempt took off the record but not off a running server. A
// removal by name alone has no UUID to look for, so only the worlds whose record held the name are asked.
func (w *Worlds) deopRunning(ctx context.Context, dropped map[string]Listed, player Listed, byName bool) (bool, error) {
	ask := map[string]Listed{}
	for _, id := range w.Worlds() {
		if entry, ok := dropped[id]; ok {
			ask[id] = entry
		} else if !byName {
			ask[id] = player
		}
	}
	errs := make([]error, 0, len(ask))
	updated := false
	var mu sync.Mutex
	var running sync.WaitGroup
	for id, entry := range ask {
		running.Go(func() {
			verdict, err := w.Launchers[id].operatorLive(ctx, "remove", entry)
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				errs = append(errs, fmt.Errorf("world %s operators: %w", id, err))
			}
			updated = updated || verdict == "updated"
		})
	}
	running.Wait()
	return updated, errors.Join(errs...)
}

// opsFile answers a world's operators as the ops.json a start writes over the stopped server's own. A Bedrock
// player's name is the one Floodgate writes.
func (w *Worlds) opsFile(ctx context.Context, world string) ([]byte, error) {
	record, err := w.operators(ctx)
	if err != nil {
		return nil, err
	}
	type op struct {
		UUID                string `json:"uuid"`
		Name                string `json:"name"`
		Level               int    `json:"level"`
		BypassesPlayerLimit bool   `json:"bypassesPlayerLimit"`
	}
	entries := []op{}
	for _, entry := range record[world] {
		entries = append(entries, op{UUID: entry.UUID, Name: serverName(entry), Level: operatorLevel})
	}
	return json.Marshal(entries)
}

// operatorLive runs the adapter's operator action on this world's server while its game answers, by the rules
// allowlistLive follows, and reports what became of the server's own list: updated or unchanged.
func (l *Launcher) operatorLive(ctx context.Context, operation string, player Listed) (string, error) {
	script := playerScript("operator", l.Config.Instance, operation, player.Edition, player.Player)
	return l.changeLive(ctx, "operator change", script, func(output string, code int32) (string, error) {
		return checkOperator(output, code, l.Config.Instance, operation, player.Edition)
	})
}

// checkOperator reads the adapter's answer, which must report the change asked for, and answers what became of
// the server's operators: updated or unchanged.
func checkOperator(output string, code int32, instance, operation, edition string) (string, error) {
	if code != 0 {
		return "", errors.New("the host's adapter refused the operator change" + adapterError(output))
	}
	var result struct {
		Instance  string `json:"instance"`
		Edition   string `json:"edition"`
		Operation string `json:"operation"`
		Operators string `json:"operators"`
	}
	if json.Unmarshal([]byte(output), &result) != nil || result.Instance != instance || result.Edition != edition || result.Operation != operation ||
		(result.Operators != "updated" && result.Operators != "unchanged") {
		return "", errors.New("the host's adapter did not confirm the operator change")
	}
	return result.Operators, nil
}
