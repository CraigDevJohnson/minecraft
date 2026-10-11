# ADR 0005: Starts from The Lobby run in AWS

Status: Accepted, 2026-10-11. Supersedes part of [ADR 0002](0002-dynamic-public-ipv4-with-cloudflare-hostname.md): starts from The Lobby's Minecraft Launcher update the hostname from AWS, not the operator computer.

The Lobby's Minecraft Launcher page (issue #56) lets a Member with the `minecraft` grant start the family server. A start must point the players' hostname at the host's new address and start the game, the same steps as `minecraft_host.py start`, without the operator computer. ADR 0002 kept every ordinary start on that computer, kept the Cloudflare token in the operator's password manager, and rejected a Lambda because its role would need a second permissions boundary.

On October 11, 2026, Craig chose "Backend drives it" over having the host update DNS itself at boot, which ADR 0002 also rejected.

## Decision

- **Launcher function:** a Go Lambda ([`lambda/launcher`](../../lambda/launcher)) behind a Function URL with `AWS_IAM` auth answers the site's status and start requests. Only the site's own role in the target account may call it (the-lobby ADR 0006).
- **What it may do:** start this one host, run the adapter's `start` on it through `AWS-RunShellScript`, read and write its own state parameter, read the idle stop's state, and invoke itself. It can never stop the host. Stopping and parking the record stay with the workstation `stop`, the idle stop and EC2's shutdown drain.
- **Second boundary:** the function's role sits inside an administrator-created launcher boundary, outside the resource prefix, and the operator role may create that role only inside it. That is the cost ADR 0002 named, now accepted.
- **Token:** the zone-wide Cloudflare token moves into AWS as one SSM SecureString, `/<prefix>/launcher/cloudflare-token` by default (G6 in aws-setup's `aws-design.md`). Only the launcher role and administrators can read it, and decryption is allowed only through SSM with the AWS-managed key. OpenTofu, the function's environment, state and logs hold only its name. It is still kept off the internet-facing host.

## Consequences

- A copy of the token now lives in AWS as well as the password manager. Rotating it means updating both, as the [launcher runbook](../operations/launcher.md) describes.
- The workstation `start`, `stop` and `dns` commands are unchanged. ADR 0002 still governs them, the idle stop's parking, and the rest of the hostname design.
- A DNS failure still never blocks the game. The launcher hides the address for that run instead of showing a stale one, and its alarm emails the operator.
- The launcher is off unless `launcher` is set in the private deployment inputs, so a deployment without it keeps ADR 0002's arrangement exactly.
