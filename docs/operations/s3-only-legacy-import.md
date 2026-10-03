# Import existing recovery points before S3-only activation

The existing family bucket contains completed points created before exact-version receipts. They must join the protected retention inventory before phase-two activation. Do not fabricate a host, deployment or bootstrap receipt for those points. The owner import records the original marker/archive versions and independently verified source volume/filesystem, content/definition and creation/publication evidence.

With the data disk still retained, save a private, reviewed inventory of the exact legacy marker and archive versions. The envelope is a mode-0600 JSON file with `schema_version: 1`, `instance`, `bucket`, and `versions`, where each entry contains only `key` and `version_id`. The instance and bucket must match the private deployment. Each point must have one reviewed completed-marker version and one matching archive version. Unknown, duplicate, conflicting, missing, corrupt or orphan versions require investigation; import does not retire or tag them.

The command binds the actual private file bytes to its reviewed SHA-256 and verifies the active account before reading the bucket. Use the approved temporary operator credentials for the shell. It does not create a workstation profile or change AWS resources:

```sh
python3 scripts/minecraft_retention.py import-legacy \
  --deployment operator-config/family/deployment.json \
  --journal-dir operator-config/family/session-journal \
  --reviewed-inventory operator-config/family/reviewed-legacy-versions.json \
  --inventory-sha256 REVIEWED_INVENTORY_SHA256 survival
```

The journal directory must already be operator-owned mode 0700. Import validates the recorded metadata and exact versions, downloads the complete archive, and verifies every file through the existing safe extractor. Only after the entire reviewed list passes does it save mode-0600 `legacy-<point>.json` records. These contain no invented host or bootstrap identity. An interrupted local publication can be retried with the same list; conflicting existing records are preserved and block replacement.

The controller calls `reconcile_retention(store, instance, journal_dir, current_selection=..., active_receipt_ids=...)` under its full workstation operation lock. It takes the latest 14 distinct complete points across imported legacy records and new receipts, ordered by verified S3 marker publication time. It adds the authoritative current point and active journal references. Every kept exact version is independently verified and tagged `minecraft-retention=keep` with readback. Unclassified versions block retirement. Missing required points never cause catalog fallback or a blank-world start.

The private retirement plan records exact keep and obsolete version identities before tagging. A separate bounded point ledger saves verified retirement intent before tags can trigger irreversible lifecycle expiry. Retry revalidates the current selection and keep set; a private journal cannot authorize a kept, unsafe or currently unclassified version. Already-absent retired versions are reconciled using a complete version listing and their known object ownership. The helper never permanently deletes a version. Retirement moves only verified obsolete versions to `retired`; the mode-specific lifecycle performs asynchronous cleanup.

`verify_stop_receipt(..., protect=True, timeout=...)` protects and reads back the selected marker, archive, immutable receipt and current selection before returning. It uses a fresh bounded S3 operation per instance. Inject `S3Store(..., environment_provider=the_reviewed_operator_environment)` when the controller assumes its temporary operator role; cloning an operation preserves that provider. Runtime `minecraft-backup prune` refuses S3-only mode. Existing retained-disk modes preserve their current pruning workflow.

These helpers provide credential-free boundary coverage, including 14 legacy points followed by a new session, interrupted retirement and preservation on invalid input. Effective role/boundary permissions, lifecycle readback, the data-volume ownership migration, live S3-only round trip and startup timing remain activation requirements. No live changes are demonstrated by unit tests.
