# Contributing

This repository runs one family Minecraft server and is not taking outside contributions. Bug reports and ideas are welcome as [issues](https://github.com/CraigDevJohnson/minecraft/issues/new/choose); report suspected vulnerabilities through the [security policy](SECURITY.md) instead.

## How changes are made

1. Start from an issue. New issues get `needs-triage`; [triage labels](docs/agents/triage-labels.md) describe the rest.
2. Work on a feature branch and open a pull request against `main` using the pull request template.
3. Run the shared gate before pushing (see [development setup](docs/development.md)):

   ```sh
   python3 scripts/bootstrap_tools.py
   .tools/bin/task check
   ```

4. Merge with a squash once the Validate and Security checks pass and review threads are resolved.
5. Changes to the host, the Launcher or the pinned server content reach AWS only through the Deploy workflow: a plan, then an apply with the plan's digest ([cloud controls](docs/operations/cloud-controls.md)).

## Keep out of Git

Credentials, `operator-config/`, `*.tfvars`, `users.json`, server data and logs stay local; `.gitignore` covers them. Leave player names and gamertags out of commits, issues and pull requests.
