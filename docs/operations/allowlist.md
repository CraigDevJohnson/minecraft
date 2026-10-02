# Instance allowlist changes

Allowlist changes require an explicit configured instance and a running, ready session. The runtime holds the global lifecycle lock, rejects changes while the host is draining, and keeps the current session backup pending. The next successful cold backup includes `whitelist.json` and the other mutable access state.

Provide one player's verified identity in a private JSON file with mode `0600`. The object has `uuid` and `name` fields: use the actual server UUID in canonical lowercase hyphenated form, and the verified Java account name or unprefixed Bedrock gamertag. The file must refer to the same account in both fields. Family identities and real join validation remain launch inputs in issues #7/#9; test fixtures do not establish those identities.

```sh
sudo /usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json allowlist survival add --edition java --player-file /private/java-player.json
sudo /usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json allowlist survival add --edition bedrock --player-file /private/bedrock-player.json
sudo /usr/local/sbin/minecraft-instance --config /etc/minecraft/operator.json allowlist survival remove --edition bedrock --player-file /private/bedrock-player.json
```

Java changes use the server's `whitelist add/remove` command. This operator interface accepts 3–16 ASCII letters, digits, and underscores for Java names. Bedrock changes use Floodgate's `fwhitelist add/remove` command with the gamertag, without the configured Floodgate username prefix. The [Floodgate documentation](https://geysermc.org/wiki/floodgate/features/) defines that command routing.

For Bedrock, this interface accepts Unicode letters/numbers, ASCII spaces, and underscores, with no leading/trailing spaces and at most 16 UTF-16 units. Tags containing spaces are passed as one quoted argument. Quotes, backslashes, control characters, and command punctuation are rejected. Floodgate build 141's [argument parser](https://github.com/GeyserMC/Floodgate/blob/81b65ccb766bf8024858ebf3eefa6974056aecc5/core/src/main/java/org/geysermc/floodgate/player/audience/PlayerAudienceArgument.java) supports quoted names and uses the same length boundary. Its source is unchanged from build 140; build 141 changes only the cloud command-framework dependency, from 2.0.0-beta.16 to 2.0.1. Passing this syntax check does not verify an Xbox account or its UUID.

The operation reads the private server allowlist before sending a command. If the supplied UUID already has the requested membership, it returns `allowlist: unchanged` without another RCON mutation. Otherwise it sends one command and polls the private `whitelist.json` for up to 60 seconds until that exact UUID is present for add, or absent for remove. It reports `allowlist: updated` only after observing the requested membership. Invalid or duplicate UUID entries prevent verified completion.

Floodgate's [whitelist implementation](https://github.com/GeyserMC/Floodgate/blob/81b65ccb766bf8024858ebf3eefa6974056aecc5/core/src/main/java/org/geysermc/floodgate/command/WhitelistCommand.java) can resolve gamertags asynchronously. RCON response text alone cannot establish completion. A lookup failure, missing change, or mismatched UUID reaches a bounded failure. A command may have changed access or may still be completing when verification fails; inspect private server state and correct the identity or lookup problem before retrying. Repeating the same request verifies current state and avoids repeating a completed change.

Successful output contains only the instance ID, edition, operation, and `updated`/`unchanged` result. Boundary failures use fixed diagnostics. UUIDs, names, raw RCON responses, and allowlist contents remain private. These commands do not disable the allowlist, change online authentication, assign operator privileges, or expose RCON publicly. Access removal changes allowlist membership; it does not promise immediate disconnection of an already-connected player.

`tests/test_minecraft_access.py` exercises the public module API using a controlled RCON/private-file boundary. It covers routing, quoted Unicode Bedrock tags, eventual completion, duplicate operations, exact UUID matching, failed lookup/removal, malformed files, private-output handling, and command-injection rejection. These fixtures do not contact identity services or connect family devices.
