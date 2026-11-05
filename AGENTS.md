# Repository instructions

## Working here

- Read `README.md`, `CONTEXT.md` for the domain terms, and the ADRs in `docs/adr/` before changing behaviour.
- Install the pinned tools with `python3 scripts/bootstrap_tools.py`, then run `.tools/bin/task check` before every push. `.tools/bin/task fmt` fixes shell, OpenTofu and Go formatting. None of these needs AWS credentials.
- Work on a feature branch and open a pull request with the template. `main` takes squash merges only.
- Write `Closes #N` only when the merge itself finishes the issue. An issue that needs a deploy, a start or a join as proof takes `Refs #N` and is closed with that evidence.
- A merge to `main` deploys nothing. Changes to the `infra` root reach AWS through the Deploy workflow: a plan, then an apply Craig approves with the plan's digest ([cloud controls](docs/operations/cloud-controls.md)).
- Keep credentials, `operator-config/`, `*.tfvars`, `users.json` and server data out of Git, and player names and gamertags out of commits, issues and pull requests.

## Agent skills

### Issue tracker

Issues and specs live in GitHub Issues for `CraigDevJohnson/minecraft`, managed with `gh`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Use a single-context layout: root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.
