# Domain docs

This repository uses a single-context layout.

## Before exploring

Read root `CONTEXT.md` and relevant ADRs in `docs/adr/` when present. If a root `CONTEXT-MAP.md` is introduced later, follow it to the relevant context documents and context-scoped ADRs.

If these files do not exist, proceed silently. Do not treat their absence as a blocker or suggest creating them upfront. The `domain-modeling` skill creates domain documents lazily as terminology and decisions are resolved.

## File structure

```text
CONTEXT.md
docs/adr/
```

## Use the glossary's vocabulary

Use terms defined in `CONTEXT.md` when naming domain concepts in issues, proposals, hypotheses, and tests. Avoid synonyms the glossary explicitly rejects. If a needed concept is missing, reconsider invented terminology or note the real gap for `domain-modeling`.

## Flag ADR conflicts

Explicitly identify any existing ADR your proposal contradicts and explain why reopening that decision may be warranted. Do not silently override recorded decisions.
