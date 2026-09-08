"""Guard lifecycle/source guarantees that a creation-only mock plan cannot expose."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"


def resource_blocks(directory):
    """Read formatter-normalized top-level resource blocks, not arbitrary HCL."""
    blocks = {}
    for path in directory.glob("*.tf"):
        text = path.read_text()
        for kind, name, body in re.findall(
            r'^resource "([^"]+)" "([^"]+)" \{\n(.*?)^\}', text, re.M | re.S
        ):
            blocks[f"{kind}.{name}"] = body
    return blocks


class ProtectedInfrastructurePolicyTests(unittest.TestCase):
    def test_retained_resources_refuse_destroy(self):
        main = resource_blocks(INFRA)
        state = resource_blocks(INFRA / "state-bootstrap")
        protected = {
            name: body
            for name, body in main.items()
            if name == "aws_ebs_volume.data" or name.startswith("aws_s3_")
        }
        protected.update(state)
        self.assertIn("aws_ebs_volume.data", protected)
        self.assertIn("aws_s3_bucket.backups", protected)
        self.assertIn("aws_s3_bucket.state", protected)
        for name, body in protected.items():
            with self.subTest(resource=name):
                self.assertRegex(body, r"lifecycle\s*\{\s*prevent_destroy\s*=\s*true\s*\}")

    def test_storage_identity_has_no_dependency_on_replaceable_compute(self):
        for directory in [INFRA, INFRA / "state-bootstrap"]:
            for name, body in resource_blocks(directory).items():
                if name.startswith("aws_s3_") or name == "aws_ebs_volume.data":
                    with self.subTest(resource=name):
                        self.assertNotRegex(
                            body,
                            r"\b(?:aws_instance|aws_volume_attachment|aws_eip_association|aws_iam_instance_profile)\.",
                        )
                        self.assertNotRegex(body, r"\breplace_triggered_by\b")
        state_source = "\n".join(path.read_text() for path in (INFRA / "state-bootstrap").glob("*.tf"))
        self.assertNotRegex(state_source, r'\bmodule\s+"|\baws_instance\.')

    def test_data_is_not_owned_by_instance_block_device_configuration(self):
        host = resource_blocks(INFRA)["aws_instance.host"]
        self.assertNotRegex(host, r"\bebs_block_device\s*\{")
        self.assertNotRegex(host, r"\binstance_market_options\s*\{")
        self.assertNotRegex(host, r"\bkey_name\s*=")
        self.assertNotIn("aws_ebs_volume", (INFRA / "network.tf").read_text())

    def test_only_separate_game_rules_can_introduce_ingress(self):
        blocks = resource_blocks(INFRA)
        self.assertEqual(
            {name for name in blocks if name.startswith("aws_security_group.")},
            {"aws_security_group.host"},
        )
        self.assertEqual(
            {name for name in blocks if "ingress" in name or name.startswith("aws_security_group_rule.")},
            {"aws_vpc_security_group_ingress_rule.game"},
        )
        self.assertNotRegex(blocks["aws_security_group.host"], r"\bingress\s*(?:=|\{)")

    def test_host_cannot_gain_static_credentials_or_extra_policies(self):
        blocks = resource_blocks(INFRA)
        self.assertEqual(
            {name for name in blocks if name.startswith("aws_iam_role_policy.")},
            {"aws_iam_role_policy.ssm", "aws_iam_role_policy.backups"},
        )
        self.assertFalse(
            any(name.startswith(("aws_iam_access_key.", "aws_iam_role_policy_attachment.", "aws_iam_policy_attachment.")) for name in blocks)
        )

    def test_host_bootstrap_receives_retained_identity_and_both_scripts(self):
        host = resource_blocks(INFRA)["aws_instance.host"]
        self.assertRegex(host, r"user_data_base64\s*=\s*base64gzip\(templatefile\(")
        self.assertIn("../scripts/host-bootstrap.sh.tftpl", host)
        self.assertIn("../scripts/minecraft_storage.py", host)
        self.assertRegex(host, r"data_volume_id\s*=\s*aws_ebs_volume.data.id\b")
        self.assertRegex(host, r"data_filesystem_uuid\s*=\s*var.data_filesystem_uuid\b")

    def test_state_backends_require_encryption_and_native_locking(self):
        for path in [INFRA / "versions.tf", INFRA / "state-bootstrap/remote-backend.tf.example"]:
            with self.subTest(path=path.name):
                source = path.read_text()
                self.assertRegex(source, r'backend\s+"s3"\s*\{')
                self.assertRegex(source, r"\bencrypt\s*=\s*true\b")
                self.assertRegex(source, r"\buse_lockfile\s*=\s*true\b")

    def test_no_age_expiration_of_current_backups_or_state(self):
        backup_lifecycle = resource_blocks(INFRA)["aws_s3_bucket_lifecycle_configuration.backups"]
        self.assertNotRegex(backup_lifecycle, r"(?<![a-z_])expiration\s*\{")
        self.assertFalse(
            any(name.startswith("aws_s3_bucket_lifecycle_configuration.") for name in resource_blocks(INFRA / "state-bootstrap"))
        )

    def test_deployment_outputs_are_private(self):
        self.assertRegex(
            (INFRA / "outputs.tf").read_text(),
            r'(?s)output "deployment" \{.*?sensitive\s*=\s*true',
        )


if __name__ == "__main__":
    unittest.main()
