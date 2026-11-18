#!/usr/bin/env python3
"""review-gate core: decide whether a pull request head commit has a recorded review.

A review is recorded as a pull request comment with a line of this exact form:

    Review seat: APPROVE <40-character head sha>

The core is stdlib only. The CLI fetches data with the GitHub CLI (`gh api`).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

APPROVAL_LINE = re.compile(r"^Review seat: APPROVE ([0-9a-f]{40})\b")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
COMMENT_FORMAT = "Review seat: APPROVE <40-character head sha>"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2


@dataclass(frozen=True)
class Comment:
    author: str
    body: str
    created_at: str = ""


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reason: str
    head_sha: str = ""


class GateError(Exception):
    """Raised when data cannot be fetched or parsed. Callers must treat it as a failure."""


def approved_shas(body: str) -> list[str]:
    """Return every sha approved by a line in a comment body."""
    shas = []
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = APPROVAL_LINE.match(line)
        if match:
            shas.append(match.group(1))
    return shas


def evaluate(
    head_sha: str,
    comments: Iterable[Comment],
    allowed_authors: Sequence[str] = (),
) -> Verdict:
    """Pass only if an allowed comment approves exactly the current head sha."""
    head = head_sha.strip().lower()
    if not FULL_SHA.match(head):
        return Verdict(False, "head sha is not a 40-character hex string", head)
    allowed = {a.strip().lower() for a in allowed_authors if a.strip()}
    stale: set[str] = set()
    skipped_authors: set[str] = set()
    for comment in comments:
        shas = approved_shas(comment.body or "")
        if not shas:
            continue
        if allowed and comment.author.lower() not in allowed:
            skipped_authors.add(comment.author)
            continue
        if head in shas:
            return Verdict(True, f"approved by {comment.author} at {head[:12]}", head)
        stale.update(s[:12] for s in shas)
    if stale:
        return Verdict(
            False,
            f"no approval for head {head[:12]}, only for {', '.join(sorted(stale))}",
            head,
        )
    if skipped_authors:
        return Verdict(
            False,
            f"no approval for head {head[:12]} from an allowed author "
            f"(ignored: {', '.join(sorted(skipped_authors))})",
            head,
        )
    return Verdict(False, f"no review record for head {head[:12]}", head)


Runner = Callable[[Sequence[str]], str]


def run_gh(args: Sequence[str]) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(f"gh failed to run: {exc}") from exc
    if proc.returncode != 0:
        raise GateError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def parse_json_stream(text: str) -> list:
    """Parse the concatenated JSON arrays that `gh api --paginate` prints."""
    decoder = json.JSONDecoder()
    items: list = []
    index = 0
    text = text.strip()
    while index < len(text):
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError as exc:
            raise GateError(f"unreadable gh output: {exc}") from exc
        items.extend(value if isinstance(value, list) else [value])
        index = end
        while index < len(text) and text[index].isspace():
            index += 1
    return items


def fetch_head_sha(repo: str, pr: int, runner: Runner = run_gh) -> str:
    out = runner(["api", f"repos/{repo}/pulls/{pr}", "--jq", ".head.sha"])
    sha = out.strip().lower()
    if not FULL_SHA.match(sha):
        raise GateError(f"unexpected head sha from API: {sha[:60]!r}")
    return sha


def fetch_comments(repo: str, pr: int, runner: Runner = run_gh) -> list[Comment]:
    out = runner(
        ["api", "--paginate", f"repos/{repo}/issues/{pr}/comments?per_page=100"]
    )
    comments = []
    for item in parse_json_stream(out):
        if not isinstance(item, dict):
            raise GateError("unexpected comment shape from API")
        user = item.get("user") or {}
        comments.append(
            Comment(
                author=str(user.get("login", "")),
                body=str(item.get("body") or ""),
                created_at=str(item.get("created_at") or ""),
            )
        )
    return comments


def check_pull_request(
    repo: str,
    pr: int,
    allowed_authors: Sequence[str] = (),
    runner: Runner = run_gh,
) -> Verdict:
    head = fetch_head_sha(repo, pr, runner)
    return evaluate(head, fetch_comments(repo, pr, runner), allowed_authors)


def split_authors(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="review_gate.py")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="check one pull request")
    check.add_argument("--repo", required=True, help="OWNER/NAME")
    check.add_argument("--pr", required=True, type=int)
    check.add_argument(
        "--allowed-authors", default="", help="comma list of logins, empty means any"
    )
    check.add_argument("--json", action="store_true", help="print a JSON verdict")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_ERROR if exc.code not in (0, None) else EXIT_PASS
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        print("error: --repo must look like OWNER/NAME", file=sys.stderr)
        return EXIT_ERROR
    try:
        verdict = check_pull_request(
            args.repo, args.pr, split_authors(args.allowed_authors)
        )
    except GateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        print(
            json.dumps(
                {
                    "passed": verdict.passed,
                    "reason": verdict.reason,
                    "head_sha": verdict.head_sha,
                }
            )
        )
    else:
        print(("PASS: " if verdict.passed else "FAIL: ") + verdict.reason)
    return EXIT_PASS if verdict.passed else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
