# Minecraft

Repository setup for Minecraft work. No server, mod, or application code is configured yet.

Use feature branches and pull requests for changes. Keep credentials, local configuration, and generated server data out of Git.

Report suspected vulnerabilities using the [security policy](SECURITY.md).

The security workflow checks for secrets with Gitleaks, validates GitHub Actions syntax with actionlint, and audits workflows and Dependabot configuration with zizmor. These checks cover the current repository files; add language and dependency scanners when application code and manifests arrive.

Dependabot checks GitHub Actions dependencies weekly, grouping minor and patch version updates separately from security updates. Version updates wait seven days after release; security updates are not delayed by that cooldown. Scanner CLI releases and their download checksums are pinned in the workflow and require manual updates: verify each replacement against its upstream release before changing the version and SHA256 together.
