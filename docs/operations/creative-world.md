# Creative world

The family has two independent server instances, `survival` and `creative`. They share the existing host and retained disk, with separate world/player data, ports, Compose projects and backup namespaces. Creative uses the same reviewed Paper, Geyser, Floodgate and ViaVersion pins as the playing survival instance; adding it does not upgrade survival or copy its world.

| Instance | Java port | Bedrock port | Heap | Mode |
| --- | --- | --- | --- | --- |
| survival | 25565 | 19132 | 5G | survival |
| creative | 25566 | 19134 | 1G | creative |

Java players add the family hostname with `:25566`. Bedrock players add a second server entry using the same hostname and port `19134`. The family allowlist and normal authentication remain enabled. Creative forces its configured game mode when players join. Its smaller heap and view/simulation distances suit the shared host; measure both instances before increasing memory or running a third instance concurrently.

## Configure an additional instance

The canonical [runtime package](runtime-installation.md) includes the creative definition and exact reviewed RakNet manifest. The additive installer publishes a coherent controller generation while leaving existing desired files, active runtime files, data and containers intact. It updates the private catalogs and launchers under the same runtime lock. This path applies to the retained-host deployment; an on-demand migration still uses its normal stopped adoption procedure.

Add `creative` to the infrastructure's `instance_ids`, and select these additional listeners:

```hcl
additional_game_ingress = {
  creative_java    = { protocol = "tcp", port = 25566 }
  creative_bedrock = { protocol = "udp", port = 19134 }
}
```

Verify that the saved plan adds the creative listeners and backup namespace without replacing the host or disk. The exact deployment input to the additive installer preserves every existing field and instance entry and adds only the creative data path and backup prefix. Run the installer from the verified root-owned package on the host, then start creative through the existing instance controller. The controller records start intent before creating world data. Reuse verified family access settings, and keep survival world/player files separate.

## Operate each world

The workstation deployment handoff and host operator catalog include both instances. Use the normal [host commands](host-commands.md) to start creative and inspect both:

```sh
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json start --instance creative
python3 scripts/minecraft_host.py --deployment operator-config/family/deployment.json status
```

Starting through the workstation needs the existing private Cloudflare token reference described in the host runbook. On the host, an instance-only stop backs up creative and leaves survival and EC2 running:

```sh
sudo /usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json stop creative
```

Host stop drains and backs up both configured instances before stopping EC2. Their completed points use `instances/survival/` and `instances/creative/` respectively. The current manual GitHub health workflow probes survival's listeners; it is not evidence that creative is ready. Check creative's controller status and both of its public endpoints separately.
