#!/usr/bin/env python3
"""Administrator-side adoption of the host permissions boundary for an existing deployment.

Runs the reviewed steps in docs/operations/state-bootstrap.md with the administrator's
credentials (for example AWS_PROFILE set to the administrator SSO profile). Each action
is separate so Craig can review its output before the next one:

  render     write the account-specific boundary and operator policies (offline)
  validate   run IAM Access Analyzer validate-policy on both rendered documents
  inventory  list every prefixed role with its boundary and policies (steps 1 and 6)
  apply      create the boundary policy, save the operator's current policy, replace it
  rollback   restore the operator policy saved by apply

The boundary-only OpenTofu apply (step 5) and the operator smoke test (step 4) stay manual.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs/operations/operator-role"
EXAMPLE_ACCOUNT = "123456789012"
EXAMPLE_REGION = "us-west-2"
EXAMPLE_PREFIX = "example-prefix-"
EXAMPLE_BOUNDARY = "example-host-boundary"
BOUNDARY_FILE = "host-boundary.json"
OPERATOR_FILE = "operator-permissions.json"
ROLLBACK_FILE = "operator-policy.rollback.json"
BLOCKING_FINDINGS = {"ERROR", "SECURITY_WARNING"}
AWS_TIMEOUT = 60


class AdoptionError(Exception):
    """A controlled stop; nothing after the failing step has run."""


class AWSCLI:
    def __init__(self, region: str):
        self.region = region

    def run(self, service: str, operation: str, *arguments: str) -> dict:
        command = ["aws", "--region", self.region, "--output", "json", "--no-cli-pager", "--no-cli-auto-prompt", service, operation, *arguments]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=AWS_TIMEOUT, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AdoptionError(f"AWS {service} {operation} could not run: {exc}") from exc
        if result.returncode:
            if "NoSuchEntity" in result.stderr:
                raise NoSuchEntity(f"{service} {operation}")
            raise AdoptionError(f"AWS {service} {operation} failed: {result.stderr.strip()}")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError as exc:
            raise AdoptionError(f"AWS {service} {operation} returned invalid JSON") from exc


class NoSuchEntity(AdoptionError):
    pass


def check_inputs(args) -> None:
    if not re.fullmatch(r"[0-9]{12}", args.account_id):
        raise AdoptionError("--account-id must be a 12-digit AWS account ID")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", args.region):
        raise AdoptionError("--region is invalid")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*-", args.iam_prefix):
        raise AdoptionError("--iam-prefix must be lowercase and end with '-', like the roles it names")
    for flag, name in (("--boundary-name", args.boundary_name), ("--operator-role", args.operator_role)):
        if not re.fullmatch(r"[\w+=,.@-]{1,64}", name):
            raise AdoptionError(f"{flag} is not a valid IAM name")
        if name.lower().startswith(args.iam_prefix):
            raise AdoptionError(f"{flag} must be named outside the IAM prefix so prefix-scoped operator permissions never cover it")


def boundary_arn(args) -> str:
    return f"arn:aws:iam::{args.account_id}:policy/{args.boundary_name}"


def render_document(name: str, args) -> dict:
    text = (
        (EXAMPLES / f"{name}.json.example").read_text()
        .replace(EXAMPLE_ACCOUNT, args.account_id)
        .replace(EXAMPLE_REGION, args.region)
        .replace(EXAMPLE_PREFIX, args.iam_prefix)
        .replace(EXAMPLE_BOUNDARY, args.boundary_name)
    )
    if "example" in text.lower():
        raise AdoptionError(f"rendered {name} still contains an example placeholder; review this tool")
    return json.loads(text)


def write_private(path: Path, document) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")


def read_rendered(args, name: str) -> dict:
    try:
        return json.loads((args.out_dir / name).read_text())
    except (OSError, ValueError) as exc:
        raise AdoptionError(f"cannot read {name}; run render first") from exc


def render(args, aws=None) -> list[str]:
    write_private(args.out_dir / BOUNDARY_FILE, render_document("host-boundary", args))
    write_private(args.out_dir / OPERATOR_FILE, render_document("permissions", args))
    return [f"wrote {args.out_dir / BOUNDARY_FILE}", f"wrote {args.out_dir / OPERATOR_FILE}", f"boundary ARN: {boundary_arn(args)}"]


def validate(args, aws) -> list[str]:
    lines, blocked = [], False
    for name in (BOUNDARY_FILE, OPERATOR_FILE):
        document = json.dumps(read_rendered(args, name))
        findings = aws.run("accessanalyzer", "validate-policy", "--policy-type", "IDENTITY_POLICY", "--policy-document", document).get("findings", [])
        for finding in findings:
            blocked |= finding.get("findingType") in BLOCKING_FINDINGS
            lines.append(f"{name}: {finding.get('findingType')} {finding.get('issueCode')}: {finding.get('findingDetails')}")
        if not findings:
            lines.append(f"{name}: no findings")
    if blocked:
        raise AdoptionError("Access Analyzer reported errors or security warnings:\n" + "\n".join(lines))
    return lines


def verify_account(args, aws) -> None:
    account = aws.run("sts", "get-caller-identity").get("Account")
    if account != args.account_id:
        raise AdoptionError(f"credentials are for account {account}, not {args.account_id}")


def inventory(args, aws) -> list[str]:
    verify_account(args, aws)
    roles = [r["RoleName"] for r in aws.run("iam", "list-roles").get("Roles", []) if r["RoleName"].lower().startswith(args.iam_prefix)]
    if not roles:
        raise AdoptionError("no prefixed roles found: check --iam-prefix and --account-id")
    lines, unbounded, stop = [], [], []
    for name in sorted(roles):
        role = aws.run("iam", "get-role", "--role-name", name)["Role"]
        boundary = role.get("PermissionsBoundary", {}).get("PermissionsBoundaryArn")
        attached = [p["PolicyArn"] for p in aws.run("iam", "list-attached-role-policies", "--role-name", name).get("AttachedPolicies", [])]
        inline = aws.run("iam", "list-role-policies", "--role-name", name).get("PolicyNames", [])
        lines.append(f"{name}: boundary={boundary or 'NONE'} attached={attached or '[]'} inline={inline or '[]'}")
        if boundary != boundary_arn(args):
            unbounded.append(name)
            if boundary or attached:
                stop.append(name)
    if stop:
        raise AdoptionError("roles carry another boundary or attached policies; stop and review:\n" + "\n".join(lines))
    if args.expect_all_bounded and unbounded:
        raise AdoptionError("roles still lack the host boundary: " + ", ".join(unbounded) + "\n" + "\n".join(lines))
    if unbounded:
        lines.append("without the host boundary: " + ", ".join(unbounded) + " (only the existing host role is expected before adoption;")
        lines.append("  compare each inline policy with `aws iam get-role-policy` against infra/iam.tf before continuing)")
    else:
        lines.append("every prefixed role carries the host boundary")
    return lines


def apply(args, aws) -> list[str]:
    if args.confirm_operator_role != args.operator_role:
        raise AdoptionError("--confirm-operator-role must repeat --operator-role")
    verify_account(args, aws)
    boundary, permissions = read_rendered(args, BOUNDARY_FILE), read_rendered(args, OPERATOR_FILE)
    rollback = args.out_dir / ROLLBACK_FILE
    if rollback.exists():
        raise AdoptionError(f"{rollback} already exists; apply ran before. Inspect it, then use rollback or move it aside")

    aws.run("iam", "get-role", "--role-name", args.operator_role)
    attached = aws.run("iam", "list-attached-role-policies", "--role-name", args.operator_role).get("AttachedPolicies", [])
    if attached:
        raise AdoptionError("the operator role has attached managed policies; the reviewed design uses inline policies only")
    inline = aws.run("iam", "list-role-policies", "--role-name", args.operator_role).get("PolicyNames", [])
    if args.policy_name not in inline:
        raise AdoptionError(f"inline policy {args.policy_name!r} not found on the operator role; it has {inline}")
    current = aws.run("iam", "get-role-policy", "--role-name", args.operator_role, "--policy-name", args.policy_name)["PolicyDocument"]

    lines = []
    try:
        version = aws.run("iam", "get-policy", "--policy-arn", boundary_arn(args))["Policy"]["DefaultVersionId"]
        existing = aws.run("iam", "get-policy-version", "--policy-arn", boundary_arn(args), "--version-id", version)["PolicyVersion"]["Document"]
        if existing != boundary:
            raise AdoptionError(f"{boundary_arn(args)} exists with a different document; stop and review")
        lines.append(f"boundary policy already exists and matches: {boundary_arn(args)}")
    except NoSuchEntity:
        aws.run("iam", "create-policy", "--policy-name", args.boundary_name, "--policy-document", json.dumps(boundary),
                "--description", "Permissions boundary for Minecraft host and recovery roles")
        lines.append(f"created boundary policy {boundary_arn(args)}")

    write_private(rollback, {"role": args.operator_role, "policy_name": args.policy_name, "document": current})
    lines.append(f"saved the operator's current policy to {rollback}")
    aws.run("iam", "put-role-policy", "--role-name", args.operator_role, "--policy-name", args.policy_name, "--policy-document", json.dumps(permissions))
    lines.append(f"replaced inline policy {args.policy_name!r} on {args.operator_role}; other inline policies untouched: {[p for p in inline if p != args.policy_name]}")
    lines.append("next: run the step 4 smoke test as the operator now, and run rollback if it fails")
    return lines


def rollback(args, aws) -> list[str]:
    verify_account(args, aws)
    try:
        saved = json.loads((args.out_dir / ROLLBACK_FILE).read_text())
    except (OSError, ValueError) as exc:
        raise AdoptionError(f"cannot read {ROLLBACK_FILE}") from exc
    if saved.get("role") != args.operator_role:
        raise AdoptionError(f"{ROLLBACK_FILE} belongs to role {saved.get('role')!r}, not {args.operator_role!r}")
    aws.run("iam", "put-role-policy", "--role-name", saved["role"], "--policy-name", saved["policy_name"], "--policy-document", json.dumps(saved["document"]))
    return [f"restored inline policy {saved['policy_name']!r} on {saved['role']}; the boundary policy was left in place"]


ACTIONS = {"render": render, "validate": validate, "inventory": inventory, "apply": apply, "rollback": rollback}


def main(argv=None, *, aws=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--iam-prefix", required=True, help="prefix of every role and bucket the roots create, e.g. 'family-'")
    parser.add_argument("--boundary-name", default="minecraft-host-boundary")
    parser.add_argument("--operator-role", default="minecraft-operator")
    parser.add_argument("--out-dir", type=Path, required=True, help="private directory, e.g. operator-config/family/boundary")
    subparsers = parser.add_subparsers(dest="action", required=True)
    for name in ("render", "validate", "rollback"):
        subparsers.add_parser(name)
    subparsers.add_parser("inventory").add_argument("--expect-all-bounded", action="store_true", help="step 6: fail unless every role carries the boundary")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--policy-name", required=True, help="inline policy on the operator role to replace, as inventory lists it")
    apply_parser.add_argument("--confirm-operator-role", required=True)
    args = parser.parse_args(argv)
    try:
        check_inputs(args)
        for line in ACTIONS[args.action](args, aws or AWSCLI(args.region)):
            print(line)
        return 0
    except AdoptionError as exc:
        print(f"stopped: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
