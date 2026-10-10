#!/usr/bin/env bash
# Install Crafty Controller and the lifecycle adapter on the prepared retained host.
# Run as root from a reviewed checkout after the host bootstrap has mounted /srv/minecraft.
# Private files (/etc/minecraft/operator.json and the Crafty API token) are supplied separately.
set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
install_dir=/opt/minecraft-crafty
data_root=/srv/minecraft
crafty_root="$data_root/crafty"

[[ "$(id -u)" == 0 ]] || {
	printf 'Run the Crafty installer as root.\n' >&2
	exit 1
}
command -v docker >/dev/null || {
	printf 'Docker is required; run the host bootstrap first.\n' >&2
	exit 1
}
command -v aws >/dev/null || {
	printf 'AWS CLI v2 is required for S3 archives; run scripts/install_aws_cli.sh first.\n' >&2
	exit 1
}
findmnt --mountpoint "$data_root" >/dev/null || {
	printf 'The retained data disk is not mounted at %s.\n' "$data_root" >&2
	exit 1
}

image="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["crafty"]["image"])' "$source_dir/runtime/crafty/server.json")"
[[ "$image" =~ ^registry\.gitlab\.com/crafty-controller/crafty-4@sha256:[0-9a-f]{64}$ ]] || {
	printf 'The Crafty image must be pinned by digest.\n' >&2
	exit 1
}

install -d -m 0755 "$install_dir" "$install_dir/scripts" "$install_dir/runtime"
install -m 0644 "$source_dir/scripts/minecraft_crafty.py" "$source_dir/scripts/minecraft_content.py" "$install_dir/scripts/"
install -m 0644 "$source_dir/runtime/crafty/server.json" "$install_dir/runtime/"
for directory in backups logs servers config import state staging; do
	install -d -m 0770 "$crafty_root/$directory"
done
chmod 0700 "$crafty_root/state" "$crafty_root/staging"

# Game ports are public; the Crafty web UI listens on loopback only and is reached through SSM.
cat >"$install_dir/compose.yaml" <<COMPOSE
services:
  crafty:
    image: ${image}
    platform: linux/arm64
    restart: unless-stopped
    stop_grace_period: 180s
    environment:
      TZ: Etc/UTC
    ports:
      - "25565:25565/tcp"
      - "19132:19132/udp"
      - "127.0.0.1:8443:8443/tcp"
    volumes:
      - ${crafty_root}/backups:/crafty/backups
      - ${crafty_root}/logs:/crafty/logs
      - ${crafty_root}/servers:/crafty/servers
      - ${crafty_root}/config:/crafty/app/config
      - ${crafty_root}/import:/crafty/import
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"
COMPOSE
chmod 0644 "$install_dir/compose.yaml"

cat >/usr/local/sbin/minecraft-instance <<'LAUNCHER'
#!/bin/sh
exec /usr/bin/python3 -B /opt/minecraft-crafty/scripts/minecraft_crafty.py "$@"
LAUNCHER
chmod 0755 /usr/local/sbin/minecraft-instance

# Stopping the unit (including an EC2 stop without the operator's drain) archives first.
cat >/etc/systemd/system/minecraft-crafty.service <<'UNIT'
[Unit]
Description=Crafty Controller for the family Minecraft server
Requires=docker.service minecraft-storage.service
After=docker.service minecraft-storage.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/docker compose -f /opt/minecraft-crafty/compose.yaml up --detach --wait
ExecStop=-/usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json host-drain
ExecStop=/usr/bin/docker compose -f /opt/minecraft-crafty/compose.yaml stop
TimeoutStartSec=600
TimeoutStopSec=900

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable minecraft-crafty.service
systemctl start minecraft-crafty.service
printf 'Crafty is running. Reach it with the workstation admin tunnel, then follow docs/operations/crafty.md.\n'
