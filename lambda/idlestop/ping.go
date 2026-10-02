package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"time"
)

const (
	pingTimeout     = 5 * time.Second
	maxStatusLength = 1 << 20
)

// PingPlayers asks a Java edition server for its online player count with the Server List Ping protocol.
// Bedrock players join through Geyser and Floodgate as Java players, so this one count covers both editions.
func PingPlayers(ctx context.Context, address string) (int, error) {
	host, portText, err := net.SplitHostPort(address)
	if err != nil {
		return 0, fmt.Errorf("ping: invalid address")
	}
	port, err := strconv.ParseUint(portText, 10, 16)
	if err != nil {
		return 0, fmt.Errorf("ping: invalid port")
	}
	ctx, cancel := context.WithTimeout(ctx, pingTimeout)
	defer cancel()
	var dialer net.Dialer
	conn, err := dialer.DialContext(ctx, "tcp", address)
	if err != nil {
		return 0, fmt.Errorf("ping: connect: %w", err)
	}
	defer conn.Close()
	if deadline, ok := ctx.Deadline(); ok {
		_ = conn.SetDeadline(deadline)
	}

	var handshake bytes.Buffer
	writeVarInt(&handshake, 0x00) // handshake packet
	writeVarInt(&handshake, -1)   // protocol version: any, for a status request
	writeString(&handshake, host)
	_ = binary.Write(&handshake, binary.BigEndian, uint16(port))
	writeVarInt(&handshake, 1) // next state: status
	var request bytes.Buffer
	writePacket(&request, handshake.Bytes())
	writePacket(&request, []byte{0x00}) // status request
	if _, err := conn.Write(request.Bytes()); err != nil {
		return 0, fmt.Errorf("ping: send: %w", err)
	}
	return readStatus(bufio.NewReader(conn))
}

func readStatus(reader *bufio.Reader) (int, error) {
	length, err := readVarInt(reader)
	if err != nil || length <= 0 || length > maxStatusLength {
		return 0, errors.New("ping: invalid response length")
	}
	packet := io.LimitReader(reader, int64(length))
	body := bufio.NewReader(packet)
	if id, err := readVarInt(body); err != nil || id != 0x00 {
		return 0, errors.New("ping: unexpected response packet")
	}
	size, err := readVarInt(body)
	if err != nil || size <= 0 || size > maxStatusLength {
		return 0, errors.New("ping: invalid status length")
	}
	payload := make([]byte, size)
	if _, err := io.ReadFull(body, payload); err != nil {
		return 0, errors.New("ping: truncated status")
	}
	var status struct {
		Players *struct {
			Online *int `json:"online"`
		} `json:"players"`
	}
	if err := json.Unmarshal(payload, &status); err != nil || status.Players == nil || status.Players.Online == nil || *status.Players.Online < 0 {
		return 0, errors.New("ping: status has no player count")
	}
	return *status.Players.Online, nil
}

func writePacket(buffer *bytes.Buffer, payload []byte) {
	writeVarInt(buffer, int32(len(payload)))
	buffer.Write(payload)
}

func writeString(buffer *bytes.Buffer, value string) {
	writeVarInt(buffer, int32(len(value)))
	buffer.WriteString(value)
}

func writeVarInt(buffer *bytes.Buffer, value int32) {
	unsigned := uint32(value)
	for {
		if unsigned&^0x7f == 0 {
			buffer.WriteByte(byte(unsigned))
			return
		}
		buffer.WriteByte(byte(unsigned&0x7f) | 0x80)
		unsigned >>= 7
	}
}

func readVarInt(reader io.ByteReader) (int32, error) {
	var result uint32
	for shift := 0; shift < 35; shift += 7 {
		next, err := reader.ReadByte()
		if err != nil {
			return 0, err
		}
		result |= uint32(next&0x7f) << shift
		if next&0x80 == 0 {
			return int32(result), nil
		}
	}
	return 0, errors.New("varint too long")
}
