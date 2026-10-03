# Family device validation

Use this runbook for [issue #9](https://github.com/CraigDevJohnson/minecraft/issues/9). Craig operates the real clients and approves console network changes. An agent can help collect server observations and investigate failures. Acceptance requires Java, tablet/iPad, PlayStation and Nintendo Switch to play together, preserve their identities and inventories after reconnecting, enforce the family allowlist, and complete a representative session approaching six players. [Issue #8](https://github.com/CraigDevJohnson/minecraft/issues/8) separately proves live recovery.

## Prepare the session

1. Record the installed content revision, actual client versions, device models, and each intended network in a private copy of the [evidence form](family-device-evidence.md.example). Keep account names, UUIDs, the endpoint, screenshots, original DNS settings and raw diagnostics in ignored `operator-config/family/` or other private storage. Share only aliases and numeric observations in the issue.
2. Check [current compatibility and release gates](client-compatibility-readiness.md), then [allowlist](allowlist.md) the verified family identities. If Floodgate cannot resolve a gamertag, use a real join attempt or verified Floodgate UUID; do not guess the identity. Sign in to the console and Microsoft accounts and confirm the household's multiplayer permissions and applicable online-service access before changing DNS.
3. Start `survival` through the [host commands](host-commands.md), including the private Cloudflare token reference when required. Wait for `application: running` and the DNS update before giving devices the private hostname. Use Java TCP port `25565` and Bedrock UDP port `19132` for the current release.
4. Begin with Java and a direct tablet connection. If both fail, investigate server readiness, hostname resolution and protocol compatibility before changing console settings. A hostname can take about a minute to follow a host start; restart a client that retains the previous address.

The current server is Paper 26.2 with ViaVersion for Java 26.3 and Geyser 2.11.3 for Bedrock 26.30–26.52. Recheck [runtime content](runtime-content.md) after any client or content update. The future NetherNet rollout changes Bedrock transport and needs its own real-device checks; this runbook does not certify Bedrock 26.60.

## Join on Java and tablet/iPad

On Java Edition, open Multiplayer, add the family hostname as the server address, and join. Port `25565` is the default. Use a supported Java version and the verified allowlisted account.

On the tablet/iPad's Bedrock Edition, sign in, open Play, then Servers and Add Server. Enter a local display name, the family hostname and port `19132`, save and join. Use the same verified Bedrock account throughout the reconnect test. Do not substitute a local world or a Realm for the family server.

If the Bedrock Servers tab or multiplayer is unavailable, resolve account/family permissions first. If an approved account reaches the server but is refused, inspect its verified allowlist identity. Keep the allowlist and online authentication enabled.

## Join on Nintendo Switch and PlayStation

The initial route is BedrockConnect DNS redirection, described by [Geyser](https://geysermc.org/wiki/geyser/using-geyser-with-consoles/) and the [BedrockConnect maintainer](https://github.com/Pugmatt/BedrockConnect#quick-setup). Look up the maintainer's current DNS choices, redirect-compatible featured servers and troubleshooting immediately before the attempt. Record the chosen service privately; addresses and availability can change.

Before editing a console, save its original DNS mode and both DNS addresses. Change only that console's selected connection; leave router settings alone.

| Device | Console path to DNS settings |
| --- | --- |
| Nintendo Switch | System Settings → Internet → Internet Settings → registered network → Change Settings → DNS Settings → Manual |
| PlayStation 5 | Settings → Network → Settings → Set Up Internet Connection → selected connection → Advanced Settings → DNS Settings → Manual |
| PlayStation 4 | Settings → Network → Set Up Internet Connection → Wi-Fi or LAN → Custom → DNS Settings → Manual |

Enter the current BedrockConnect primary DNS and its documented secondary DNS. Test the connection, restart Minecraft, then join a currently redirect-compatible featured server. In the BedrockConnect list, add the family hostname and port `19132`, then connect. Record failures separately at DNS lookup, list entry, transfer and family-server join. Successful list entry alone is not a successful join.

BedrockConnect's DNS method routes console DNS through another operator, and its server list receives the target address. Use it only for the chosen console connection and remove the saved target if retiring the route. This route remains unverified for the household until each real console passes.

### Restore the console connection

Exit Minecraft. Return to the selected connection's DNS settings. If the original mode was automatic, select automatic; if it was manual, restore both original addresses exactly. Save and test internet connectivity, then restart Minecraft. Remove the saved family entry if abandoning the route. Record whether normal featured-server access works again. If a connection fails, compare with the private original settings before making another change.

If the public service or household network prevents the route, restore DNS and use the maintainer's [troubleshooting](https://github.com/Pugmatt/BedrockConnect/wiki/Troubleshooting). Geyser also documents LAN-based alternatives. A different helper or self-hosted service needs a concrete selection and real console validation; keep #9 open while the route is unresolved.

## Prove access, interaction and reconnect behavior

For each required device, record the world/spawn landmark, send and receive chat, place and break a test block, interact with a shared chest, and confirm another player sees the interaction. Put a distinctive ordinary item in the inventory and note its slot, quantity and location privately. Disconnect, fully restart the client, and rejoin using the same account and route. Confirm the same player identity, inventory, location and shared-world changes.

Use a separate known unapproved account for a denial attempt. Verify it cannot enter the world, then immediately recheck an approved account. Record the time and sanitized outcome. A discovery response or configured whitelist setting does not prove denial.

Combine player checkpoint confirmation with #8 when its approved persistence/recovery test runs. Keep the original world, accounts and recovery points intact.

## Measure representative play

Gather the intended players, approaching the six-player limit. Record the actual peak count and duration. Include shared building, simultaneous exploration into new chunks, inventory/chest interaction and a reconnect on every required device. Keep existing view/simulation distances, plugins, JVM settings and instance size fixed during the baseline so observations are comparable.

Collect samples before play, during exploration and after shared play. On the already-running host in the existing private operator session, first confirm readiness:

```sh
sudo /usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json status survival
```

Then collect the server's tick metrics without changing the session lifecycle:

```sh
date -u +%FT%TZ
sudo docker compose --project-name minecraft-survival \
  --file /srv/minecraft/instances/survival/runtime/compose.json \
  exec -T minecraft rcon-cli tps
sudo docker compose --project-name minecraft-survival \
  --file /srv/minecraft/instances/survival/runtime/compose.json \
  exec -T minecraft rcon-cli mspt
```

Read container memory separately. This selects the same Compose project/service and refuses an ambiguous result:

```sh
family_container=$(sudo docker ps -q \
  --filter label=com.docker.compose.project=minecraft-survival \
  --filter label=com.docker.compose.service=minecraft)
if [ -n "$family_container" ] && [ "$(printf '%s\n' "$family_container" | wc -l)" -eq 1 ]; then
  sudo docker stats --no-stream --format '{{.MemUsage}}' "$family_container"
else
  printf 'Expected one running survival container; check instance status.\n' >&2
fi
```

Transcribe only the timestamp, actual player count, TPS intervals, MSPT values and container memory usage/limit into the evidence form. Record lag, rubber-banding, crashes and user experience beside the same timestamps. Container memory includes more than the Java heap. About 20 TPS and tick durations below the 50 ms tick budget are useful observations, but the family experience and peak-session behavior determine whether the hardware is acceptable.

These commands use the private generated Compose file without displaying it or putting the RCON secret in arguments. Never use Compose start/stop, edit operation records, or stop EC2 to collect metrics. Current main has no `minecraft_host.py diagnostics` command; the manual samples above are the supported preparation for this runbook.

If play still stutters, use [Paper's profiling guidance](https://docs.papermc.io/paper/profiling/) to agree on a bounded spark capture during the failure. Paper bundles spark. A profiler report can be uploaded to a third-party viewer, so confirm the capture/report destination before starting it. Collect evidence before deciding on JVM flags, pre-generation, distance changes or a larger paid instance. The October 3 proof of concept already reported stutter during chunk generation; idle TPS does not resolve that observation.

## Finish the acceptance record

Have Craig follow the documented start, status and clean host-stop commands, then confirm backup completion in the returned evidence. Recovery acceptance requires the isolated procedure in #8; practicing a stop is not a restore test. If a command needs undocumented setup, record and repair it before accepting the operator workflow.

Post a sanitized device matrix, denial/reconnect results, peak-player count, numeric samples, user experience, console rollback result and unresolved failures to #9. Close it only after every criterion passes. Keep future release support in #26/#27 separate from evidence for the currently installed release.
