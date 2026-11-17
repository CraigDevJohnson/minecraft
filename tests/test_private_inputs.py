import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUCKET = "example-private-state-bucket"
NAMES = ["deployment.tfvars", "backend.hcl", "bootstrap.tfvars", "bootstrap.backend.hcl"]

# An aws stand-in for `aws s3api get-object` and `put-object`: the bucket is the folder FAKE_S3, an object's
# version is made from its name, and every call's arguments are recorded. With FAKE_AWS_DENIED set it refuses the
# way S3 does, naming the bucket in its message.
FAKE_AWS = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_AWS_LOG"
operation="$2"
shift 2
bucket="" key="" body="" outfile=""
while [[ $# -gt 0 ]]; do
  case "$1" in
  --no-cli-pager) ;;
  --bucket) bucket="$2"; shift ;;
  --key) key="$2"; shift ;;
  --body) body="$2"; shift ;;
  --query | --output) shift ;;
  *) outfile="$1" ;;
  esac
  shift
done
if [[ -n "${FAKE_AWS_DENIED:-}" ]]; then
  printf 'An error occurred (AccessDenied) when calling the operation: not authorized on "arn:aws:s3:::%s/%s"\\n' "$bucket" "$key" >&2
  exit 254
fi
case "$operation" in
get-object)
  if [[ ! -f "$FAKE_S3/$key" ]]; then
    echo "An error occurred (NoSuchKey) when calling the GetObject operation: The specified key does not exist." >&2
    exit 254
  fi
  cp "$FAKE_S3/$key" "$outfile"
  ;;
put-object)
  mkdir -p "$(dirname "$FAKE_S3/$key")"
  cp "$body" "$FAKE_S3/$key"
  ;;
*)
  echo "unexpected call: $operation" >&2
  exit 2
  ;;
