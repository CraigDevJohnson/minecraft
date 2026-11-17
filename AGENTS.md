# Repository instructions

## Working here

- The repository runs one EC2 host per world (`survival`, `creative`, `adventure`), each with its own mods; `runtime/crafty/server.json` names the worlds and pins everything they run ([ADR 0009](docs/adr/0009-one-host-per-world.md)).
- Read `README.md`, `CONTEXT.md` for the domain terms, and the ADRs in `docs/adr/` before changing behaviour.
- Install the pinned tools with `python3 scripts/bootstrap_tools.py`, then run `.tools/bin/task check` before every push. `.tools/bin/task fmt` fixes shell, OpenTofu and Go formatting. None of these needs AWS credentials.
- Work on a feature branch and open a pull request with the template. `main` takes squash merges only.
- Write `Closes #N` only when the merge itself finishes the issue. An issue that needs a deploy, a start or a join as proof takes `Refs #N` and is closed with that evidence.
- A merge to `main` deploys nothing. Changes to the `infra` root reach AWS through the Deploy workflow: a plan, then an apply Craig approves with the plan's digest ([cloud controls](docs/operations/cloud-controls.md)). Every Deploy runs with all worlds stopped, and a change to `scripts/world-bootstrap.sh.tftpl` or `scripts/install_aws_cli.sh` replaces every world host ([worlds](docs/operations/worlds.md)).
- Where these docs say a terminal, a cloud thread counts: an agent session in a cloud container, not on Craig's computer. The thread signs in with `aws sso login --sso-session identity-center --use-device-code` and posts the link and the code for Craig to approve on his phone (aws-setup `aws-design.md` §5.2). Do not ask Craig to use his own computer for a step a cloud thread can do. The approval rules are unchanged: anything that can change AWS or Cloudflare still needs Craig's approval of that specific change first. [Cloud controls](docs/operations/cloud-controls.md#from-a-cloud-thread) says what a cloud thread cannot do. A thread gets the two roots' private input files with `task inputs:pull`, keeps what they hold out of Git, pull requests, issues and logs, and pushes one only with Craig's approval of that change ([the private inputs](docs/operations/cloud-controls.md#the-private-inputs)).
- A mod or a world is added in `runtime/crafty/server.json`, pinned by URL and SHA-256. `task check` is offline, so the Pins workflow is what downloads and checks a pin.
- Keep credentials, `operator-config/`, `*.tfvars`, `users.json` and server data out of Git, and player names and gamertags out of commits, issues and pull requests.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues for `CraigDevJohnson/minecraft`, managed with `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Use a single-context layout: root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.
