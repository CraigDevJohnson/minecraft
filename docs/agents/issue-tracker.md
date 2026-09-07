# Issue tracker: GitHub

Issues and specs for this repo live in GitHub Issues for `CraigDevJohnson/minecraft`. Use the `gh` CLI, with `--repo CraigDevJohnson/minecraft` for issue and PR commands.

## Conventions

- Create an issue: `gh issue create --repo CraigDevJohnson/minecraft --title "..." --body-file <body-file>`.
- Read an issue, including labels and discussion: `gh issue view <number> --repo CraigDevJohnson/minecraft --json number,title,body,state,author,createdAt,updatedAt,labels,assignees,comments,url`. Preserve comment authors and timestamps when interpreting replies.
- List issues: `gh issue list --repo CraigDevJohnson/minecraft --state open --limit 200 --json number,title,body,state,author,createdAt,updatedAt,labels,assignees,comments,url`, with appropriate label and state filters. A full page is not proof of completeness; use paginated retrieval when needed and exclude PRs from REST issue results.
- Comment: `gh issue comment <number> --repo CraigDevJohnson/minecraft --body-file <body-file>`.
- Apply or remove labels: `gh issue edit <number> --repo CraigDevJohnson/minecraft --add-label "..."` or `--remove-label "..."`.
- Close: `gh issue close <number> --repo CraigDevJohnson/minecraft` after any needed explanation has been posted.

For multiline bodies, write the exact Markdown to a temporary file and pass it with `--body-file`.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares one number space across issues and PRs. Resolve an ambiguous reference with `gh pr view <number> --repo CraigDevJohnson/minecraft`, falling back to `gh issue view` only when it is not a PR; authentication or network errors do not establish its type.

## Skill vocabulary

- "Publish to the issue tracker" means create a GitHub issue.
- "Fetch the relevant ticket" means read the issue body, labels, state, and comments using the command above.

## Wayfinding operations

- A map is one issue labelled `wayfinder:map`, with Notes, Decisions-so-far, and Fog in its body.
- Child tickets are GitHub sub-issues of the map, labelled `wayfinder:<type>` where type is `research`, `prototype`, `grilling`, or `task`. If sub-issues are unavailable, use a task list in the map and a `Part of #<map>` line in each child.
- Use native GitHub issue dependencies for blockers. If unavailable, use a `Blocked by: #<number>` line in the child. A ticket is unblocked only after every blocker is closed.
- The frontier is the map's open children in map order, excluding assigned tickets and tickets with open blockers.
- Claim a ticket by assigning the driving developer; for the authenticated user, use `gh issue edit <number> --repo CraigDevJohnson/minecraft --add-assignee @me`.
- Resolve by posting the outcome, closing the ticket, and adding a concise result and link to the map's Decisions-so-far.
