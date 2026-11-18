# review-gate

review-gate blocks a pull request from merging unless a review is recorded for its exact head commit.

It has two locks. A GitHub Action sets a required commit status on the server. A Claude Code hook stops an agent from running `gh pr merge` without that record. The core is one stdlib-only Python file.

## The review record

A reviewer posts a pull request comment that contains this line:

```
Review seat: APPROVE <40-character head sha>
```

The line must start the line. The sha must be the full lowercase 40 characters and must equal the current head of the pull request. A new push changes the head, so older approvals stop counting. Text after the sha is allowed.

## Threat model and limits

review-gate stops a skipped review. It does not stop collusion.

- Anyone who can comment can write the line. With no author filter, a same-account agent can approve its own work. Set `allowed-authors` to limit who counts.
- The check reads the current comment text. A commenter can edit or delete their own comment, and the next run reflects that.
- The status is only a lock if the repository requires it. Without the ruleset below, the status is advisory.
- Repository admins can bypass rulesets unless the ruleset forbids it.
- The Claude Code hook only sees Bash tool calls. It does not stop a person at a terminal, the web UI, or `gh api` calls that call the merge endpoint directly. The server lock covers those paths.
- The hook parses shell text. Unusual quoting or generated command strings can hide a merge. The hook blocks unbalanced quoting and inspects `bash -c` strings, but it cannot see everything.

## Install the Action

Add a workflow that runs on `pull_request_target` and `issue_comment`. Never check out the pull request head in this workflow. Pin the action to a full commit sha of this repository.

```yaml
name: review-gate
on:
  pull_request_target:
    types: [opened, synchronize, reopened, ready_for_review]
  issue_comment:
    types: [created, edited, deleted]
permissions:
  contents: read
  statuses: write
  pull-requests: read
  issues: read
jobs:
  gate:
    if: github.event_name != 'issue_comment' || github.event.issue.pull_request
    runs-on: ubuntu-24.04
    steps:
      - uses: rallat/agentic/hooks/review-gate@<full commit sha>
        with:
          pr-number: ${{ github.event.pull_request.number || github.event.issue.number }}
          allowed-authors: ""
```

Inputs:

| Input | Default | Meaning |
| --- | --- | --- |
| `allowed-authors` | empty | Comma list of logins whose approvals count. Empty means any commenter. |
| `status-context` | `review-gate` | Name of the commit status. |
| `pr-number` | none | Pull request number. Required. |

If the check cannot read the pull request, the job fails and no status is set. A required status that is never set blocks the merge.

## Require the status

Create a ruleset that requires the `review-gate` status on the default branch. Replace `OWNER/REPO`.

```sh
gh api repos/OWNER/REPO/rulesets --method POST --input - <<'JSON'
{
  "name": "require-review-gate",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["~DEFAULT_BRANCH"], "exclude": [] } },
  "rules": [
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [ { "context": "review-gate" } ]
      }
    }
  ]
}
JSON
```

## Install the Claude Code hook

Copy `review_gate.py` and the `hooks` folder to `tools/review-gate/` in your repository. Keep the `hooks` folder next to `review_gate.py`. Add this to `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/tools/review-gate/hooks/claude_code_pre_merge.py\""
          }
        ]
      }
    ]
  }
}
```

The hook reads the hook JSON from stdin. A command without `gh pr merge` exits 0 at once and makes no network call. A merge command must include `--match-head-commit <full sha>`, the sha must equal the current head, and the pull request must have a valid record. Otherwise the hook exits 2 and prints the reason to stderr, which Claude Code shows to the agent. Any API error on a merge command also blocks.

Set `REVIEW_GATE_ALLOWED_AUTHORS` to a comma list of logins to filter approvals in the hook.

## CLI

```sh
python3 review_gate.py check --repo OWNER/REPO --pr 123
```

Exit code 0 means pass. Exit code 1 means fail. Exit code 2 means a usage or API error. The CLI needs the `gh` command, signed in. Add `--json` for machine output.

## Tests

```sh
python3 -m pytest tests -q
```

The tests mock `gh` and use no network.
