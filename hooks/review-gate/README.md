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
- The Claude Code hook only sees Bash tool calls. It does not stop a person at a terminal or the web UI. The server lock covers those paths.
- The hook stops accidental skipped reviews. It is not a sandbox. The required server status check is the enforcement against deliberate evasion.
- The hook is a strict allow-list. A command with no merge word passes with no network call. The text gate decodes the command, removes quotes and backslashes, and matches `merge` as a whole word. So `'mer'ge` is seen, and field names such as `mergeable` or `mergeStateStatus` in `--json` and `--jq` values do not count. The GraphQL names `mergePullRequest` and `enablePullRequestAutoMerge` are always seen. It also inspects a `gh` command, or a `g` that bash could expand into `gh` (`g?`, `g*`, `g[h]`, `g{h,}`), when `$`, a backtick, or a glob character (`[ ] ? * { }`) sits in the group slot, the subcommand slot, or the `gh api` endpoint, and any `gh api` call with `graphql`, `--input`, or `@file`. Values of data flags such as `--jq`, `--json`, `--search`, and `--label` are data and do not trigger this.
- A command that mentions merge passes only if it is exactly one plain call: `gh pr merge <number or URL>` with an explicit `--repo OWNER/REPO` or a full pull request URL. The program must be `gh` by basename. No wrapper, env assignment, chaining, redirection, `$`, backtick, parenthesis, or glob character is allowed. Only `--squash`, `--merge`, `--rebase`, `--delete-branch`, `--match-head-commit`, `--repo`, `--subject`, and `--body` are allowed. `--auto` and `--admin` are blocked. The hook then requires `--match-head-commit` to equal the current head and a review record for that sha. It runs `gh` in the `cwd` from the hook payload.
- The only other commands that may mention merge are short chains of read-only programs. The program must be the bare name. Allowed: `git` with `merge`, `merge-base`, `log`, `status`, `diff`, `show`, or `rev-parse`, plus `grep`, `ls`, `cat`, `head`, `tail`, `wc`, `echo`, and `printf`. Git takes no global options. Every option must match a full name on a per-subcommand list, so an abbreviated option such as `--exe` is refused. Redirection operators, `$`, backticks, parentheses, and braces refuse the whole command. A glob character in a program name, a git subcommand, or an option refuses it too. Path arguments such as `*.py` may keep globs. A pipe, a semicolon, `&`, `&&`, `||`, and a newline join segments only when each segment passes.
- Everything else that mentions merge exits 2 with the plain form `gh pr merge <n> --repo OWNER/REPO --squash --match-head-commit <sha>`. This covers `gh api` calls, `gh alias`, `curl`, `env`, `sudo`, `xargs`, shells, `eval`, and `git rebase`, `fetch`, `checkout`, `branch`, and `cherry-pick`.
- Known limit: commands that build `gh` and the merge words from files in the working directory or at run time can still pass the hook. The server status check covers them.
- Known limit: a command that builds the merge at run time without the word, for example `$(printf ...)`, passes the hook. So does a pre-existing alias such as `gh m 5` and a script file that merges.
- Known false positives, with workarounds:
  - `git commit -m` with merge in the message. Use `git commit -F <file>`.
  - `gh pr view "$N"` or a `gh` command with `$`, a backtick, or a glob character in the group, subcommand, or endpoint slot. Use a literal number.
  - `gh pr create --title` with merge in the text. Avoid the word or use `--body-file` with a file.
  - `git push` of a branch named `merge-*`. Pick another branch name.
  - `npm run merge-*`. Avoid the word in the script name or run it from a script file.

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
