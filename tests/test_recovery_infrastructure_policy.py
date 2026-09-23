import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECOVERY = ROOT / "infra" / "recovery-rehearsal"


class RecoveryInfrastructurePolicyTests(unittest.TestCase):
    def source(self, name):
        return (RECOVERY / name).read_text()

    def test_recovery_root_owns_only_temporary_compute_storage_and_endpoint(self):
        resources = "\n".join(path.read_text() for path in RECOVERY.glob("*.tf"))
        resource_types = set(re.findall(r'resource\s+"([^"]+)"', resources))
        self.assertEqual(
            resource_types,
            {
                "aws_ebs_volume",
                "aws_eip",
                "aws_eip_association",
                "aws_iam_instance_profile",
                "aws_iam_role",
                "aws_iam_role_policy",
                "aws_instance",
                "aws_s3_bucket",
                "aws_s3_bucket_ownership_controls",
                "aws_s3_bucket_policy",
                "aws_s3_bucket_public_access_block",
                "aws_s3_bucket_server_side_encryption_configuration",
                "aws_s3_bucket_versioning",
                "aws_security_group",
                "aws_vpc_security_group_egress_rule",
                "aws_volume_attachment",
            },
        )
        self.assertNotIn('resource "aws_vpc"', resources)
        self.assertNotIn('resource "aws_subnet"', resources)

    def test_source_backup_access_is_read_only_and_instance_scoped(self):
        iam = self.source("iam.tf")
        source_policy = iam.split('resource "aws_iam_role_policy" "source_backup_read"', 1)[1].split(
            'resource "aws_iam_role_policy" "recovery_backup_read"', 1
        )[0]
        for forbidden in (
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:DeleteObjectVersion",
            "s3:AbortMultipartUpload",
            "s3:ListMultipartUploadParts",
            "s3:GetObjectVersion",
        ):
            self.assertNotIn(forbidden, source_policy)
        self.assertIn('Action   = ["s3:GetObject"]', source_policy)
        self.assertIn('"instances/${var.instance_id}/*"', source_policy)
        self.assertIn('"${local.source_backup_bucket_arn}/instances/${var.instance_id}/*"', source_policy)

    def test_temporary_backup_reads_are_retained_when_writes_are_disabled(self):
        iam = self.source("iam.tf")
        temporary_read_policy = iam.split(
            'resource "aws_iam_role_policy" "recovery_backup_read"', 1
        )[1].split('resource "aws_iam_role_policy" "recovery_backup_writes"', 1)[0]
        temporary_write_policy = iam.split(
            'resource "aws_iam_role_policy" "recovery_backup_writes"', 1
        )[1]

        self.assertNotIn("count =", temporary_read_policy)
        for required in ("s3:ListBucket", "s3:GetObject", "s3:GetBucketLocation"):
            self.assertIn(required, temporary_read_policy)
        self.assertIn('Resource = [local.recovery_backup_bucket_arn]', temporary_read_policy)
        self.assertIn('"instances/${var.instance_id}/*"', temporary_read_policy)
        self.assertIn(
            'Resource = ["${local.recovery_backup_bucket_arn}/instances/${var.instance_id}/*"]',
            temporary_read_policy,
        )
        for forbidden in (
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:AbortMultipartUpload",
            "s3:ListMultipartUploadParts",
        ):
            self.assertNotIn(forbidden, temporary_read_policy)

        self.assertIn("count = var.backup_writes_enabled ? 1 : 0", temporary_write_policy)
        for required in (
            "s3:PutObject",
            "s3:DeleteObject",
            "s3:AbortMultipartUpload",
            "s3:ListMultipartUploadParts",
        ):
            self.assertIn(required, temporary_write_policy)
        self.assertIn(
            'Resource = ["${local.recovery_backup_bucket_arn}/instances/${var.instance_id}/*"]',
            temporary_write_policy,
        )
        for forbidden in ("s3:ListBucket", "s3:GetObject", "s3:GetBucketLocation"):
            self.assertNotIn(forbidden, temporary_write_policy)

    def test_temporary_backup_bucket_is_private_and_destroyable(self):
        storage = self.source("storage.tf")
        self.assertIn("force_destroy = true", storage)
        self.assertIn('sse_algorithm = "AES256"', storage)
        self.assertIn('status = "Enabled"', storage)
        self.assertIn("block_public_acls       = true", storage)
        self.assertIn('"aws:SecureTransport" = "false"', storage)

    def test_recovery_host_and_disk_preserve_storage_and_metadata_guards(self):
        host = self.source("host.tf")
        self.assertIn("encrypted         = true", host)
        self.assertIn("type              = \"gp3\"", host)
        self.assertIn("volume_size           = var.recovery_root_volume_size_gib", host)
        self.assertIn("size              = var.recovery_data_volume_size_gib", host)
        self.assertIn("delete_on_termination = true", host)
        self.assertIn("http_tokens                 = \"required\"", host)
        self.assertIn("http_put_response_hop_limit = 1", host)
        self.assertIn("force_detach = false", host)
        self.assertIn("skip_destroy = false", host)

    def test_recovery_network_is_owned_no_ingress_and_web_egress_only(self):
        resources = "\n".join(path.read_text() for path in RECOVERY.glob("*.tf"))
        host = self.source("host.tf")
        self.assertIn('resource "aws_security_group" "recovery"', resources)
        self.assertIn('resource "aws_vpc_security_group_egress_rule" "web"', resources)
        self.assertNotIn('resource "aws_vpc_security_group_ingress_rule"', resources)
        self.assertIn("vpc_id      = var.source_vpc_id", resources)
        self.assertNotIn("ingress {", resources)
        self.assertNotIn("egress {", resources)
        self.assertIn("vpc_security_group_ids      = [aws_security_group.recovery.id]", host)
        self.assertIn('for_each          = toset(["80", "443"])', resources)
        self.assertIn("from_port         = tonumber(each.value)", resources)
        self.assertIn("to_port           = tonumber(each.value)", resources)
        self.assertIn('ip_protocol       = "tcp"', resources)
        self.assertIn('cidr_ipv4         = "0.0.0.0/0"', resources)

    def test_outputs_separate_private_deployment_from_cleanup_inventory(self):
        outputs = self.source("outputs.tf")
        self.assertRegex(outputs, r'output "deployment"[\s\S]+?sensitive\s+=\s+true')
        self.assertRegex(outputs, r'output "cleanup_inventory"[\s\S]+?sensitive\s+=\s+true')
        self.assertIn('backup_prefix = "instances/${var.instance_id}/"', outputs)
        self.assertIn('purpose               = "recovery-rehearsal"', outputs)
        self.assertIn("backup_bucket_name   = var.recovery_backup_bucket_name", outputs)
        self.assertIn("backup_bucket_name    = var.recovery_backup_bucket_name", outputs)
        self.assertIn("security_group_id     = aws_security_group.recovery.id", outputs)


if __name__ == "__main__":
    unittest.main()
