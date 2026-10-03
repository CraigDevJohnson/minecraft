# Family Minecraft operations

Build one fresh survival server for six family players using Java and Bedrock clients, with full infrastructure provisioning, persistent data, and verified recovery. PlayStation and Switch need real device/network validation.

## Terms

- **Host:** the replaceable EC2 machine. Its running/stopped state controls compute billing.
- **Endpoint:** the DNS hostname players use to reach the host. Its IPv4 address changes on every host start and is parked on a never-routed address while the host is stopped.
- **Server instance:** one independently configured Minecraft runtime and content set, initially `survival`. It owns its world data, ports, backup namespace, and lifecycle.
- **World:** a playable map/dimension within an instance. An instance can include its Overworld, Nether, and End; those do not require separate machines.
- **Content manifest:** the exact runtime, plugins/mods, resource packs, mappings, versions, and integrity metadata needed to recreate an instance.
- **Recovery point:** a verified, completed backup of the data and content configuration needed to recover an instance.

GitHub issues define task acceptance criteria. The [server design](docs/superpowers/specs/2026-09-07-server-operations-design.md) records shared requirements; [ADR 0001](docs/adr/0001-independent-server-instances.md) records the runtime boundary and [ADR 0002](docs/adr/0002-dynamic-public-ipv4-with-cloudflare-hostname.md) the endpoint.

An opt-in **host session** owns a temporary host, its replaceable root and orderly data attachment; the independent data disk remains retained in the first on-demand phase. An absent host is a valid version-2 deployment handoff, and the designated operator workstation recreates/configures it before instance start. [ADR 0003](docs/adr/0003-on-demand-host-sessions.md) records this accepted lifecycle and its separate live migration/recurring-policy evidence.
