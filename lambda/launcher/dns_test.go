package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/ssm"
	ssmtypes "github.com/aws/aws-sdk-go-v2/service/ssm/types"
)

const (
	token      = "cf-token-value"
	zoneID     = "0123456789abcdef0123456789abcdef"
	recordID   = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	tokenParam = "/minecraft/launcher/cloudflare-token"
)

type fakeTokenParameter struct {
	value     string
	err       error
	name      string
	decrypted bool
}

func (f *fakeTokenParameter) GetParameter(_ context.Context, input *ssm.GetParameterInput, _ ...func(*ssm.Options)) (*ssm.GetParameterOutput, error) {
	f.name = aws.ToString(input.Name)
	f.decrypted = aws.ToBool(input.WithDecryption)
	if f.err != nil {
		return nil, f.err
	}
	return &ssm.GetParameterOutput{Parameter: &ssmtypes.Parameter{Value: aws.String(f.value)}}, nil
}

type roundTripper func(*http.Request) (*http.Response, error)

func (f roundTripper) RoundTrip(request *http.Request) (*http.Response, error) { return f(request) }

func record(content string, ttl int, proxied bool) dnsRecord {
	return dnsRecord{ID: recordID, Name: "mc.example.com", Type: "A", Content: content, TTL: ttl, Proxied: aws.Bool(proxied)}
}

func dnsReply(result any) *http.Response {
	encoded, _ := json.Marshal(map[string]any{"success": true, "result": result})
	return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(string(encoded)))}
}

type cloudflare struct {
	records []dnsRecord
	patched map[string]any
	calls   []string
}

func (c *cloudflare) pointer(param *fakeTokenParameter) *DNSPointer {
	return &DNSPointer{Hostname: "mc.example.com", ZoneID: zoneID, TokenParameter: tokenParam, Parameters: param,
		Client: &http.Client{Transport: roundTripper(func(request *http.Request) (*http.Response, error) {
			if request.Header.Get("Authorization") != "Bearer "+token || request.URL.Host != "api.cloudflare.com" {
				return nil, errors.New("unexpected request")
			}
			c.calls = append(c.calls, request.Method+" "+request.URL.Path)
			if request.Method == "PATCH" {
				_ = json.NewDecoder(request.Body).Decode(&c.patched)
				updated := record(c.patched["content"].(string), int(c.patched["ttl"].(float64)), c.patched["proxied"].(bool))
				return dnsReply(updated), nil
			}
			if request.URL.Query().Get("name.exact") != "mc.example.com" {
				return nil, errors.New("unexpected query")
			}
			return dnsReply(c.records), nil
		})}}
}

func TestPointUpdatesTheOneRecord(t *testing.T) {
	cf := &cloudflare{records: []dnsRecord{record("192.0.2.1", 60, false)}}
	param := &fakeTokenParameter{value: token}
	if err := cf.pointer(param).Point(context.Background(), "203.0.113.10"); err != nil {
		t.Fatal(err)
	}
	if param.name != tokenParam || !param.decrypted {
		t.Fatalf("read %q decrypted=%v", param.name, param.decrypted)
	}
	if cf.patched["content"] != "203.0.113.10" || cf.patched["ttl"] != float64(60) || cf.patched["proxied"] != false || cf.patched["type"] != "A" {
		t.Fatalf("patched %v", cf.patched)
	}
	if strings.Join(cf.calls, ",") != "GET /client/v4/zones/"+zoneID+"/dns_records,PATCH /client/v4/zones/"+zoneID+"/dns_records/"+recordID {
		t.Fatalf("calls %v", cf.calls)
	}
}

func TestPointLeavesACurrentRecord(t *testing.T) {
	cf := &cloudflare{records: []dnsRecord{record("203.0.113.10", 60, false)}}
	if err := cf.pointer(&fakeTokenParameter{value: token}).Point(context.Background(), "203.0.113.10"); err != nil || len(cf.calls) != 1 {
		t.Fatalf("calls=%v err=%v", cf.calls, err)
	}
	// A proxied record or another TTL is not current: the proxy carries only HTTP.
	cf = &cloudflare{records: []dnsRecord{record("203.0.113.10", 60, true)}}
	if err := cf.pointer(&fakeTokenParameter{value: token}).Point(context.Background(), "203.0.113.10"); err != nil || len(cf.calls) != 2 {
		t.Fatalf("calls=%v err=%v", cf.calls, err)
	}
}

func TestPointRefusesAmbiguousRecords(t *testing.T) {
	for _, records := range [][]dnsRecord{
		{},
		{record("192.0.2.1", 60, false), record("192.0.2.2", 60, false)},
		{{ID: recordID, Name: "mc.example.com", Type: "CNAME", Content: "elsewhere.example.com", TTL: 60}},
	} {
		cf := &cloudflare{records: records}
		if err := cf.pointer(&fakeTokenParameter{value: token}).Point(context.Background(), "203.0.113.10"); err == nil || cf.patched != nil {
			t.Fatalf("records %v must be refused", records)
		}
	}
}

func TestPointNeedsAnAddressAndAToken(t *testing.T) {
	cf := &cloudflare{records: []dnsRecord{record("192.0.2.1", 60, false)}}
	for _, target := range []string{"", "not-an-ip", "2001:db8::1"} {
		if err := cf.pointer(&fakeTokenParameter{value: token}).Point(context.Background(), target); err == nil {
			t.Fatalf("address %q must be refused", target)
		}
	}
	for _, param := range []*fakeTokenParameter{{err: errors.New("AccessDenied: " + token)}, {value: ""}, {value: "two words"}} {
		err := cf.pointer(param).Point(context.Background(), "203.0.113.10")
		if err == nil || strings.Contains(err.Error(), token) {
			t.Fatalf("err=%v", err)
		}
	}
	if len(cf.calls) != 0 {
		t.Fatalf("called Cloudflare without a token: %v", cf.calls)
	}
}
