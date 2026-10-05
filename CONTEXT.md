# Family Minecraft operations

Operate independent `survival` and `creative` instances for six family players using Java and Bedrock clients, with full infrastructure provisioning, persistent data, and verified recovery. On October 3, 2026, Craig directed that a separate creative world be stood up, advancing the original ongoing hobby scope of two or three instances. Each instance owns separate world data, ports, backups and lifecycle; further content can be selected as needed.

On October 3, 2026, Craig accepted the connected clients and waived requirements or validations involving consoles or family-member testing. Reported joins were Java on Mac, Bedrock on iPhone and two iPads, with four simultaneous players. PS5/Switch were untested and BedrockConnect was never configured; console guidance remains optional future help. Family-device acceptance is resolved by user acceptance/waiver, with unperformed checks recorded as unperformed rather than passed.

This acceptance does not replace [#8's live recovery proof](https://github.com/CraigDevJohnson/minecraft/issues/8) or [#26](https://github.com/CraigDevJohnson/minecraft/issues/26)/[#27](https://github.com/CraigDevJohnson/minecraft/issues/27)'s upstream, runtime, infrastructure and technical protocol safety gates.

Craig also removed recovery authorization and maintenance windows and overall recovery time ceilings on October 3, 2026. Continue the approved recovery scope without renewing permission because time elapsed. Record actual durations; command execution timeouts, the mandatory 15-minute fresh-disk provenance check, write safety and source integrity remain in force. See the [recovery procedure](docs/operations/live-recovery-rehearsal.md).

## Terms

- **Host:** the replaceable EC2 machine. Its running/stopped state controls compute billing.
- **Endpoint:** the DNS hostname players use to reach the host. Its IPv4 address changes on every host start and is parked on a never-routed address while the host is stopped.
- **Server instance:** one independently configured Minecraft runtime and content set, such as `survival` or `creative`. It owns its world data, ports, backup namespace, and lifecycle.
- **World:** a playable map/dimension within an instance. An instance can include its Overworld, Nether, and End; those do not require separate machines.
- **Content manifest:** the exact runtime, plugins/mods, resource packs, mappings, versions, and integrity metadata needed to recreate an instance.
- **Recovery point:** a verified, completed backup of the data and content configuration needed to recover an instance.

GitHub issues define task acceptance criteria. The [server design](docs/superpowers/specs/2026-09-07-server-operations-design.md) records shared requirements; [ADR 0001](docs/adr/0001-independent-server-instances.md) records the runtime boundary and [ADR 0002](docs/adr/0002-dynamic-public-ipv4-with-cloudflare-hostname.md) the endpoint.

An opt-in **host session** owns a temporary host, its replaceable root and orderly data attachment; the independent data disk remains retained in the first on-demand phase. An absent host is a valid version-2 deployment handoff, and the designated operator workstation recreates/configures it before instance start. [ADR 0003](docs/adr/0003-on-demand-host-sessions.md) records this accepted lifecycle and its separate live migration/recurring-policy evidence.

On October 5, 2026, #23's normal `on-demand-host` stop and unattended cold start passed with automatic DNS updates. The cycle preserved the original encrypted 5 GiB disk, filesystem identity, and world, player, and control checkpoints. Survival reached controller readiness in 158.700 seconds, and both worlds were ready in 232.909 seconds. Independent external DNS and direct Java and Bedrock checks passed for both worlds. Craig accepted the measured 3-minute-53-second result, and #23 is closed. The provisional 180-second target was missed; acceptance records the actual result. See the [recorded live status](docs/operations/on-demand-host.md#recorded-live-status-2026-10-05).

#24's four policy updates, effective host historical-version reads, compatible runtime installation, and trusted private configuration were verified before the off trial. The original disk moved to session ownership through an exact state-address move and a full plan with no physical resource changes. The actual session-owned handoff and installed private configuration matched. Receipt and retention proof covers 22 points, including 20 legacy points and two genuine final-session receipts, with the original recovery points preserved.

On October 5, the normal S3-only stop verified that the host, root disk, and session data EBS disk were absent. Both authoritative exact-version receipts matched the saved world, player, and control checkpoint baseline. DNS parking remains pending because the Cloudflare token is unavailable.

The cold trial exposed three workstation boundary defects: quoted UUID CLI encoding, matching GP3 defaults after successful creation, and a missing filesystem UUID in the zero-fill command. PRs #48, #49, and #50 fixed those defects and merged with passing CI. The same new host and encrypted 4 GiB disk are preserved with their recorded target UUID and protected receipts. An actual 906-second provenance wait passed, then the zero-fill parser refused the command before disk writes. The disk remains unformatted, and no game has started. The original proof is now historical; corrected initialization requires the native fresh 900-second proof. Expired AWS SSO credentials and password-manager access currently block the explicit repair and DNS recovery. Initialization, restore, and actual failure/resume proof remain pending; #24 stays open. Recovery authorization windows and overall time ceilings remain removed.

The October 5, 2026, 08:12 UTC upstream check found no qualifying release for #26/#27. Paper 26.3 had 67 builds: 48 ALPHA, 19 BETA, and no STABLE builds. Its latest build was #152 BETA. Geyser 2.11.3 build 1248 still supported Java 26.2 and Bedrock 26.30 through 26.52, and [Geyser PR #6712](https://github.com/GeyserMC/Geyser/pull/6712) remained open. This maintenance build does not qualify as the required new upstream support.
