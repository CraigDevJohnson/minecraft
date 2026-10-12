package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"time"
)

// The player lookups are the public identity services the server itself trusts: Mojang for Java accounts,
// and GeyserMC's XUID and link lookups for Bedrock gamertags, whose Floodgate UUID the server derives from
// the XUID.
const (
	mojangAPI = "https://api.mojang.com"
	geyserAPI = "https://api.geysermc.org"
	// lookupBodyLimit bounds what is read from either service; a profile is a few hundred bytes.
	lookupBodyLimit = 64 << 10
)

var undashedUUID = regexp.MustCompile(`^[0-9a-f]{32}$`)

// PlayerLookup resolves names through the two services. Both are fixed; redirects are refused.
type PlayerLookup struct {
	Client *http.Client
	Mojang string
	Geyser string
}

func lookupClient() *http.Client {
	return &http.Client{Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return errors.New("lookup redirect refused")
	}}
}

// Lookup answers the account the server knows under name, or ErrPlayerNotFound.
func (p *PlayerLookup) Lookup(ctx context.Context, edition, name string) (Player, error) {
	switch edition {
	case "java":
		return p.java(ctx, name)
	case "bedrock":
		return p.bedrock(ctx, name)
	}
	return Player{}, fmt.Errorf("%w: unknown edition", ErrBadAllowlistRequest)
}

// java asks Mojang's profile endpoint, which answers the account's undashed UUID and its exact name.
func (p *PlayerLookup) java(ctx context.Context, name string) (Player, error) {
	if !javaName.MatchString(name) {
		return Player{}, fmt.Errorf("%w: invalid Java name", ErrBadAllowlistRequest)
	}
	var profile struct {
		ID   string `json:"id"`
		Name string `json:"name"`
	}
	if err := p.get(ctx, p.base(p.Mojang, mojangAPI)+"/users/profiles/minecraft/"+url.PathEscape(name), "Mojang", &profile); err != nil {
		return Player{}, err
	}
	if !undashedUUID.MatchString(profile.ID) || !javaName.MatchString(profile.Name) {
		return Player{}, errors.New("Mojang answered an unusable profile")
	}
	id := profile.ID
	return Player{UUID: id[0:8] + "-" + id[8:12] + "-" + id[12:16] + "-" + id[16:20] + "-" + id[20:32], Name: profile.Name}, nil
}

// bedrock asks GeyserMC for the gamertag's XUID. Floodgate gives such a player the UUID whose high 64 bits
// are zero and low 64 bits are the XUID, which is what the server's allowlist holds. A gamertag linked to a
// Java account through GeyserMC's global linking, which Floodgate uses by default, joins as that Java account
// instead, so its own entry would neither admit nor remove the player: the lookup refuses it, and the Java
// account's name is the one to allowlist.
func (p *PlayerLookup) bedrock(ctx context.Context, gamertag string) (Player, error) {
	if !validGamertag(gamertag) {
		return Player{}, fmt.Errorf("%w: invalid gamertag", ErrBadAllowlistRequest)
	}
	geyser := p.base(p.Geyser, geyserAPI)
	var answer struct {
		XUID json.Number `json:"xuid"`
	}
	if err := p.get(ctx, geyser+"/v2/xbox/xuid/"+url.PathEscape(gamertag), "GeyserMC", &answer); err != nil {
		return Player{}, err
	}
	xuid, err := strconv.ParseUint(answer.XUID.String(), 10, 64)
	if err != nil || xuid == 0 {
		return Player{}, ErrPlayerNotFound
	}
	// The link lookup Floodgate itself makes: an empty object for a gamertag with no link.
	var link struct {
		JavaID   string `json:"java_id"`
		JavaName string `json:"java_name"`
	}
	switch err := p.get(ctx, geyser+"/v2/link/bedrock/"+strconv.FormatUint(xuid, 10), "GeyserMC", &link); {
	case errors.Is(err, ErrPlayerNotFound):
		return Player{}, errors.New("GeyserMC did not say whether the gamertag is linked")
	case err != nil:
		return Player{}, err
	}
	if link.JavaID != "" || link.JavaName != "" {
		return Player{}, ErrLinkedAccount
	}
	return Player{UUID: fmt.Sprintf("00000000-0000-0000-%04x-%012x", xuid>>48, xuid&0xffffffffffff), Name: gamertag}, nil
}

func (p *PlayerLookup) base(configured, standard string) string {
	if configured != "" {
		return configured
	}
	return standard
}

// get reads one JSON answer. A 404 (and Mojang's older 204) means the name is unknown; anything else that is
// not a 200 is the service's problem, reported without its body.
func (p *PlayerLookup) get(ctx context.Context, target, service string, into any) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return err
	}
	request.Header.Set("Accept", "application/json")
	client := p.Client
	if client == nil {
		client = lookupClient()
	}
	response, err := client.Do(request)
	if err != nil {
		return fmt.Errorf("%s lookup failed", service)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, lookupBodyLimit))
	if err != nil {
		return fmt.Errorf("%s lookup failed", service)
	}
	switch response.StatusCode {
	case http.StatusOK:
		if json.Unmarshal(body, into) != nil {
			return fmt.Errorf("%s answered something other than a profile", service)
		}
		return nil
	case http.StatusNotFound, http.StatusNoContent, http.StatusBadRequest:
		return ErrPlayerNotFound
	default:
		return fmt.Errorf("%s answered %d", service, response.StatusCode)
	}
}
