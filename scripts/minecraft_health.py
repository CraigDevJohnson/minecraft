#!/usr/bin/env python3
"""Fixed-target, read-only observations; stdout contains only sanitized health JSON."""
from __future__ import annotations

import io
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time

ACCOUNT = "793680745829"
REGION = "us-west-2"
HOSTNAME = "mc.craigdevjohnson.com"
AWS_TIMEOUT = 30
NETWORK_TIMEOUT = 5
MAX_STATUS_BYTES = 16 * 1024
MAX_DISCOVERY_BYTES = 4096
MAX_SIGNALING_BYTES = 16 * 1024
# The pinned server content of the checked-out main, which says whether the server also offers NetherNet.
MANIFEST = Path(__file__).resolve().parents[1] / "runtime" / "crafty" / "server.json"
RAKNET_MAGIC = bytes.fromhex("00ffff00fefefefefdfdfdfd12345678")
HOST_STATES = {"pending", "running", "stopping", "stopped", "shutting-down"}
AWS_READS = {
    "identity": ["sts", "get-caller-identity", "--query", "Account"],
    "hosts": ["ec2", "describe-instances", "--filters",
              "Name=tag:Project,Values=minecraft", "Name=tag:Environment,Values=family",
              "Name=tag:Name,Values=minecraft-family-host",
              "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down",
              "--max-items", "2", "--query",
              "{hosts:Reservations[].Instances[].{instance_id:InstanceId,state:State.Name,public_ipv4:PublicIpAddress},next_token:NextToken}"],
    "checks": ["ec2", "describe-instance-status", "--include-all-instances", "--query",
               "InstanceStatuses[].{instance_id:InstanceId,state:InstanceState.Name,instance_status:InstanceStatus.Status,system_status:SystemStatus.Status}"],
}


class HealthError(Exception):
    """Only fixed, authored error codes may be published."""


def aws_read(kind: str, instance_id: str | None = None):
    if kind not in AWS_READS or (kind == "checks") != (instance_id is not None):
        raise HealthError("invalid_read_request")
    command = ["aws", *AWS_READS[kind]]
    if instance_id is not None:
        if not re.fullmatch(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", instance_id):
            raise HealthError("invalid_host_metadata")
        command.extend(["--instance-ids", instance_id])
    command.extend(["--region", REGION, "--output", "json", "--no-cli-pager",
                    "--cli-connect-timeout", "5", "--cli-read-timeout", "10"])
    environment = dict(os.environ, AWS_MAX_ATTEMPTS="1", AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true")
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True,
                                timeout=AWS_TIMEOUT, env=environment)
        if len(result.stdout) > 16384:
            raise HealthError("invalid_aws_response")
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError):
        raise HealthError("aws_read_failed") from None
    except (ValueError, RecursionError):
        raise HealthError("invalid_aws_response") from None


def public_address(value) -> str:
    try:
        address = ipaddress.IPv4Address(value) if isinstance(value, str) else None
        if address is None or not address.is_global or address.is_multicast:
            raise ValueError
        return str(address)
    except ValueError:
        raise HealthError("invalid_public_address") from None


def require_dns_match(address: str) -> None:
    # getaddrinfo has no portable timeout. A daemon resolver cannot keep this process alive.
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(HOSTNAME, 25565, socket.AF_INET, socket.SOCK_STREAM))
        except Exception:
            result.put(None)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        answers = result.get(timeout=NETWORK_TIMEOUT)
    except queue.Empty:
        raise HealthError("dns_lookup_failed") from None
    if answers is None:
        raise HealthError("dns_lookup_failed")
    if {answer[4][0] for answer in answers} != {address}:
        raise HealthError("dns_mismatch")


def varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    encoded = bytearray()
    while True:
        byte, value = value & 127, value >> 7
        encoded.append(byte | (128 if value else 0))
        if not value:
            return bytes(encoded)


def read_varint(read) -> int:
    value = 0
    for shift in range(0, 35, 7):
        byte = read(1)
        if len(byte) != 1 or (shift == 28 and byte[0] > 15):
            raise ValueError
        value |= (byte[0] & 127) << shift
        if not byte[0] & 128:
            return value
    raise ValueError


