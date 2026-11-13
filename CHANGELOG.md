# Changelog

Notable changes to the family's Minecraft worlds and the tooling around them, newest first. The repository publishes no releases, so entries are grouped by the month they merged to `main`, with the pull request in brackets. Routine dependency updates are left out; the [commit history](https://github.com/CraigDevJohnson/minecraft/commits/main) has them.

## November 2026

### Added

- Seven mods that make survival gentler for young players join the catalog, off until switched on: Universal Graves, Sleep, Healing Campfire, No Hostiles Around Campfire, Replanting Crops, Starter Kit and Zombie Proof Doors. A mod can now name the library mods it needs (#101).
- spark and Chunky join the mod catalog on every world and Lithium on adventure, off until switched on, now that the Launcher's host command may be 64 KiB (#100).
- Lithium, FallingTree and Essential Commands join the mod catalog, off on every world until switched on, and the mods page shows only what each mod does (#97).
- Each world (survival, creative, adventure) runs on its own host with its own mods and its own address (#91).
- The Pins workflow downloads every pinned file and verifies its SHA-256 (#89).
- Idle stop: a world with nobody playing stops on its own, using the Launcher's Cloudflare token (#86).
- Dependabot covers the OpenTofu roots and labels its pull requests (#80), and its minor and patch updates merge once checks pass (#90), with GitHub Actions updates held for review (#92).
- Issue forms, a pull request template, CODEOWNERS and contributing notes (#79).

### Changed

- The functions build with one exact Go version, so a Deploy plan and its apply build the same packages on any runner (#99).
- Survival and creative run on Fabric and adventure on NeoForge, replacing Paper (#95).
- Geyser moved to build 1288 and Paper to build 242, and the runbooks were tidied (#85).

### Removed

- The single family host is retired; its archives stay in the backup bucket (#93).

## October 2026

### Added

- The family server was rebuilt on Crafty Controller and Multiverse (#52).
- The Launcher: a status and start backend for The Lobby (#57), one function per Lobby site environment (#60), then stop, the allowlist and deploys from GitHub Actions (#63).
- Server control restores archives, and the Crafty console is reachable without a port-forward (#72).
- NetherNet is served by applying the pinned server content on each start (#71).
- A separate creative world alongside survival (#37).
- On-demand, read-only health checks (#36).
- The host is recreated on demand after a verified drain (#32), and S3-only sessions are restored before Minecraft starts (#33, #34).
- An explicit RakNet network transport option (#39).
- Idle hosts drain and stop safely, and crossplay gained NetherNet dual transport.

### Changed

- Paper 26.3 build 227 is pinned and ViaVersion dropped (#77).
- Geyser 2.12.1 is pinned so Bedrock 26.60 can join (#74).
- The repository was prepared for public visibility (#65).
- A Cloudflare hostname replaced the Elastic IP, and the data disk is 5 GiB (#25).
- Current Bedrock (26.50 and later) and Java 26.3 clients can join the 26.2 server (#20).

### Fixed

- The Launcher's allowlist works on the Crafty host (#69).
- The workflow guard refuses escapes and non-ASCII it cannot read (#68).
- Lifecycle, retention and state reconciliation fixes found during the first live runs (#38, #41 to #44, #46 to #50).
- Fresh EBS volumes initialize correctly and operator-created host roles are bounded (#16); associating the Elastic IP no longer replaces the host (#19).

### Removed

- The S3-only, on-demand and Compose-era code, the retired lifecycle inputs and the rehearsal root (#75, #76).

## September 2026

### Added

- The repository's security baseline and vulnerability reporting policy (#1).
- Reproducible server operations tooling (#10).
- The EC2 host and its protected infrastructure (#11).
- The crossplay runtime and safe session commands (#12).
- Verified backups, an isolated restore and a live recovery rehearsal (#13, #14, #15).
