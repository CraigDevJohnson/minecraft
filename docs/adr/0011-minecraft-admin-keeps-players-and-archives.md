# ADR 0011: Minecraft Admin keeps the allowlist, the operators and the archives

Status: Accepted, 2026-11-14. Changes what [ADR 0006](0006-cloud-controls.md) says of who may change the allowlist and restore an archive; a forced stop stays the operator's. Built and checked offline. Nothing is proven live until the routes answer the site on dev after a Deploy.

ADR 0006 kept the allowlist and a restore for an operator: the Launcher answered a site's role `403`, so only the Server control workflow and `task server:*` could change who may join or put an archive back. On November 14, 2026, Craig asked for three admin functions on The Lobby's Minecraft Admin page (`/minecraft/admin`), which the site shows only to a Member with its `minecraft-admin` grant: the allowlist (the-lobby #86), each world's archives with a restore (the-lobby #87), and each world's operators (the-lobby #91).

## Decision

- **A site's role may do everything but force a stop.** `POST /server/allowlist` and `POST /server/restore` accept the site's role, as `GET /mods` and `POST /mods` already did. The site checks the Member's `minecraft-admin` grant before it calls them and sends no Member header, so the Launcher logs no Member and no player name for them. `{"force": true}` on a stop is still the operator's alone.
- **`GET /server/allowlist` lists the allowlist of record** as `{edition, name}`, with the name an admin would type to add the player (a Java account's name, a Bedrock gamertag as written), and no UUID.
- **`GET /server/archives?world=<id>` lists a world's archives** under `instances/<world>/crafty/` in the backup bucket, newest first, at most 50, each with the name `POST /server/restore` takes. The Launcher's role, and the administrator-created launcher boundary, gain `s3:ListBucket` on the bucket for those prefixes only; the Launcher reads no archive itself.
- **Each world has operators, kept by the Launcher.** One String parameter, `/<prefix>/launcher/operators`, holds each world's operators as entries of the allowlist of record. `GET /server/operators` lists them per world, and `POST /server/operators` adds or removes one player of the allowlist on one world (`409 not_allowlisted` otherwise). Every start writes the world's list over its `ops.json`, as it writes the allowlist over `whitelist.json`, and a change reaches a running world through a new host adapter action, `operator`, which runs `op` or `deop` on the console and confirms the change in `ops.json`. Removing a player from the allowlist also removes them from every world's operators, because an operator joins a running server whether or not the allowlist lists them.

## Consequences

- Anyone with the `minecraft-admin` grant can change who may join, make a player an operator and put an older archive in place of a stopped world. The Lobby asks before a restore, naming the world and the archive.
- A world has no operator but the ones the record lists from its next start: an operator made by hand in the Crafty console loses it at that start unless they are added on the page.
- A Bedrock player goes by Floodgate's name, which a running server knows only once that player has joined it. Making them an operator of a running world they have never joined fails with `operators_failed`; the record keeps the change and the world's next start makes it.
- The records are standard SSM parameters written with a read and then a write, like the mods switches and the allowlist, so two changes in the same instant can lose one.
- The boundary needs a new version from an administrator before `GET /server/archives` can answer; until then it answers `502 archives_unconfirmed`.
- Rejected: an archives list the host keeps, because the host is stopped whenever a restore is offered; operators changed only at a world's next start, because an admin making a player an operator while they play expects it at once.
