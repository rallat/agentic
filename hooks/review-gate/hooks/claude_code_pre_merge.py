#!/usr/bin/env python3
"""Claude Code PreToolUse hook for the Bash tool.

Blocks `gh pr merge` unless the command pins the head commit with
`--match-head-commit <sha>` and the pull request has a review record for that sha.
Any other command passes at once without network access.

Contract: the hook reads JSON on stdin with `tool_name` and `tool_input.command`.
Exit code 0 allows the call. Exit code 2 blocks it and feeds stderr to Claude.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import review_gate as core  # noqa: E402

MERGE_WORDS = ("gh", "pr", "merge")
MERGE_HINT = re.compile(r"\bgh\s+pr\s+merge\b")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
FLAGS_WITH_VALUE = {
    "-R", "--repo", "--match-head-commit", "-b", "--body", "-F", "--body-file",
    "-t", "--subject", "-A", "--author-email",
}
FULL_SHA = core.FULL_SHA


@dataclass(frozen=True)
class MergeInvocation:
    selector: Optional[str]
    repo: Optional[str]
    match_head: Optional[str]


def _segments(command: str) -> list[list[str]]:
    """Split a command line into simple commands at shell operators."""
    segments: list[list[str]] = []
    for line in command.replace("\r", "\n").split("\n"):
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        current: list[str] = []
        for token in lexer:
            if set(token) <= set("&|;()<>") and token:
                if current:
                    segments.append(current)
                current = []
            else:
                current.append(token)
        if current:
            segments.append(current)
    return segments


def _parse_merge(args: Sequence[str]) -> MergeInvocation:
    selector = repo = match_head = None
    index = 0
    while index < len(args):
        arg = args[index]
        name, eq, inline = arg.partition("=")
        if arg.startswith("-") and name in FLAGS_WITH_VALUE:
            if eq:
                value = inline
                index += 1
            else:
                value = args[index + 1] if index + 1 < len(args) else None
                index += 2
            if name in ("-R", "--repo"):
                repo = value
            elif name == "--match-head-commit":
                match_head = value
            continue
        if not arg.startswith("-") and selector is None:
            selector = arg
        index += 1
    return MergeInvocation(selector, repo, match_head)


def find_merge_invocations(command: str, depth: int = 0) -> list[MergeInvocation]:
    """Return every `gh pr merge` invocation found in a shell command line."""
    found: list[MergeInvocation] = []
    if depth > 3:
        return found
    try:
        segments = _segments(command)
    except ValueError:
        # Unbalanced quotes. Fail closed when the text mentions a merge.
        return [MergeInvocation(None, None, None)] if MERGE_HINT.search(command) else []
    for segment in segments:
        for start in range(len(segment) - 2):
            if tuple(segment[start : start + 3]) == MERGE_WORDS:
                found.append(_parse_merge(segment[start + 3 :]))
                break
        else:
            program = os.path.basename(segment[0])
            if program in SHELLS or program == "eval":
                for token in segment[1:]:
                    if MERGE_HINT.search(token):
                        found.extend(find_merge_invocations(token, depth + 1))
    return found


def _block_message(problem: str) -> str:
    return (
        f"Merge blocked by review-gate: {problem} "
        "Run `gh pr merge <number> --match-head-commit <full head sha>` only after a "
        f"pull request comment contains a line exactly like `{core.COMMENT_FORMAT}` "
        "for the current head commit. Get the head with "
        "`gh pr view <number> --json headRefOid -q .headRefOid`. "
        "A new push changes the head and voids older approvals."
    )


def _resolve(invocation: MergeInvocation, runner: core.Runner) -> tuple[str, int, str]:
    args = ["pr", "view"]
    if invocation.selector:
        args.append(invocation.selector)
    if invocation.repo:
        args += ["--repo", invocation.repo]
    args += ["--json", "number,headRefOid,url"]
    data = json.loads(runner(args))
    match = re.match(r"https://[^/]+/([^/]+/[^/]+)/pull/(\d+)", data["url"])
    if not match:
        raise core.GateError("could not read the repository from the pull request URL")
    return match.group(1), int(data["number"]), str(data["headRefOid"]).lower()


def decide(
    command: str,
    runner: core.Runner = core.run_gh,
    allowed_authors: Sequence[str] = (),
) -> Optional[str]:
    """Return None to allow or a block message. Fails closed for merge commands."""
    if not MERGE_HINT.search(command):
        return None
    try:
        for invocation in find_merge_invocations(command):
            if not invocation.match_head:
                return _block_message("`--match-head-commit <sha>` is missing.")
            if not FULL_SHA.match(invocation.match_head.lower()):
                return _block_message(
                    "`--match-head-commit` must be the full 40-character sha."
                )
            repo, number, head = _resolve(invocation, runner)
            if invocation.match_head.lower() != head:
                return _block_message(
                    f"`--match-head-commit` is {invocation.match_head[:12]} but PR "
                    f"#{number} head is {head[:12]}."
                )
            verdict = core.check_pull_request(repo, number, allowed_authors, runner)
            if not verdict.passed:
                return _block_message(
                    f"PR #{number} has no valid review record ({verdict.reason})."
                )
    except Exception as exc:  # noqa: BLE001 - fail closed on any error
        return _block_message(f"the check could not complete ({exc}).")
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        command = payload["tool_input"]["command"]
        if payload.get("tool_name", "Bash") != "Bash" or not isinstance(command, str):
            return 0
    except Exception:  # noqa: BLE001 - not a Bash command call, nothing to inspect
        return 0
    allowed = core.split_authors(os.environ.get("REVIEW_GATE_ALLOWED_AUTHORS", ""))
    message = decide(command, allowed_authors=allowed)
    if message:
        print(message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
