#!/usr/bin/env python3
"""Bounded S3 operations using the host's AWS CLI and temporary credentials."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import tempfile
import time

PART_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
COMMAND_TIMEOUT = 900


class BackupError(Exception):
    """A backup operation could not be completed or verified."""


class _AWSError(BackupError):
    def __init__(self, operation: str, code: str):
        super().__init__(f"S3 {operation} failed; no recovery point was verified")
        self.code = code


def valid_version(version):
    if (not isinstance(version, str) or not re.fullmatch(r"[\x21-\x7e]{1,1024}", version)
            or version == "null"):
        raise BackupError("a non-null S3 object version is required")
    return version


class S3Store:
    def __init__(self, bucket: str, region: str | None = None, *, timeout: int = 900, profile: str | None = None,
                 environment_provider=None):
        if type(timeout) is not int or not 0 < timeout <= 3000:
            raise BackupError("backup operation timeout must be between 1 and 3000 seconds")
        self.bucket = bucket
        self.region = region
        if profile is not None and (not isinstance(profile, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}', profile)):
            raise BackupError('invalid configured AWS CLI profile')
        self.profile = profile
        if environment_provider is not None and (not callable(environment_provider) or profile is not None):
            raise BackupError('temporary operator credentials require a provider without an explicit profile')
        self.environment_provider = environment_provider
        self.deadline = time.monotonic() + timeout

    def for_operation(self, *, timeout: int):
        return S3Store(self.bucket, self.region, timeout=timeout, profile=self.profile,
                       environment_provider=self.environment_provider)

    def _call(self, operation: str, *arguments: str, download_limit: tuple[Path, int] | None = None,
              transfer_budget: bool = False) -> dict:
        started = time.monotonic()
        if started >= self.deadline:
            raise BackupError("S3 operation timed out")
        deadline = (self.deadline if operation == 'get-object' and transfer_budget and download_limit is not None
                    else min(self.deadline, started + COMMAND_TIMEOUT))
        command = ["aws", "s3api", operation, "--bucket", self.bucket,
                   "--output", "json", "--no-cli-pager", "--no-cli-auto-prompt",
                   "--cli-connect-timeout", "10", "--cli-read-timeout", "60"]
        if self.region:
            command.extend(("--region", self.region))
        if self.profile:
            command.extend(("--profile", self.profile))
        command.extend(arguments)
        try:
            source_environment = self.environment_provider() if self.environment_provider else os.environ
            if not isinstance(source_environment, (dict, type(os.environ))) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in source_environment.items()):
                raise ValueError()
        except Exception as error:
            raise BackupError('temporary operator credentials are unavailable') from error
        environment = dict(source_environment, AWS_PAGER="", AWS_CLI_AUTO_PROMPT="off",
                           AWS_RETRY_MODE="standard", AWS_MAX_ATTEMPTS="3", LC_ALL="C")
        process = None
        output = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=environment)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    if download_limit and download_limit[0].exists() and download_limit[0].stat().st_size > download_limit[1]:
                        raise BackupError("S3 download exceeds the safety limit")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BackupError("S3 command timed out")
                    for stream, _ in selector.select(min(remaining, 1)):
                        chunk = os.read(stream.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(stream.fileobj)
                            continue
                        output[stream.data].extend(chunk)
                        if sum(map(len, output.values())) > MAX_OUTPUT_BYTES:
                            raise BackupError("S3 command output exceeds the safety limit")
            result = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if result:
                match = re.search(rb"An error occurred \(([^)\r\n]+)\) when calling the "
                                  + re.escape(operation.title().replace("-", "").encode())
                                  + rb" operation", output["stderr"])
                raise _AWSError(operation, match[1].decode("ascii", "replace") if match else "unknown")
            value = json.loads(output["stdout"] or b"{}")
            if not isinstance(value, dict):
                raise BackupError("S3 returned invalid response metadata")
            return value
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            raise BackupError("S3 command failed, timed out, or returned invalid metadata") from error
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()

    def upload(self, key: str, source: Path) -> None:
        try:
            size = source.stat().st_size
            if not source.is_file() or size > MAX_ARCHIVE_BYTES:
                raise BackupError("backup upload is not a file within the archive size limit")
            if size > PART_BYTES:
                self._multipart_upload(key, source)
                return
            with source.open("rb") as handle:
                checksum = base64.b64encode(hashlib.file_digest(handle, "sha256").digest()).decode("ascii")
            try:
                self._call("put-object", "--key", key, "--body", str(source),
                           "--if-none-match", "*", "--checksum-sha256", checksum)
            except _AWSError as error:
                if error.code not in ("PreconditionFailed", "412"):
                    raise
        except OSError as error:
            raise BackupError("cannot read backup upload source") from error

    def download(self, key: str, target: Path, *, max_bytes: int) -> bool:
        return self._download(key, target, max_bytes=max_bytes) is not None

    def download_revision(self, key: str, target: Path, *, max_bytes: int,
                          version_id: str | None = None) -> dict | None:
        """Read one immutable version, including when selecting the current key."""
        if version_id is not None:
            valid_version(version_id)
        return self._download(key, target, max_bytes=max_bytes, version_id=version_id, versioned=True)

    def _download(self, key, target, *, max_bytes, version_id=None, versioned=False):
        if type(max_bytes) is not int or not 0 <= max_bytes <= MAX_ARCHIVE_BYTES:
            raise BackupError("invalid backup download size limit")
        target = Path(target)
        version_arguments = ("--version-id", version_id) if version_id is not None else ()
        try:
            metadata = self._call("head-object", "--key", key, *version_arguments)
        except _AWSError as error:
            if version_id is not None:
                # An exact version cannot be substituted with the current key.
                if error.code in ("NoSuchVersion", "NoSuchKey"):
                    return None
                raise
            if error.code == "NoSuchKey":
                return None
            if error.code in ("404", "NotFound"):
                # HEAD hides the difference between a missing key and a missing bucket.
                listing = self._call("list-objects-v2", "--prefix", key, "--max-keys", "1", "--no-paginate")
                contents = listing.get("Contents", [])
                if (type(listing.get("IsTruncated")) is not bool or not isinstance(contents, list)
                        or len(contents) > 1 or any(not isinstance(item, dict)
                            or not isinstance(item.get("Key"), str) or not item["Key"].startswith(key)
                            or item["Key"] == key for item in contents)):
                    raise BackupError("S3 object absence could not be confirmed")
                return None
            raise
        size = metadata.get("ContentLength")
        etag = metadata.get("ETag")
        if type(size) is not int or not 0 <= size <= max_bytes or not isinstance(etag, str) or not etag:
            raise BackupError("S3 object size or revision is invalid or exceeds the download limit")
        selected_version = valid_version(metadata.get("VersionId")) if versioned else None
        if version_id is not None and selected_version != version_id:
            raise BackupError("S3 returned a different object version")
        if versioned:
            version_arguments = ("--version-id", selected_version)
        try:
            with tempfile.TemporaryDirectory(prefix=".minecraft-download-", dir=target.parent) as directory:
                temporary = Path(directory) / "object"
                result = self._call("get-object", "--key", key, *version_arguments,
                                    "--if-match", etag, str(temporary),
                                    download_limit=(temporary, max_bytes), transfer_budget=key.endswith('.tar.gz'))
                if (not temporary.is_file() or temporary.stat().st_size != size
                        or type(result.get("ContentLength")) is not int or result["ContentLength"] != size
                        or (versioned and (result.get("VersionId") != selected_version or result.get("ETag") != etag))):
                    raise BackupError("S3 download size differs from the selected object")
                os.replace(temporary, target)
            return {"key": key, "version_id": selected_version, "etag": etag, "size_bytes": size}
        except OSError as error:
            raise BackupError("cannot write the verified S3 download") from error

    def conditional_write(self, key: str, source: Path, *, expected_etag: str | None) -> dict:
        """Advance a pointer only if its independently observed current revision matches."""
        if expected_etag is not None and (not isinstance(expected_etag, str) or not expected_etag):
            raise BackupError("invalid expected S3 selection revision")
        try:
            if not source.is_file() or not 0 < source.stat().st_size <= MAX_OUTPUT_BYTES:
                raise BackupError("selection publication exceeds its safety limit")
            with source.open("rb") as stream:
                checksum = base64.b64encode(hashlib.file_digest(stream, "sha256").digest()).decode("ascii")
            condition = ("--if-match", expected_etag) if expected_etag is not None else ("--if-none-match", "*")
            result = self._call("put-object", "--key", key, "--body", str(source),
                                *condition, "--checksum-sha256", checksum)
            version = valid_version(result.get("VersionId"))
            etag = result.get("ETag")
            if not isinstance(etag, str) or not etag:
                raise BackupError("S3 did not identify the published selection")
            return {"key": key, "version_id": version, "etag": etag, "size_bytes": source.stat().st_size}
        except OSError as error:
            raise BackupError("cannot read the selection publication") from error

    def _list_objects(self, prefix: str, *, include_last_modified: bool):
        objects = []
        seen = set()
        token = None
        tokens = set()
        for _ in range(1000):
            arguments = ["--prefix", prefix, "--max-keys", "1000", "--no-paginate"]
            if token is not None:
                arguments.extend(("--continuation-token", token))
            page = self._call("list-objects-v2", *arguments)
            contents = page.get("Contents", [])
            if not isinstance(contents, list) or type(page.get("IsTruncated")) is not bool:
                raise BackupError("S3 returned an invalid object listing")
            for item in contents:
                key = item.get("Key") if isinstance(item, dict) else None
                if not isinstance(key, str) or not key.startswith(prefix) or key in seen:
                    raise BackupError("S3 object listing is invalid, duplicated, or outside the instance prefix")
                if include_last_modified:
                    last_modified = item.get("LastModified")
                    try:
                        modified = datetime.fromisoformat(last_modified.replace("Z", "+00:00"))
                    except (AttributeError, TypeError, ValueError) as error:
                        raise BackupError("S3 object listing has invalid publication metadata") from error
                    if modified.tzinfo is None:
                        raise BackupError("S3 object listing has invalid publication metadata")
                    objects.append({
                        "key": key,
                        "last_modified": modified.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    })
                else:
                    objects.append(key)
                seen.add(key)
                if len(objects) > 10000:
                    raise BackupError("S3 object listing exceeds the safety limit")
            if not page["IsTruncated"]:
                return objects
            token = page.get("NextContinuationToken")
            if not isinstance(token, str) or not token or token in tokens:
                raise BackupError("S3 object listing is incomplete or repeated")
            tokens.add(token)
        raise BackupError("S3 object listing exceeds the page safety limit")

    def list_objects(self, prefix: str) -> list[dict[str, str]]:
        return self._list_objects(prefix, include_last_modified=True)

    def keys(self, prefix: str) -> list[str]:
        return self._list_objects(prefix, include_last_modified=False)

    def delete(self, key: str) -> None:
        self._call("delete-object", "--key", key)

    def list_versions(self, prefix: str) -> list[dict]:
        if not re.fullmatch(r'instances/[a-z][a-z0-9-]{0,31}/', prefix):
            raise BackupError('version inventory requires one named-instance prefix')
        entries, seen, cursors = [], set(), set()
        cursor = None
        for _ in range(1000):
            arguments = ['--prefix', prefix, '--max-keys', '1000', '--no-paginate']
            if cursor:
                arguments.extend(('--key-marker', cursor[0], '--version-id-marker', cursor[1]))
            page = self._call('list-object-versions', *arguments)
            if type(page.get('IsTruncated')) is not bool:
                raise BackupError('version inventory is incomplete or malformed')
            for field in ('Versions', 'DeleteMarkers'):
                values = page.get(field, [])
                if not isinstance(values, list):
                    raise BackupError('version inventory is malformed')
                for item in values:
                    if (not isinstance(item, dict) or not isinstance(item.get('Key'), str)
                            or not item['Key'].startswith(prefix) or type(item.get('IsLatest')) is not bool):
                        raise BackupError('version inventory is outside the named instance or malformed')
                    version = valid_version(item.get('VersionId'))
                    identity = (item['Key'], version)
                    if identity in seen:
                        raise BackupError('version inventory contains duplicates')
                    try:
                        modified = datetime.fromisoformat(item['LastModified'].replace('Z', '+00:00'))
                        if modified.tzinfo is None:
                            raise ValueError()
                    except (KeyError, TypeError, AttributeError, ValueError) as error:
                        raise BackupError('version inventory has invalid publication metadata') from error
                    seen.add(identity)
                    entries.append({'key': item['Key'], 'version_id': version, 'is_latest': item['IsLatest'],
                                    'delete_marker': field == 'DeleteMarkers',
                                    'last_modified': modified.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})
                    if len(entries) > 10000:
                        raise BackupError('version inventory exceeds its safety limit')
            if not page['IsTruncated']:
                return entries
            cursor = (page.get('NextKeyMarker'), page.get('NextVersionIdMarker'))
            if (not isinstance(cursor[0], str) or not cursor[0].startswith(prefix)
                    or not isinstance(cursor[1], str) or not cursor[1] or cursor in cursors):
                raise BackupError('version inventory pagination is incomplete or repeated')
            valid_version(cursor[1])
            cursors.add(cursor)
        raise BackupError('version inventory exceeds its page limit')

    def version_tags(self, key: str, version_id: str) -> dict[str, str]:
        valid_version(version_id)
        if not re.fullmatch(r'instances/[a-z][a-z0-9-]{0,31}/[^\x00\r\n]+', key):
            raise BackupError('version tags require a named-instance object')
        result = self._call('get-object-tagging', '--key', key, '--version-id', version_id)
        tags = result.get('TagSet')
        if result.get('VersionId') != version_id or not isinstance(tags, list) or len(tags) > 10:
            raise BackupError('S3 version tags are unavailable or malformed')
        values = {}
        for tag in tags:
            if (not isinstance(tag, dict) or set(tag) != {'Key', 'Value'}
                    or not isinstance(tag['Key'], str) or not isinstance(tag['Value'], str)
                    or tag['Key'] in values):
                raise BackupError('S3 version tags are malformed or duplicated')
            values[tag['Key']] = tag['Value']
        return values

    def set_retention(self, key: str, version_id: str, state: str) -> None:
        if state not in ('keep', 'retired'):
            raise BackupError('invalid recovery retention state')
        current = self.version_tags(key, version_id)
        if set(current) - {'minecraft-retention'} or current.get('minecraft-retention') not in (None, 'keep', 'retired'):
            raise BackupError('unexpected version tags require operator investigation')
        expected = {'minecraft-retention': state}
        if current != expected:
            tagging = json.dumps({'TagSet': [{'Key': 'minecraft-retention', 'Value': state}]})
            result = self._call('put-object-tagging', '--key', key, '--version-id', version_id, '--tagging', tagging)
            if result.get('VersionId') != version_id:
                raise BackupError('S3 did not confirm the selected tagged version')
        if self.version_tags(key, version_id) != expected:
            raise BackupError('S3 retention tag readback differs from the selected version')

    def _multipart_upload(self, key: str, source: Path) -> None:
        created = self._call("create-multipart-upload", "--key", key,
                             "--checksum-algorithm", "SHA256", "--checksum-type", "COMPOSITE")
        upload_id = created.get("UploadId")
        if not isinstance(upload_id, str) or not upload_id:
            raise BackupError("S3 did not identify the multipart upload")
        try:
            with tempfile.TemporaryDirectory(prefix=".minecraft-upload-", dir=source.parent) as directory:
                part_path = Path(directory) / "part"
                parts = []
                total = 0
                with source.open("rb") as handle:
                    while data := handle.read(PART_BYTES):
                        total += len(data)
                        if total > MAX_ARCHIVE_BYTES or len(parts) >= 10000:
                            raise BackupError("backup upload exceeds the archive size or part limit")
                        part_path.write_bytes(data)
                        checksum = base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")
                        number = len(parts) + 1
                        response = self._call("upload-part", "--key", key, "--upload-id", upload_id,
                                              "--part-number", str(number), "--body", str(part_path),
                                              "--checksum-sha256", checksum)
                        etag = response.get("ETag")
                        if not isinstance(etag, str) or not etag or response.get("ChecksumSHA256") != checksum:
                            raise BackupError("S3 did not verify an uploaded archive part")
                        parts.append({"PartNumber": number, "ETag": etag, "ChecksumSHA256": checksum})
                if not parts:
                    raise BackupError("backup upload source changed while reading")
                manifest = Path(directory) / "parts.json"
                manifest.write_text(json.dumps({"Parts": parts}), encoding="utf-8")
                try:
                    self._call("complete-multipart-upload", "--key", key, "--upload-id", upload_id,
                               "--multipart-upload", "file://" + str(manifest), "--if-none-match", "*")
                except _AWSError as error:
                    if error.code not in ("PreconditionFailed", "412"):
                        raise
                    self._call("abort-multipart-upload", "--key", key, "--upload-id", upload_id)
        except BaseException:
            try:
                self._call("abort-multipart-upload", "--key", key, "--upload-id", upload_id)
            except BackupError:
                pass
            raise
