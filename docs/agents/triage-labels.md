# Triage labels

The five canonical triage roles map directly to these GitHub labels.

| Canonical role | GitHub label | Meaning |
| --- | --- | --- |
| `needs-triage` | `needs-triage` | Maintainer needs to evaluate this issue. |
| `needs-info` | `needs-info` | Waiting on the reporter for more information. |
| `ready-for-agent` | `ready-for-agent` | Fully specified and ready for autonomous agent implementation. |
| `ready-for-human` | `ready-for-human` | Requires human implementation. |
| `wontfix` | `wontfix` | Will not be actioned. |

When a skill mentions a triage role, use the corresponding label string from this table. The `bug` and `enhancement` category labels and supplemental labels such as `documentation` remain separate from these five roles.

Edit the GitHub label column if the repository's vocabulary changes.

## Priority labels

Priority describes delivery impact; it does not replace a canonical triage role.

| Label | Meaning |
| --- | --- |
| `priority:high` | Required to deliver or safely operate the first family server. |
| `priority:medium` | Important follow-up that does not block the first safe delivery. |
| `priority:low` | Optional improvement that can wait without affecting the first delivery. |

Reassess readiness whenever acceptance criteria, dependencies, or required human actions change. Keep exactly one canonical triage role aligned with the current next action: move blocked or underspecified work out of `ready-for-agent`, and return it only after its information and dependencies are resolved. Priority may remain unchanged while readiness changes.
