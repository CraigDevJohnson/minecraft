package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/secretsmanager"
)

const cloudflareAPI = "https://api.cloudflare.com/client/v4"
const parkedAddress = "192.0.2.1"

var cloudflareID = regexp.MustCompile(`^[0-9a-f]{32}$`)
var hostname = regexp.MustCompile(`^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$`)
var secretARN = regexp.MustCompile(`^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}$`)
var apiToken = regexp.MustCompile(`^[\x21-\x7e]+$`)

type SecretsAPI interface {
	GetSecretValue(context.Context, *secretsmanager.GetSecretValueInput, ...func(*secretsmanager.Options)) (*secretsmanager.GetSecretValueOutput, error)
}

type DNSParker struct {
	Hostname  string
	ZoneID    string
	SecretARN string
	Secrets   SecretsAPI
	Client    *http.Client
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

func (p *DNSParker) Park(ctx context.Context) error {
	secret, err := p.Secrets.GetSecretValue(ctx, &secretsmanager.GetSecretValueInput{SecretId: aws.String(p.SecretARN)})
	if err != nil || secret == nil || !apiToken.MatchString(aws.ToString(secret.SecretString)) {
		return errors.New("cannot retrieve the Cloudflare token")
	}
	token := aws.ToString(secret.SecretString)
	path := "/zones/" + p.ZoneID + "/dns_records"
	var records []dnsRecord
	if err := p.request(ctx, token, "GET", path+"?"+url.Values{"name.exact": {p.Hostname}}.Encode(), nil, &records); err != nil {
		return err
	}
	if len(records) != 1 || records[0].Name != p.Hostname || records[0].Type != "A" || !cloudflareID.MatchString(records[0].ID) {
		return errors.New("the hostname must have exactly one A record")
	}
	record := records[0]
	if isParked(record) {
		return nil
	}
	body := map[string]any{"type": "A", "name": p.Hostname, "content": parkedAddress, "ttl": 60, "proxied": false}
	var updated dnsRecord
	if err := p.request(ctx, token, "PATCH", path+"/"+record.ID, body, &updated); err != nil {
		return err
	}
	if updated.ID != record.ID || updated.Name != p.Hostname || updated.Type != "A" || !isParked(updated) {
		return errors.New("Cloudflare did not confirm DNS parking")
	}
	return nil
}

func isParked(record dnsRecord) bool {
	return record.Content == parkedAddress && record.TTL == 60 && record.Proxied != nil && !*record.Proxied
}

func (p *DNSParker) request(ctx context.Context, token, method, path string, body any, result any) error {
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
