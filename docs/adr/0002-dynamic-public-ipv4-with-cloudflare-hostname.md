# ADR 0002: Dynamic public IPv4 with a Cloudflare hostname

Status: Accepted, 2026-10-03. Starts from The Lobby's Minecraft Launcher are governed by [ADR 0005](0005-lobby-launcher-starts-from-aws.md) instead.

The host runs only during play, but its Elastic IP billed all month: about $3.65, roughly 43% of the idle bill. An auto-assigned public IPv4 address has the same hourly rate and bills only while the host runs. It changes on every start, so players need a stable name that follows the host.

Give the host an auto-assigned address and no Elastic IP. Players use one endpoint: a single Cloudflare A record, DNS only, TTL 60, supplied as a private deployment input. The operator command (`scripts/minecraft_host.py`) points the record at the host after a start and parks it on `192.0.2.1` after a stop. AWS can assign a released address to another customer, so the name must not keep pointing at one. A DNS failure never blocks the game start, the drain, or the EC2 stop.

The ordinary start/stop update runs on the operator computer. The optional [idle-stop function](../operations/idle-stop.md) also parks the record after its automatic stop. It retrieves a separate administrator-owned token secret at runtime, with access scoped to that secret; it is disabled by default and requires a reviewed enablement plan. That needs no IAM, permissions-boundary, or AWS resource change, shows a failure to the person starting the server, and keeps the token off the internet-facing host. A Cloudflare token cannot be scoped to one record, so it stays in the operator's password manager and is set for one command.

Rejected:

- A host boot unit: it puts a zone-wide token on the game host, cannot park the record on stop, and reports failures only in the host journal.
- An EC2 state-change rule and Lambda: the operator role lacks the permissions, and a role it creates would need a second permissions boundary.
- Cloudflare proxy, Tunnel, and Spectrum: the proxy carries only HTTP, Bedrock needs public UDP 19132, and Spectrum is paid.
- Route 53: DNS already lives at Cloudflare.

Consequences:

- A start or stop outside the operator command or enabled idle-stop function leaves the record stale or unparked until `dns`, `start`, or `stop` next runs. If console or other scheduled paths become routine, move the same update and park logic into an EC2 state-change Lambda.
- Clients can reach the old address briefly after a start. Nothing may pin the raw address; use the hostname everywhere.
- The recovery rehearsal deployment has no hostname, so it can never take over the family record.

This conflicts with no earlier ADR; [ADR 0001](0001-independent-server-instances.md) is unaffected. Operating details are in [host commands](../operations/host-commands.md#public-hostname).
