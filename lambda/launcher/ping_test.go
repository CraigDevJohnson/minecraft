package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"io"
	"net"
	"strconv"
	"testing"
)

// serveStatus runs a one-shot fake Java server that checks the handshake and answers with status.
func serveStatus(t *testing.T, status string) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { listener.Close() })
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		reader := bufio.NewReader(conn)
		handshake := readTestPacket(t, reader)
		if handshake == nil || handshake[0] != 0x00 || handshake[len(handshake)-1] != 0x01 {
			return
		}
		port := binary.BigEndian.Uint16(handshake[len(handshake)-3 : len(handshake)-1])
		if strconv.Itoa(int(port)) != portOf(listener.Addr().String()) {
			return
		}
		if request := readTestPacket(t, reader); !bytes.Equal(request, []byte{0x00}) {
			return
		}
		var body bytes.Buffer
		writeVarInt(&body, 0x00)
		writeString(&body, status)
		var response bytes.Buffer
		writePacket(&response, body.Bytes())
		_, _ = conn.Write(response.Bytes())
	}()
	return listener.Addr().String()
}

func readTestPacket(t *testing.T, reader *bufio.Reader) []byte {
	length, err := readVarInt(reader)
	if err != nil {
		return nil
	}
	packet := make([]byte, length)
	if _, err := io.ReadFull(reader, packet); err != nil {
		return nil
	}
	return packet
}

func portOf(address string) string {
	_, port, _ := net.SplitHostPort(address)
	return port
}

func TestPingPlayersReadsOnlineCount(t *testing.T) {
	address := serveStatus(t, `{"version":{"name":"Paper 26.2","protocol":776},"players":{"max":6,"online":3},"description":"family"}`)
	players, err := PingPlayers(context.Background(), address)
	if err != nil || players != 3 {
		t.Fatalf("players=%d err=%v", players, err)
	}
}

func TestPingPlayersZeroOnline(t *testing.T) {
	address := serveStatus(t, `{"players":{"max":6,"online":0}}`)
	players, err := PingPlayers(context.Background(), address)
	if err != nil || players != 0 {
		t.Fatalf("players=%d err=%v", players, err)
	}
}

func TestPingPlayersRejectsStatusWithoutCount(t *testing.T) {
	for _, status := range []string{`{"players":{"max":6}}`, `{}`, `not json`, `{"players":{"online":-1}}`} {
		address := serveStatus(t, status)
		if _, err := PingPlayers(context.Background(), address); err == nil {
			t.Fatalf("status %q must be rejected", status)
		}
	}
}

func TestPingPlayersFailsWhenNothingListens(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	listener.Close()
	if _, err := PingPlayers(context.Background(), address); err == nil {
		t.Fatal("a closed port must fail the ping")
	}
}

func TestVarIntRoundTrip(t *testing.T) {
	for _, value := range []int32{0, 1, 127, 128, 255, 25565, 2097151, 2147483647, -1} {
		var buffer bytes.Buffer
		writeVarInt(&buffer, value)
		got, err := readVarInt(bufio.NewReader(&buffer))
		if err != nil || got != value {
			t.Fatalf("value %d round-tripped to %d (%v)", value, got, err)
		}
	}
}