def remaining_timeout(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def safe_status(version, protocol, online, limit) -> dict:
    if (type(protocol) is not int or not 0 <= protocol <= 0x7FFFFFFF
            or type(online) is not int or type(limit) is not int or not 0 <= online <= limit <= 100000):
        raise ValueError
    status = {"protocol": protocol, "online": online, "limit": limit}
    if isinstance(version, str) and len(version) <= 64:
        release = re.fullmatch(r"(?:Paper |Spigot |Purpur |Minecraft )?([0-9]{1,3}(?:\.[0-9]{1,3}){1,3})", version)
        if release:
            status["version"] = release[1]
    return status


def java_status(address: str) -> dict:
    deadline = time.monotonic() + NETWORK_TIMEOUT
    try:
        with socket.create_connection((address, 25565), timeout=NETWORK_TIMEOUT) as client:
            def read(size):
                received = bytearray()
                while len(received) < size:
                    client.settimeout(remaining_timeout(deadline))
                    part = client.recv(size - len(received))
                    if not part:
                        raise ValueError
                    received.extend(part)
                return bytes(received)

            hostname = HOSTNAME.encode("ascii")
            # Protocol -1 requests generic status; next state 1 never attempts login or play.
            handshake = varint(0) + varint(-1) + varint(len(hostname)) + hostname + struct.pack(">H", 25565) + varint(1)
            client.settimeout(remaining_timeout(deadline))
            client.sendall(varint(len(handshake)) + handshake + b"\x01\x00")
            length = read_varint(read)
            if not 0 < length <= MAX_STATUS_BYTES:
                raise ValueError
            packet = io.BytesIO(read(length))
            if read_varint(packet.read) != 0:
                raise ValueError
            text_length = read_varint(packet.read)
            text = packet.read(text_length)
            if len(text) != text_length or packet.read(1):
                raise ValueError
            status = json.loads(text.decode("utf-8"))
            return safe_status(status["version"].get("name"), status["version"]["protocol"],
                               status["players"]["online"], status["players"]["max"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise HealthError("java_status_failed") from None


def bedrock_discovery(address: str) -> dict:
    deadline = time.monotonic() + NETWORK_TIMEOUT
    ping = b"\x01" + bytes(8) + RAKNET_MAGIC + struct.pack(">q", 5)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(remaining_timeout(deadline))
            client.connect((address, 19132))
            client.send(ping)
            client.settimeout(remaining_timeout(deadline))
            response = client.recv(MAX_DISCOVERY_BYTES + 1)
        if (not 35 <= len(response) <= MAX_DISCOVERY_BYTES or response[:1] != b"\x1c"
                or response[1:9] != ping[1:9] or response[17:33] != RAKNET_MAGIC
                or struct.unpack(">H", response[33:35])[0] != len(response) - 35):
            raise ValueError
        fields = response[35:].decode("utf-8").split(";")
        if len(fields) < 6 or fields[0] != "MCPE" or any(
                not re.fullmatch(r"[0-9]{1,10}", fields[index]) for index in (2, 4, 5)):
            raise ValueError
        return safe_status(fields[3], int(fields[2]), int(fields[4]), int(fields[5]))
    except (OSError, ValueError, struct.error):
        raise HealthError("bedrock_discovery_failed") from None


def nethernet_selected() -> bool:
    """Whether the pinned Geyser configuration adds NetherNet's built-in signaling and WebRTC to RakNet."""
    try:
        manifest = json.loads(MANIFEST.read_text())
        [bedrock] = [item["content"]["bedrock"] for item in manifest["managed_files"]
                     if item["path"] == "plugins/Geyser-Spigot/config.yml"]
        return bedrock.get("transport") == "both"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise HealthError("manifest_unreadable") from None


def http_response(data: bytes, closed: bool) -> tuple[str, dict, bytes] | None:
    """The status line, headers and body of one HTTP response, or None while more of it is due."""
    head, separator, rest = data.partition(b"\r\n\r\n")
    if not separator:
        if closed:
            raise ValueError
        return None
    status, *lines = head.decode("ascii").split("\r\n")
    headers = {}
    for line in lines:
        name, colon, value = line.partition(":")
        if not colon:
            raise ValueError
        headers[name.strip().lower()] = value.strip()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = b""
        while True:
            size, separator, rest = rest.partition(b"\r\n")
            length = int(size.split(b";")[0], 16) if separator else None
            if length is not None and length < 0:
                raise ValueError
            if length is None or len(rest) < length + 2:
                if closed:
                    raise ValueError
                return None
            if rest[length:length + 2] != b"\r\n":
                raise ValueError
            body, rest = body + rest[:length], rest[length + 2:]
            if length == 0:
                return status, headers, body
    if "content-length" in headers:
        length = int(headers["content-length"])
        if length < 0:
            raise ValueError
        if len(rest) < length:
            if closed:
                raise ValueError
            return None
        return status, headers, rest[:length]
    return (status, headers, rest) if closed else None


def nethernet_signaling(address: str) -> dict:
    """Ask NetherNet's built-in signaling on TCP 19132 for its discovery answer. The GET admits no one."""
    deadline = time.monotonic() + NETWORK_TIMEOUT
    request = f"GET /v1/join HTTP/1.1\r\nHost: {HOSTNAME}:19132\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
    try:
        with socket.create_connection((address, 19132), timeout=NETWORK_TIMEOUT) as client:
            client.settimeout(remaining_timeout(deadline))
            client.sendall(request.encode("ascii"))
            received, response = bytearray(), None
            while response is None:
                client.settimeout(remaining_timeout(deadline))
                part = client.recv(MAX_SIGNALING_BYTES + 1 - len(received))
                received.extend(part)
                if len(received) > MAX_SIGNALING_BYTES:
                    raise ValueError
                response = http_response(bytes(received), closed=not part)
        status, headers, body = response
        if (not re.fullmatch(r"HTTP/1\.[01] 200(?: .*)?", status)
                or headers.get("content-type", "").split(";")[0].strip().lower() != "application/json"):
            raise ValueError
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict) or value.get("onlineAuth") is not True:
            raise ValueError
        return safe_status(value.get("version"), value.get("protocol"), value.get("players"), value.get("maxPlayers"))
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise HealthError("nethernet_signaling_failed") from None


def observe(report: dict) -> None:
    if aws_read("identity") != ACCOUNT:
        raise HealthError("account_mismatch")
    report["identity_verified"] = True
    inventory = aws_read("hosts")
    if not isinstance(inventory, dict) or not isinstance(inventory.get("hosts"), list):
        raise HealthError("invalid_host_metadata")
    hosts = inventory["hosts"]
    if inventory.get("next_token") or len(hosts) > 1:
        raise HealthError("ambiguous_hosts")
    if not hosts:
        report.update(state="absent", status="idle")
        return
    host = hosts[0]
    if (not isinstance(host, dict) or not isinstance(host.get("state"), str)
            or host["state"] not in HOST_STATES
            or not isinstance(host.get("instance_id"), str)
            or not re.fullmatch(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", host["instance_id"])):
        raise HealthError("invalid_host_metadata")
    report["state"] = host["state"]
    if host["state"] == "stopped":
        report["status"] = "idle"
        return
    if host["state"] != "running":
        raise HealthError("host_transitioning")
    address = public_address(host.get("public_ipv4"))
    report["ec2_checks_passed"] = False
    checks = aws_read("checks", host["instance_id"])
    expected = {"instance_id": host["instance_id"], "state": "running", "instance_status": "ok", "system_status": "ok"}
    if checks != [expected]:
        raise HealthError("ec2_checks_failed")
    report["ec2_checks_passed"] = True
    report["dns_matches_host"] = False
    require_dns_match(address)
    report["dns_matches_host"] = True
    report["java_status_ok"] = False
    report["java"] = java_status(address)
    report["java_status_ok"] = True
    report["bedrock_discovery_ok"] = False
    report["bedrock"] = bedrock_discovery(address)
    report["bedrock_discovery_ok"] = True
    if nethernet_selected():
        report["nethernet_signaling_ok"] = False
        report["nethernet"] = nethernet_signaling(address)
        report["nethernet_signaling_ok"] = True
    report.update(game_ready=True, status="healthy")


def main(arguments: list[str] | None = None) -> int:
    report = {"state": "unknown", "status": "failed", "identity_verified": False,
              "ec2_checks_passed": None, "dns_matches_host": None, "java_status_ok": None,
              "bedrock_discovery_ok": None, "nethernet_signaling_ok": None, "game_ready": False}
    try:
        supplied_arguments = arguments if arguments is not None else sys.argv[1:]
        if supplied_arguments:
            raise HealthError("unsupported_arguments")
        observe(report)
    except HealthError as error:
        report["failure"] = str(error)
    except Exception:
        report["failure"] = "observation_failed"
    print(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 1 if "failure" in report else 0


if __name__ == "__main__":
    raise SystemExit(main())
