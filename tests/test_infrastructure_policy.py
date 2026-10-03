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

    def test_retained_data_disk_size_is_an_explicit_private_input(self):
        # A default would let an existing deployment silently plan a smaller disk. EBS cannot
        # shrink: the provider plans a smaller size as an in-place update that AWS then rejects.
        self.assertRegex(resource_blocks(INFRA)["aws_ebs_volume.data"], r"(?m)^\s*size\s*=\s*var\.data_volume_size_gib$")
        variable = re.search(
            r'^variable "data_volume_size_gib" \{\n(.*?)^\}', (INFRA / "variables.tf").read_text(), re.M | re.S)
        self.assertIsNotNone(variable, "data_volume_size_gib is not declared")
        self.assertRegex(variable.group(1), r"(?m)^\s*type\s*=\s*number$")
        self.assertRegex(variable.group(1), r"(?m)^\s*nullable\s*=\s*false$")
        self.assertNotRegex(variable.group(1), r"(?m)^\s*default\s*=")

    def test_auto_assigned_public_ip_does_not_force_host_replacement(self):
        # EC2 releases an auto-assigned address when the host stops, and the provider then
        # reads associate_public_ip_address=false. Without ignore_changes every plan taken
        # while the host is stopped, which is most of the time, would replace it.
        hosts = {
            "infra": resource_blocks(INFRA)["aws_instance.host"],
            "recovery-rehearsal": resource_blocks(INFRA / "recovery-rehearsal")["aws_instance.recovery"],
        }
        for root, body in hosts.items():
            with self.subTest(root=root):
                self.assertRegex(body, r"associate_public_ip_address\s*=\s*true")
                self.assertRegex(body, r"lifecycle\s*\{[^}]*ignore_changes\s*=\s*\[\s*associate_public_ip_address\s*\]")

    def test_no_root_declares_an_elastic_ip(self):
        # An allocated address bills all month; the hosts use an address billed only while running.
        for root in (INFRA, INFRA / "state-bootstrap", INFRA / "recovery-rehearsal"):
            with self.subTest(root=root.name):
                source = "\n".join(path.read_text() for path in root.glob("*.tf"))
                self.assertNotRegex(source, r"\baws_eip(?:_association)?\b")
        self.assertRegex(resource_blocks(INFRA)["aws_subnet.public"], r"map_public_ip_on_launch\s*=\s*false")

    def test_host_launch_waits_for_its_internet_route(self):
        # The address exists from first boot, so cloud-init reaches packages and SSM at once;
        # the Elastic IP association used to order this after the route.
        host = resource_blocks(INFRA)["aws_instance.host"]
        self.assertRegex(host, r"depends_on\s*=\s*\[[^\]]*\baws_route\.internet\b[^\]]*\baws_route_table_association\.public\b")

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
        role_policies = {name: body for name, body in blocks.items() if name.startswith("aws_iam_role_policy.")}
        self.assertEqual(
            {name for name, body in role_policies.items() if re.search(r"\brole\s*=\s*aws_iam_role\.host\.", body)},
            {"aws_iam_role_policy.ssm", "aws_iam_role_policy.backups"},
        )
        # The only other inline policy belongs to the separate idle-stop function role, never the host.
        self.assertEqual(set(role_policies), {"aws_iam_role_policy.ssm", "aws_iam_role_policy.backups", "aws_iam_role_policy.idle_stop"})
        self.assertRegex(role_policies["aws_iam_role_policy.idle_stop"], r"\brole\s*=\s*aws_iam_role\.idle_stop\[0\]\.id\b")
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
