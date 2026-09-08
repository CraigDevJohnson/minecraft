# Server Operations Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the first backlog issue: reproducible local tooling and credential-free infrastructure validation with a precise instance contract.

**Architecture:** A thin OpenTofu root establishes tool/provider constraints without creating cloud resources. A shared Taskfile invokes pinned tools installed under the checkout and runs the same checks locally and in CI. Documentation defines the private deployment inputs and instance boundary consumed by later infrastructure/runtime work.

**Tech Stack:** Python standard library (3.11+ host prerequisite), OpenTofu 1.12.6, Task 3.53.1, ShellCheck 0.11.0, shfmt 3.14.0, AWS provider 6.63.0, GitHub Actions.

**Spec:** [Server operations design](../specs/2026-09-07-server-operations-design.md).

## Global Constraints

- First instance ID is `survival`; data mount `/srv/minecraft`; instance data `/srv/minecraft/instances/<id>/data`; backup namespace `instances/<id>/`.
- Foundation checks must pass without AWS credentials, backend access, Docker, EULA acceptance, or a running Minecraft process.
- Public tool/provider downloads are permitted; do not describe credential-free checks as network-free.
- Keep existing security workflow and its required check names intact.
- Install tools only under ignored `.tools/`; support macOS/Linux on arm64/amd64.
- Do not introduce cloud resources or placeholder runtime pins.

---

### Task 1: Reproducible tooling and validation

**Files:**
- Create: `tools/lock.json`, `scripts/bootstrap_tools.py`, `scripts/check.sh`, `Taskfile.yml`.
- Create: `infra/versions.tf`, `infra/.terraform.lock.hcl`.
- Create: `tests/test_bootstrap_tools.py`, `.github/workflows/validate.yml`.
- Create: `docs/development.md`, `docs/operations/state-bootstrap.md`.
- Modify: `README.md`, `.gitignore`, `docs/agents/triage-labels.md`.

**Interfaces:**
- Consumes the shared instance contract in the spec; implementation of runtime operations is outside this task.
- `python3 scripts/bootstrap_tools.py` installs exact versions from `tools/lock.json` into `.tools/bin/`.
- `.tools/bin/task check` is the local/CI gate; `fmt` formats actual HCL/shell files; `infra:validate` performs backend-free initialization and validation.
- `scripts/check.sh` propagates every failure, invokes project-local tools, uses a temporary `TF_DATA_DIR`, and never changes an operator's initialized backend.

- [ ] **Step 1: Define and test tool installation behavior.**

Use one committed JSON manifest containing each tool's version and, for each supported platform, its official HTTPS URL, SHA256, archive kind and exact member path (or raw binary). Obtain real hashes from upstream release checksum files or GitHub release asset digests; verify any downloaded asset before execution.

Implement a Python standard-library installer with `main()`, using `platform.system()/machine()`, `hashlib.sha256`, `urllib.request`, and the appropriate archive reader. Install only the expected regular member, write to a temporary sibling and atomically replace the destination after verification. Do not extract arbitrary archive paths. A failed download/hash/member validation must preserve an existing tool. Re-running setup must work safely; unknown platforms fail clearly.

Expose `install_payload(payload, expected_sha256, archive_kind, member, destination)`, where payload is bytes, the checksum and archive kind are strings, member is a string or None, and destination is a pathlib.Path. Invalid data raises ValueError before changing destination. Tests use fixture bytes and a temporary destination, not live release downloads. Include a successful archive install and rejection of a wrong hash, missing/non-regular member, and download failure without overwriting an existing executable. The wrong-hash test is:

```python
with tempfile.TemporaryDirectory() as directory:
    destination = Path(directory) / "tool"
    destination.write_bytes(b"existing executable")
    with self.assertRaises(ValueError):
        install_payload(b"invalid download", "0" * 64, "raw", None, destination)
    self.assertEqual(destination.read_bytes(), b"existing executable")
```

Ensure this test fails if verification or atomic preservation is removed.

- [ ] **Step 2: Install pinned tools and establish the OpenTofu root.**

```hcl
terraform {
  required_version = "= 1.12.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.63.0"
    }
  }
}
```

Generate and commit provider locks for `darwin_arm64`, `darwin_amd64`, `linux_amd64`, and `linux_arm64`:

```sh
python3 scripts/bootstrap_tools.py
.tools/bin/tofu -chdir=infra providers lock -platform=darwin_arm64 -platform=darwin_amd64 -platform=linux_amd64 -platform=linux_arm64
```

Do not add a provider block, backend configuration or resource merely to make the root look larger.

- [ ] **Step 3: Wire the shared check/format commands and CI.**

The check path runs script syntax/ShellCheck/shfmt checks, OpenTofu formatting, bootstrap unit tests, then:

```sh
tofu -chdir=infra init -backend=false -input=false -lockfile=readonly
tofu -chdir=infra validate -no-color
```

Run those commands using the project-local binary and an isolated temporary data directory. Disable IMDS and point AWS config/credentials to nonexistent paths. Remove inherited AWS credential variables for the validation subprocess. Clean up only the temporary directory created by this invocation.

CI runs on pull requests and main pushes with contents-read permission, checkout pinned to the existing verified SHA, no persisted checkout credentials, a bounded timeout, and no AWS secrets/OIDC permission. It installs tools then invokes the exact local check command. Preserve existing security jobs.

- [ ] **Step 4: Document setup, state bootstrap and ignore boundaries.**

README links setup, commands, the design, and issues while honestly stating that game runtime and live infrastructure are separate deliveries. State bootstrap documentation describes private target/account inputs, reviewed bootstrap creation, state migration into a versioned/locked backend, and recovery of bootstrap state; it must not present unimplemented apply commands as executable today. Mark AWS authentication/creation as later operator steps, distinct from current validation.

Ignore tools, worktrees, generated nested instance data, operator config, all state/plan artifacts and runtime secrets. Preserve tracking of provider locks and safe examples. Add priority label meanings and readiness reassessment behavior to the existing triage guidance without replacing the canonical roles.

- [ ] **Step 5: Validate success and meaningful failure paths.**

```sh
python3 scripts/bootstrap_tools.py
.tools/bin/task check
.tools/bin/task fmt
git diff --check
actionlint
```

Use temporary copies to demonstrate malformed HCL and bad formatting fail the main gate. In a fresh git checkout with no `.tools/` or provider cache, bootstrap and check must pass without AWS configuration. Verify representative private files are ignored and `infra/.terraform.lock.hcl` is not ignored. Do not start Minecraft or create AWS resources.

- [ ] **Step 6: Commit and request independent review.**

Stage only this task's files and commit after checks pass. Report commit IDs, commands/results, supported platforms actually executed, and limits (no live infrastructure or game validation). Root handles the reviewed PR and remote CI; do not merge.
