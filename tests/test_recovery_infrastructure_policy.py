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
                "aws_volume_attachment",
            },
        )
        self.assertNotIn("aws_vpc", resources)
        self.assertNotIn("aws_subnet", resources)
        self.assertNotIn("aws_security_group", resources)

    def test_source_backup_access_is_read_only_and_instance_scoped(self):
        iam = self.source("iam.tf")
        source_policy = iam.split('resource "aws_iam_role_policy" "source_backup_read"', 1)[1].split(
            'resource "aws_iam_role_policy" "recovery_backups"', 1
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
        self.assertIn('count = var.backup_writes_enabled ? 1 : 0', iam)
        self.assertIn('local.recovery_backup_bucket_arn', iam)

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
        self.assertIn("size              = 40", host)
        self.assertIn("delete_on_termination = true", host)
        self.assertIn("http_tokens                 = \"required\"", host)
        self.assertIn("http_put_response_hop_limit = 1", host)
        self.assertIn("force_detach = false", host)
        self.assertIn("skip_destroy = false", host)

    def test_outputs_separate_private_deployment_from_cleanup_inventory(self):
        outputs = self.source("outputs.tf")
        self.assertRegex(outputs, r'output "deployment"[\s\S]+?sensitive\s+=\s+true')
        self.assertRegex(outputs, r'output "cleanup_inventory"[\s\S]+?sensitive\s+=\s+true')
        self.assertIn('backup_prefix = "instances/${var.instance_id}/"', outputs)
        self.assertIn('purpose               = "recovery-rehearsal"', outputs)
        self.assertIn("backup_bucket_name   = var.recovery_backup_bucket_name", outputs)
        self.assertIn("backup_bucket_name    = var.recovery_backup_bucket_name", outputs)


if __name__ == "__main__":
    unittest.main()
