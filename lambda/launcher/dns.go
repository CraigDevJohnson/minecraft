package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
)

const cloudflareAPI = "https://api.cloudflare.com/client/v4"

// dnsTTL matches the workstation start, so players follow a new address within a minute.
const dnsTTL = 60

var cloudflareID = regexp.MustCompile(`^[0-9a-f]{32}$`)
var hostname = regexp.MustCompile(`^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$`)
var parameterName = regexp.MustCompile(`^/[A-Za-z0-9/_.-]{1,500}$`)
var apiToken = regexp.MustCompile(`^[\x21-\x7e]+$`)

// ParameterAPI reads one SecureString. It is the only way the function sees the Cloudflare token.
type ParameterAPI interface {
	GetParameter(context.Context, *ssm.GetParameterInput, ...func(*ssm.Options)) (*ssm.GetParameterOutput, error)
}

// DNSPointer points the players' hostname at the running host, as `minecraft_host.py start` does.
// The token is read at the moment it is needed, sent only to the fixed Cloudflare API, and never logged.
type DNSPointer struct {
	Hostname       string
	ZoneID         string
	TokenParameter string
	Parameters     ParameterAPI
	Client         *http.Client
}

type dnsRecord struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	Type    string `json:"type"`
	Content string `json:"content"`
	TTL     int    `json:"ttl"`
	Proxied *bool  `json:"proxied"`
}

func cloudflareClient() *http.Client {
	return &http.Client{Timeout: 15 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return errors.New("Cloudflare redirect refused")
	}}
}

// Point sets the hostname's one existing A record to address, DNS only. It never creates or deletes a record.
func (p *DNSPointer) Point(ctx context.Context, address string) error {
	if ip := net.ParseIP(address); ip == nil || ip.To4() == nil {
		return errors.New("the host has no IPv4 address to publish")
	}
	parameter, err := p.Parameters.GetParameter(ctx, &ssm.GetParameterInput{Name: aws.String(p.TokenParameter), WithDecryption: aws.Bool(true)})
	if err != nil || parameter == nil || parameter.Parameter == nil || !apiToken.MatchString(aws.ToString(parameter.Parameter.Value)) {
		return errors.New("cannot retrieve the Cloudflare token")
	}
	token := aws.ToString(parameter.Parameter.Value)
	path := "/zones/" + p.ZoneID + "/dns_records"
	var records []dnsRecord
	if err := p.request(ctx, token, "GET", path+"?"+url.Values{"name.exact": {p.Hostname}}.Encode(), nil, &records); err != nil {
		return err
	}
	// Any second record, such as a stray AAAA or CNAME, would send some clients elsewhere.
	if len(records) != 1 || records[0].Name != p.Hostname || records[0].Type != "A" || !cloudflareID.MatchString(records[0].ID) {
		return errors.New("the hostname must have exactly one A record")
	}
	record := records[0]
	if pointsAt(record, address) {
		return nil
	}
	body := map[string]any{"type": "A", "name": p.Hostname, "content": address, "ttl": dnsTTL, "proxied": false}
	var updated dnsRecord
	if err := p.request(ctx, token, "PATCH", path+"/"+record.ID, body, &updated); err != nil {
		return err
	}
	if updated.ID != record.ID || updated.Name != p.Hostname || updated.Type != "A" || !pointsAt(updated, address) {
		return errors.New("Cloudflare did not confirm the DNS update")
	}
	return nil
}

func pointsAt(record dnsRecord, address string) bool {
	return record.Content == address && record.TTL == dnsTTL && record.Proxied != nil && !*record.Proxied
}

func (p *DNSPointer) request(ctx context.Context, token, method, path string, body any, result any) error {
	var payload []byte
	if body != nil {
		payload, _ = json.Marshal(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, cloudflareAPI+path, bytes.NewReader(payload))
	if err != nil {
		return errors.New("invalid Cloudflare request")
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	response, err := p.Client.Do(req)
	if err != nil {
		return errors.New("Cloudflare request failed")
	}
	defer response.Body.Close()
	content, err := io.ReadAll(io.LimitReader(response.Body, (1<<20)+1))
	if err != nil || len(content) > 1<<20 || response.StatusCode != http.StatusOK {
		return errors.New("Cloudflare response failed")
	}
	var envelope struct {
		Success bool            `json:"success"`
		Result  json.RawMessage `json:"result"`
	}
	if json.Unmarshal(content, &envelope) != nil || !envelope.Success || json.Unmarshal(envelope.Result, result) != nil {
		return errors.New("invalid Cloudflare response")
	}
	return nil
}
