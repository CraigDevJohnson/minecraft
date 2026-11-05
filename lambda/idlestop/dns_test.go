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

const recordID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

func record(content string) dnsRecord {
	return dnsRecord{ID: recordID, Name: "mc.example.com", Type: "A", Content: content, TTL: 60, Proxied: aws.Bool(false)}
}

func dnsReply(result any) *http.Response {
	encoded, _ := json.Marshal(map[string]any{"success": true, "result": result})
	return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(string(encoded)))}
}

func TestDNSParksOnlyTheExistingRecordWithRuntimeToken(t *testing.T) {
	secret := &fakeTokenParameter{value: "fake-private-token"}
	calls := 0
	client := cloudflareClient()
	client.Transport = roundTripper(func(req *http.Request) (*http.Response, error) {
		calls++
		if req.URL.Host != "api.cloudflare.com" || req.URL.Scheme != "https" || req.Header.Get("Authorization") != "Bearer fake-private-token" {
			t.Fatal("token must go only to the fixed HTTPS Cloudflare API")
		}
		if calls == 1 {
			if req.Method != "GET" || req.URL.Query().Get("name.exact") != "mc.example.com" {
				t.Fatalf("unexpected lookup: %s", req.URL)
			}
			return dnsReply([]dnsRecord{record("192.0.2.10")}), nil
		}
		if req.Method != "PATCH" || !strings.HasSuffix(req.URL.Path, "/"+recordID) {
			t.Fatal("parking must update the existing record")
		}
		var body dnsRecord
		if json.NewDecoder(req.Body).Decode(&body) != nil || !isParked(body) || body.Name != "mc.example.com" || body.Type != "A" {
			t.Fatal("parking must use the documentation address, TTL 60 and DNS only")
		}
		return dnsReply(record(parkedAddress)), nil
	})
	parker := &DNSParker{Hostname: "mc.example.com", ZoneID: "0123456789abcdef0123456789abcdef", TokenParameter: "/minecraft/launcher/cloudflare-token", Parameters: secret, Client: client}
	if err := parker.Park(context.Background()); err != nil || calls != 2 || secret.name != "/minecraft/launcher/cloudflare-token" || !secret.decrypted {
		t.Fatalf("err=%v calls=%d parameter=%s decrypted=%v", err, calls, secret.name, secret.decrypted)
	}
}

func TestDNSRejectsAmbiguousRecordInventory(t *testing.T) {
	for _, records := range [][]dnsRecord{nil, {record("192.0.2.10"), record("192.0.2.20")}, {{ID: recordID, Name: "mc.example.com", Type: "AAAA"}}, {{ID: "../other-record", Name: "mc.example.com", Type: "A"}}} {
		client := cloudflareClient()
		calls := 0
		client.Transport = roundTripper(func(*http.Request) (*http.Response, error) { calls++; return dnsReply(records), nil })
		parker := &DNSParker{Hostname: "mc.example.com", ZoneID: "0123456789abcdef0123456789abcdef", TokenParameter: "/minecraft/launcher/cloudflare-token", Parameters: &fakeTokenParameter{value: "fake-token"}, Client: client}
		if err := parker.Park(context.Background()); err == nil || calls != 1 {
			t.Fatalf("invalid inventory must forbid a mutation: err=%v calls=%d", err, calls)
		}
	}
}

func TestDNSErrorsNeverExposeResponseOrSecret(t *testing.T) {
	for _, mode := range []string{"secret", "network", "response", "redirect", "unconfirmed"} {
		t.Run(mode, func(t *testing.T) {
			secret := &fakeTokenParameter{value: "fake-private-token"}
			if mode == "secret" {
				secret.err = errors.New("fake-private-token")
			}
			client := cloudflareClient()
			calls := 0
			client.Transport = roundTripper(func(req *http.Request) (*http.Response, error) {
				calls++
				if calls > 2 {
					t.Fatal("redirect must never be followed")
				}
				switch mode {
				case "network":
					return nil, errors.New("fake-private-token")
				case "response":
					return &http.Response{StatusCode: 401, Body: io.NopCloser(strings.NewReader("fake-private-token"))}, nil
				case "redirect":
					return &http.Response{StatusCode: 302, Header: http.Header{"Location": {"https://other.example/"}}, Body: io.NopCloser(strings.NewReader("fake-private-token"))}, nil
				default:
					if calls == 1 {
						return dnsReply([]dnsRecord{record("192.0.2.10")}), nil
					}
					return dnsReply(record("192.0.2.10")), nil
				}
			})
			parker := &DNSParker{Hostname: "mc.example.com", ZoneID: "0123456789abcdef0123456789abcdef", TokenParameter: "/minecraft/launcher/cloudflare-token", Parameters: secret, Client: client}
			if err := parker.Park(context.Background()); err == nil || strings.Contains(err.Error(), "private") {
				t.Fatalf("error must be safe: %v", err)
			}
		})
	}
}

func TestAlreadyParkedDNSDoesNotWrite(t *testing.T) {
	client := cloudflareClient()
	client.Transport = roundTripper(func(req *http.Request) (*http.Response, error) {
		if req.Method != "GET" {
			t.Fatal("already parked DNS must not be rewritten")
		}
		return dnsReply([]dnsRecord{record(parkedAddress)}), nil
	})
	parker := &DNSParker{Hostname: "mc.example.com", ZoneID: "0123456789abcdef0123456789abcdef", TokenParameter: "/minecraft/launcher/cloudflare-token", Parameters: &fakeTokenParameter{value: "fake-token"}, Client: client}
	if err := parker.Park(context.Background()); err != nil {
		t.Fatal(err)
	}
}
