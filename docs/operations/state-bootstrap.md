# State bootstrap contract

The current foundation validates OpenTofu locally without an AWS identity or remote backend. Authenticated bootstrap creation and migration are later operator actions that require a reviewed implementation and Craig's approval.

## Private inputs

Before bootstrap work, record these outside Git for the selected AWS target:

- AWS account ID and authorized operator role
- environment name and region (initially `us-west-2`)
- globally unique state bucket name and state object key
- state encryption key identifier
- locking configuration and recovery contacts

Do not place account IDs, role identifiers, bucket names, credentials, state, or generated plans in tracked files. Safe example files may document field names with non-live values.

## Reviewed creation and migration

The infrastructure issue must define a small, separately reviewable bootstrap configuration for a private bucket with encryption, versioning, public-access blocking, least-privilege access, and state locking. Review its concrete plan before creating anything. Record the resulting resource identifiers in private operator configuration.

After the backend exists, initialize an isolated working copy against the reviewed target, migrate the bootstrap state into the versioned and locked backend, and verify that the remote state can be read under the intended operator role. Preserve a protected encrypted recovery copy until the remote state and locking behavior have been verified. None of those authenticated creation or migration commands is implemented by this foundation.

## Bootstrap-state recovery

Keep the bucket's version history and the protected recovery copy independent of a workstation. Recovery starts in an isolated directory: restore the last known-good bootstrap state or import the existing bootstrap resources, compare their real encryption, versioning, access, and locking settings with the reviewed configuration, and only then reconnect or migrate to the backend. Never overwrite the active state object while another operation may hold the lock.

Repository validation remains the first check after a recovery, but it does not prove AWS access, backend health, resource creation, or recoverability. Those claims require authenticated operator checks in the later infrastructure and recovery issues.
