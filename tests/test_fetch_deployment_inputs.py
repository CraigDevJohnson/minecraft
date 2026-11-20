import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUCKET = "example-private-state-bucket"
BACKEND = f'bucket       = "{BUCKET}"\nkey          = "sentinel/terraform.tfstate"\nregion       = "us-west-2"\nuse_lockfile = true\n'

# An aws stand-in for `aws s3api get-object`: the bucket is the folder FAKE_S3, an object's version is made from
# its name, and every call's arguments are recorded. With FAKE_AWS_DENIED set it refuses the way S3 does, naming
# the bucket in its message.
FAKE_AWS = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_AWS_LOG"
operation="$2"
shift 2
bucket="" key="" outfile=""
while [[ $# -gt 0 ]]; do
  case "$1" in
  --no-cli-pager) ;;
  --bucket) bucket="$2"; shift ;;
  --key) key="$2"; shift ;;
  --query | --output) shift ;;
  *) outfile="$1" ;;
  esac
  shift
done
if [[ "$operation" != get-object ]]; then
  echo "unexpected call: $operation" >&2
  exit 2
fi
if [[ -n "${FAKE_AWS_DENIED:-}" ]]; then
  printf 'An error occurred (AccessDenied) when calling the GetObject operation: not authorized on "arn:aws:s3:::%s/%s"\\n' "$bucket" "$key" >&2
  exit 254
fi
if [[ ! -f "$FAKE_S3/$key" ]]; then
  echo "An error occurred (NoSuchKey) when calling the GetObject operation: The specified key does not exist." >&2
  exit 254
fi
cp "$FAKE_S3/$key" "$outfile"
printf 'version-of-%s\\n' "${key##*/}"
"""


class FetchDeploymentInputsTests(unittest.TestCase):
    """The Deploy workflow reads deployment.tfvars from the bucket its backend settings name, without showing either."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fetch-deployment-inputs-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        aws = self.bin / "aws"
        aws.write_text(FAKE_AWS)
        aws.chmod(aws.stat().st_mode | stat.S_IXUSR)
        self.s3 = self.root / "s3"
        (self.s3 / "inputs").mkdir(parents=True)
        self.log = self.root / "aws.log"
        self.log.write_text("")
        self.deploy = self.root / "deploy"
        self.deploy.mkdir()
        (self.deploy / "backend.hcl").write_text(BACKEND)

    def run_script(self, **changes):
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", DEPLOY_DIR=str(self.deploy),
                   FAKE_S3=str(self.s3), FAKE_AWS_LOG=str(self.log))
        for name, value in changes.items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return subprocess.run(["bash", str(ROOT / "scripts" / "fetch_deployment_inputs.sh")],
                              capture_output=True, text=True, env=env, check=False)

    def calls(self):
        return self.log.read_text().splitlines()

    def test_reads_the_buckets_copy_for_its_owner_only_and_prints_no_content(self):
        (self.s3 / "inputs" / "deployment.tfvars").write_text('resource_prefix = "sentinel-private-inputs"\n')
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "deployment.tfvars: read from the state bucket (version version-of-deployment.tfvars)\n")
        self.assertEqual(completed.stderr, "")
        fetched = self.deploy / "deployment.tfvars"
        self.assertEqual(fetched.read_text(), 'resource_prefix = "sentinel-private-inputs"\n')
        self.assertEqual(stat.S_IMODE(fetched.stat().st_mode), 0o600)
        # Nothing but the two files is left in the deploy directory.
        self.assertEqual(sorted(path.name for path in self.deploy.iterdir()), ["backend.hcl", "deployment.tfvars"])
        for hidden in ["sentinel", BUCKET]:
            self.assertNotIn(hidden, completed.stdout + completed.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(self.calls()[0].startswith(f"s3api get-object --no-cli-pager --bucket {BUCKET} --key inputs/deployment.tfvars "),
                        self.calls()[0])

    def test_a_failed_read_ends_the_run_without_naming_the_bucket_or_leaving_a_file(self):
        (self.s3 / "inputs" / "deployment.tfvars").write_text('resource_prefix = "sentinel-private-inputs"\n')
        for said, changes in (("AccessDenied", {"FAKE_AWS_DENIED": "1"}), ("NoSuchKey", {"FAKE_S3": str(self.root / "empty")})):
            with self.subTest(said=said):
                completed = self.run_script(**changes)
                self.assertEqual(completed.returncode, 1)
                self.assertEqual(completed.stdout, "")
                self.assertIn("::error::deployment.tfvars could not be read from the state bucket", completed.stderr)
                self.assertIn("task inputs:push", completed.stderr)
                self.assertIn(said, completed.stderr)
                self.assertNotIn(BUCKET, completed.stderr)
                self.assertEqual([path.name for path in self.deploy.iterdir()], ["backend.hcl"])
        self.assertIn('arn:aws:s3:::<the state bucket>/inputs/deployment.tfvars', self.run_script(FAKE_AWS_DENIED="1").stderr)

    def test_backend_settings_must_name_one_bucket(self):
        for text in ('key = "sentinel/terraform.tfstate"\n', f'bucket = "{BUCKET}"\nbucket = "sentinel-second-bucket"\n', "bucket = \n"):
            with self.subTest(text=text):
                (self.deploy / "backend.hcl").write_text(text)
                completed = self.run_script()
                self.assertEqual(completed.returncode, 1)
                self.assertIn("backend.hcl must name the state bucket once", completed.stderr)
                self.assertNotIn("sentinel", completed.stdout + completed.stderr)
                self.assertNotIn(BUCKET, completed.stdout + completed.stderr)
                self.assertEqual(self.calls(), [])

    def test_needs_the_deploy_directory_with_its_backend_settings(self):
        (self.deploy / "backend.hcl").unlink()
        for changes in ({}, {"DEPLOY_DIR": None}):
            with self.subTest(changes=changes):
                completed = self.run_script(**changes)
                self.assertEqual(completed.returncode, 2)
                self.assertIn("DEPLOY_DIR must name the directory holding backend.hcl", completed.stderr)
                self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