esac
printf 'version-of-%s\\n' "${key##*/}"
"""


class PrivateInputsTests(unittest.TestCase):
    """The private input files move between the state bucket and operator-config/inputs/ without being shown."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="private-inputs-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "private_inputs.sh", self.root / "scripts" / "private_inputs.sh")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        aws = self.bin / "aws"
        aws.write_text(FAKE_AWS)
        aws.chmod(aws.stat().st_mode | stat.S_IXUSR)
        self.s3 = self.root / "s3"
        (self.s3 / "inputs").mkdir(parents=True)
        self.log = self.root / "aws.log"
        self.log.write_text("")
        self.inputs = self.root / "operator-config" / "inputs"

    def run_script(self, *arguments, **changes):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", MINECRAFT_STATE_BUCKET=BUCKET,
                   AWS_PROFILE="example-operator", FAKE_S3=str(self.s3), FAKE_AWS_LOG=str(self.log))
        for name, value in changes.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return subprocess.run(["bash", str(self.root / "scripts" / "private_inputs.sh"), *arguments],
                              capture_output=True, text=True, env=env, check=False)

    def calls(self):
        return self.log.read_text().splitlines()

    def test_pull_writes_what_the_bucket_holds_for_its_owner_only_and_prints_no_content(self):
        (self.s3 / "inputs" / "deployment.tfvars").write_text('resource_prefix = "sentinel-private-inputs"\n')
        (self.s3 / "inputs" / "backend.hcl").write_text('bucket = "sentinel-private-backend"\n')
        completed = self.run_script("pull")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines(), [
            "deployment.tfvars: pulled (version version-of-deployment.tfvars)",
            "backend.hcl: pulled (version version-of-backend.hcl)",
            "bootstrap.tfvars: not in the bucket",
            "bootstrap.backend.hcl: not in the bucket",
        ])
        self.assertEqual(completed.stderr, "")
        self.assertEqual((self.inputs / "deployment.tfvars").read_text(), 'resource_prefix = "sentinel-private-inputs"\n')
        self.assertEqual((self.inputs / "backend.hcl").read_text(), 'bucket = "sentinel-private-backend"\n')
        # Nothing but the two files is left behind, and only their owner can read them.
        self.assertEqual(sorted(path.name for path in self.inputs.iterdir()), ["backend.hcl", "deployment.tfvars"])
        for path in [self.inputs / "deployment.tfvars", self.inputs / "backend.hcl"]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)
        self.assertEqual(stat.S_IMODE(self.inputs.stat().st_mode), 0o700)
        for hidden in ["sentinel", BUCKET]:
            self.assertNotIn(hidden, completed.stdout + completed.stderr)
        self.assertEqual(len(self.calls()), 4)
        for name, call in zip(NAMES, self.calls()):
            self.assertTrue(call.startswith(f"s3api get-object --no-cli-pager --bucket {BUCKET} "), call)
            self.assertIn(f"--key inputs/{name} ", call)

    def test_pull_leaves_a_local_file_that_differs_unless_forced(self):
        (self.s3 / "inputs" / "deployment.tfvars").write_text('resource_prefix = "sentinel-from-the-bucket"\n')
        self.inputs.mkdir(parents=True)
        local = self.inputs / "deployment.tfvars"
        local.write_text('resource_prefix = "sentinel-local-edit"\n')
        completed = self.run_script("pull")
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertEqual(local.read_text(), 'resource_prefix = "sentinel-local-edit"\n')
        self.assertIn("deployment.tfvars: left as it is, because the local file differs from the bucket's copy", completed.stderr)
        self.assertIn("FORCE=1", completed.stderr)
        self.assertNotIn("pulled", completed.stdout)
        self.assertNotIn("sentinel", completed.stdout + completed.stderr)
        self.assertEqual([path.name for path in self.inputs.iterdir()], ["deployment.tfvars"])

        completed = self.run_script("pull", "--force")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(local.read_text(), 'resource_prefix = "sentinel-from-the-bucket"\n')
        self.assertEqual(stat.S_IMODE(local.stat().st_mode), 0o600)
        self.assertIn("deployment.tfvars: pulled (version version-of-deployment.tfvars)", completed.stdout)

        # A local file that already matches is no difference to report.
        completed = self.run_script("pull")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("deployment.tfvars: already the bucket's copy", completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_pull_fails_when_the_bucket_holds_none_of_the_four(self):
        completed = self.run_script("pull")
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout.splitlines(), [f"{name}: not in the bucket" for name in NAMES])
        self.assertIn("the bucket holds none of the four private input files", completed.stderr)
        self.assertEqual(list(self.inputs.iterdir()), [])

    def test_a_refusal_from_aws_fails_without_naming_the_bucket(self):
        (self.s3 / "inputs" / "deployment.tfvars").write_text("private\n")
        for arguments in (["pull"], ["push", "deployment.tfvars", "--from", str(self.s3 / "inputs" / "deployment.tfvars")]):
            with self.subTest(arguments=arguments):
                completed = self.run_script(*arguments, FAKE_AWS_DENIED="1")
                self.assertEqual(completed.returncode, 1)
                self.assertIn("deployment.tfvars: the AWS call failed", completed.stderr)
                self.assertIn("AccessDenied", completed.stderr)
                self.assertIn("arn:aws:s3:::<the state bucket>/inputs/deployment.tfvars", completed.stderr)
                self.assertNotIn(BUCKET, completed.stdout + completed.stderr)
        self.assertFalse((self.inputs / "deployment.tfvars").exists())

    def test_push_uploads_the_files_that_exist_and_prints_no_content(self):
        self.inputs.mkdir(parents=True)
        (self.inputs / "backend.hcl").write_text('bucket = "sentinel-private-backend"\n')
        (self.inputs / "bootstrap.tfvars").write_text('environment = "sentinel-private-bootstrap"\n')
        # Anything else kept beside them stays where it is.
        (self.inputs / "tfplan").write_text("sentinel-saved-plan\n")
        completed = self.run_script("push")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines(), [
            "backend.hcl: pushed (version version-of-backend.hcl)",
            "bootstrap.tfvars: pushed (version version-of-bootstrap.tfvars)",
        ])
        self.assertEqual(completed.stderr, "")
        self.assertEqual(sorted(path.name for path in (self.s3 / "inputs").iterdir()), ["backend.hcl", "bootstrap.tfvars"])
        self.assertEqual((self.s3 / "inputs" / "backend.hcl").read_text(), 'bucket = "sentinel-private-backend"\n')
        for hidden in ["sentinel", BUCKET]:
            self.assertNotIn(hidden, completed.stdout + completed.stderr)
        for call in self.calls():
            self.assertTrue(call.startswith(f"s3api put-object --no-cli-pager --bucket {BUCKET} "), call)

    def test_push_of_one_name_uploads_only_that_file(self):
        self.inputs.mkdir(parents=True)
        for name in NAMES:
            (self.inputs / name).write_text(f"{name}\n")
        completed = self.run_script("push", "bootstrap.backend.hcl")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines(), ["bootstrap.backend.hcl: pushed (version version-of-bootstrap.backend.hcl)"])
        self.assertEqual([path.name for path in (self.s3 / "inputs").iterdir()], ["bootstrap.backend.hcl"])

    def test_push_from_uploads_a_file_of_another_name_as_the_name_given(self):
        elsewhere = self.root / "private" / "ci.deployment.tfvars"
        elsewhere.parent.mkdir()
        elsewhere.write_text('resource_prefix = "sentinel-first-upload"\n')
        completed = self.run_script("push", "deployment.tfvars", "--from", str(elsewhere))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.splitlines(), ["deployment.tfvars: pushed (version version-of-deployment.tfvars)"])
        self.assertEqual((self.s3 / "inputs" / "deployment.tfvars").read_text(), 'resource_prefix = "sentinel-first-upload"\n')
        self.assertEqual([path.name for path in (self.s3 / "inputs").iterdir()], ["deployment.tfvars"])
        self.assertNotIn("sentinel", completed.stdout + completed.stderr)
        self.assertFalse((self.root / "operator-config").exists())

    def test_a_name_outside_the_four_is_refused_before_any_call(self):
        self.inputs.mkdir(parents=True)
        (self.inputs / "users.json").write_text("{}\n")
        for arguments in (["push", "users.json"], ["push", "tfplan"], ["push", "../inputs/deployment.tfvars"],
                          ["push", "deployment.tfvars backend.hcl"], ["push", "deployment.tfvars", "backend.hcl"],
                          ["push", "users.json", "--from", str(self.inputs / "users.json")],
                          ["push", "--from", str(self.inputs / "users.json")], ["push", "deployment.tfvars", "--from"],
                          ["pull", "deployment.tfvars"], ["push", "--force"], ["list"], []):
            with self.subTest(arguments=arguments):
                completed = self.run_script(*arguments)
                self.assertEqual(completed.returncode, 2, completed.stderr)
                self.assertIn("where NAME is one of: deployment.tfvars backend.hcl bootstrap.tfvars bootstrap.backend.hcl",
                              completed.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(list((self.s3 / "inputs").iterdir()), [])

    def test_push_fails_when_there_is_no_file_to_send(self):
        completed = self.run_script("push")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("nothing to push", completed.stderr)
        completed = self.run_script("push", "backend.hcl")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("backend.hcl: operator-config/inputs/backend.hcl is not a file", completed.stderr)
        completed = self.run_script("push", "backend.hcl", "--from", str(self.root / "missing.hcl"))
        self.assertEqual(completed.returncode, 1)
        self.assertIn("backend.hcl: the FROM path is not a file", completed.stderr)
        self.assertEqual(self.calls(), [])

    def test_the_bucket_and_the_profile_are_required_and_the_bucket_is_never_named(self):
        for missing, said in (("MINECRAFT_STATE_BUCKET", "MINECRAFT_STATE_BUCKET must name the state bucket"),
                              ("AWS_PROFILE", "AWS_PROFILE must name a profile that assumes the minecraft-operator role")):
            for mode in ("pull", "push"):
                with self.subTest(missing=missing, mode=mode):
                    completed = self.run_script(mode, **{missing: None})
                    self.assertEqual(completed.returncode, 2)
                    self.assertIn(said, completed.stderr)
                    self.assertNotIn(BUCKET, completed.stdout + completed.stderr)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / "operator-config").exists())

    def test_the_task_targets_pass_force_name_and_from(self):
        taskfile = (ROOT / "Taskfile.yml").read_text()
        self.assertIn('cmd: ./scripts/private_inputs.sh pull {{if eq .FORCE "1"}}--force{{end}}', taskfile)
        self.assertIn('cmd: ./scripts/private_inputs.sh push {{if .NAME}}"{{.NAME}}"{{end}} {{if .FROM}}--from "{{.FROM}}"{{end}}',
                      taskfile)


if __name__ == "__main__":
    unittest.main()
