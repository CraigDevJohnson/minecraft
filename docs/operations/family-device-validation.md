# Family device validation

Family-device requirements for [issue #9](https://github.com/CraigDevJohnson/minecraft/issues/9) are resolved by Craig's [user acceptance/waiver](https://github.com/CraigDevJohnson/minecraft/issues/9#issuecomment-5972110637) on October 3, 2026: the connected iPads were good, and he directed that requirements or validations mentioning consoles or family-member testing be marked resolved. Reported joins were Craig's Java client on Mac, Bedrock on iPhone and two iPads, with four simultaneous players. PS5/Switch were untested and BedrockConnect was never configured. Chat, reconnect, inventory, denial, six-player and console checks are not recorded as passed. Issue #9 is closed on this acceptance basis; these unperformed family checks no longer block the current milestone.

The remaining instructions are optional future help and observations, not replacement family acceptance gates. Craig operates real clients and approves console network changes; an agent can help investigate a requested check. [Issue #8](https://github.com/CraigDevJohnson/minecraft/issues/8) separately proves live recovery. [#26](https://github.com/CraigDevJohnson/minecraft/issues/26)/[#27](https://github.com/CraigDevJohnson/minecraft/issues/27) retain their upstream, runtime, infrastructure and technical protocol safety gates.

## Prepare an optional session

1. Record the installed content revision, actual client versions, device models, and each intended network in a private copy of the [evidence form](family-device-evidence.md.example). Keep account names, UUIDs, the endpoint, screenshots, original DNS settings and raw diagnostics in ignored `operator-config/family/` or other private storage. Share only aliases and numeric observations in the issue.
2. Check [current compatibility and release gates](client-compatibility-readiness.md), then [allowlist](allowlist.md) the verified family identities. If Floodgate cannot resolve a gamertag, use a real join attempt or verified Floodgate UUID; do not guess the identity. Sign in to the console and Microsoft accounts and confirm the household's multiplayer permissions and applicable online-service access before changing DNS.
3. Start the server with the [Server control workflow](cloud-controls.md#the-server-control-workflow) or the `/minecraft` page. Wait for the start to report the server ready and the DNS update before giving devices the hostname. Use Java TCP port `25565` and Bedrock UDP port `19132` for the current release.
4. Begin with Java and a direct tablet connection. If both fail, investigate server readiness, hostname resolution and protocol compatibility before changing console settings. A hostname can take about a minute to follow a host start; restart a client that retains the previous address.

The current server is Paper 26.3 (native Java 26.3, no ViaVersion) with Geyser 2.12.1 for Bedrock 26.40–26.60. Recheck [what is pinned](crafty.md#what-is-pinned) after any client or content update. The approved dual-transport revision keeps this hostname/port while adding built-in NetherNet signaling on TCP `19132` and WebRTC UDP `19133`. After an approved rollout, confirm the hostname before accepting its first-use trust prompt; WebRTC UDP `19133` is negotiated, not a separate Add Server port. Preserve the Geyser identity through backup/restore and investigate unexpected trust changes. Console routes remain unverified optional guidance. This acceptance does not certify Bedrock 26.60 or resolve the separate technical release gates; the 26.60 join itself was recorded on [#26](https://github.com/CraigDevJohnson/minecraft/issues/26) on 2026-10-29.

## Join on Java and tablet/iPad

On Java Edition, open Multiplayer, add the family hostname as the server address, and join. Port `25565` is the default. Use a supported Java version and the verified allowlisted account.

On the tablet/iPad's Bedrock Edition, sign in, open Play, then Servers and Add Server. Enter a local display name, the family hostname and port `19132`, save and join. Use the same verified Bedrock account throughout the reconnect test. Do not substitute a local world or a Realm for the family server.

If the Bedrock Servers tab or multiplayer is unavailable, resolve account/family permissions first. If an approved account reaches the server but is refused, inspect its verified allowlist identity. Keep the allowlist and online authentication enabled.

## Optional Nintendo Switch and PlayStation help

If console access is requested later, one documented route is BedrockConnect DNS redirection, described by [Geyser](https://geysermc.org/wiki/geyser/using-geyser-with-consoles/) and the [BedrockConnect maintainer](https://github.com/Pugmatt/BedrockConnect#quick-setup). Look up the maintainer's current DNS choices, redirect-compatible featured servers and troubleshooting immediately before the attempt. Record the chosen service privately; addresses and availability can change.

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

If the public service or household network prevents the optional route, restore DNS and use the maintainer's [troubleshooting](https://github.com/Pugmatt/BedrockConnect/wiki/Troubleshooting). Geyser also documents LAN-based alternatives. Document any different helper or self-hosted route before changing settings; an unresolved optional console route does not reopen the waived family requirements.

## Optional access, interaction and reconnect observations

For each device selected for an optional check, record the world/spawn landmark, send and receive chat, place and break a test block, interact with a shared chest, and confirm another player sees the interaction. Put a distinctive ordinary item in the inventory and note its slot, quantity and location privately. Disconnect, fully restart the client, and rejoin using the same account and route. Confirm the same player identity, inventory, location and shared-world changes.

Use a separate known unapproved account for a denial attempt. Verify it cannot enter the world, then immediately recheck an approved account. Record the time and sanitized outcome. A discovery response or configured whitelist setting does not prove denial.

Combine player checkpoint confirmation with #8 when its approved persistence/recovery test runs. Keep the original world, accounts and recovery points intact.

## Optional representative-play observations

For a requested play observation, record the actual peak count and duration; four simultaneous joins were reported at acceptance, not a six-player measurement. Shared building, exploration into new chunks, inventory/chest interaction and reconnects can help investigate a reported problem. Keep existing view/simulation distances, plugins, JVM settings and instance size fixed during the baseline so observations are comparable.

Collect samples before play, during exploration and after shared play. On the already-running host in the existing private operator session, first confirm readiness:

```sh
sudo /usr/local/sbin/minecraft-instance status family
```

Then collect the server's tick metrics through the adapter's `console` action ([Crafty host](crafty.md#send-a-console-command)), which changes nothing about the session:

```sh
date -u +%FT%TZ
sudo /usr/local/sbin/minecraft-instance console family 'tps'
sudo /usr/local/sbin/minecraft-instance console family 'mspt'
```

Read container memory separately. This selects Crafty's container, which runs the server, and refuses an ambiguous result:

```sh
crafty_container=$(sudo docker ps -q \
  --filter label=com.docker.compose.project=minecraft-crafty \
  --filter label=com.docker.compose.service=crafty)
if [ -n "$crafty_container" ] && [ "$(printf '%s\n' "$crafty_container" | wc -l)" -eq 1 ]; then
  sudo docker stats --no-stream --format '{{.MemUsage}}' "$crafty_container"
else
  printf 'Expected one running Crafty container; check the host.\n' >&2
fi
```

Transcribe only the timestamp, actual player count, TPS intervals, MSPT values and container memory usage/limit into the evidence form. Record lag, rubber-banding, crashes and user experience beside the same timestamps. Container memory includes Crafty itself as well as the Java heap. About 20 TPS and tick durations below the 50 ms tick budget are useful observations, but the family experience and peak-session behavior determine whether the hardware is acceptable.

The `console` action sends each command through Crafty's loopback API; the server has RCON off. Never start or stop the server by hand, edit the adapter's state records, or stop EC2 to collect metrics. The manual samples above are the supported preparation for this runbook.

If play still stutters, use [Paper's profiling guidance](https://docs.papermc.io/paper/profiling/) to agree on a bounded spark capture during the failure. Paper bundles spark. A profiler report can be uploaded to a third-party viewer, so confirm the capture/report destination before starting it. Collect evidence before deciding on JVM flags, pre-generation, distance changes or a larger paid instance. The October 3 proof of concept already reported stutter during chunk generation; idle TPS does not resolve that observation.

## Record acceptance and any optional follow-up

Record the October 3 user acceptance/waiver separately from any later observations. Operator start/status/stop and backup evidence belong to their documented operational workflows; recovery acceptance still requires the isolated procedure in #8. Practicing a stop is not a restore test.

If optional follow-up is undertaken, share only its sanitized observed results and mark unperformed checks as unperformed. #9's family-testing requirements are already resolved by user acceptance/waiver; no additional family test is required for closure. Keep live recovery and #26/#27's technical gates separate from acceptance of the connected clients.
