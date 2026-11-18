#!/usr/bin/env python3
"""Claude Code PreToolUse hook for the Bash tool.

Strict allow-list. A command that mentions a merge passes only in one of two ways.

1. It is exactly one plain `gh pr merge` call with an explicit repository, only
   allow-listed flags, and `--match-head-commit <sha>`. The pull request must have a
   review record for that sha.
2. It is a short chain of read-only commands such as `git merge main` or
   `grep merge file`.

Every other command that mentions a merge exits 2. A command with no merge word
passes at once with no subprocess and no network.

Contract: the hook reads JSON on stdin with `tool_name`, `tool_input.command` and
optionally `cwd`. Exit code 0 allows the call. Exit code 2 blocks it and feeds
stderr to Claude.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass
from typing import Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import review_gate as core  # noqa: E402

GH_WORD = re.compile(r"(?<![\w-])gh(?![\w-])")
GH_API_RISK = re.compile(r"(?<![\w-])gh(?![\w-])[^;&|\n]*\bapi\b[^;&|\n]*(--input|@|graphql)")
FORBIDDEN_CHARS = set(";&|\n\r$`<>(){}[]?*")
BOOLEAN_FLAGS = {"--squash", "--merge", "--rebase", "--delete-branch", "-d"}
VALUE_FLAGS = {
    "--match-head-commit", "-R", "--repo", "--subject", "-t", "--body", "-b",
}
REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
PR_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)$")
FULL_SHA = core.FULL_SHA
PLAIN_FORM = "gh pr merge <n> --repo OWNER/REPO --squash --match-head-commit <sha>"

GLOB_CHARS = set("[]?*{}")
# Read-only exception path. Deny by default: every option must match a full name below.
GIT_OPTIONS = {
    "merge": {"--no-ff", "--ff-only", "--no-edit", "--abort", "--continue", "--squash"},
    "merge-base": {"--is-ancestor", "--all"},
    "log": {"--oneline", "--merges", "--no-merges", "--graph", "--stat", "--decorate",
            "--all", "--first-parent"},
    "status": {"-s", "--short", "--porcelain", "-b", "--branch"},
    "diff": {"--stat", "--name-only", "--name-status", "--cached", "--no-ext-diff"},
    "show": {"--stat", "--name-only", "--name-status", "--cached", "--no-ext-diff"},
    "rev-parse": {"--abbrev-ref", "--short", "--verify"},
}
GREP_CLUSTER = re.compile(r"^-[rRniLlEFwcvHhosq]+$")
GREP_LONG = re.compile(r"^--(include|exclude|exclude-dir)=[^\s]+$|^--color=never$")
LS_CLUSTER = re.compile(r"^-[lahR1tSF]+$")
WC_CLUSTER = re.compile(r"^-[lwcm]+$")
COUNT = re.compile(r"^\d+$")
LINE_COUNT = re.compile(r"^-\d+$")
GH_MERGE_TEXT = re.compile(r"(?<![\w-])gh(?![\w-])[\s\S]*merge", re.IGNORECASE)


class NotPlain(Exception):
    """The command is not the one plain merge form."""


@dataclass(frozen=True)
class MergeInvocation:
    selector: str
    repo: Optional[str]
    match_head: Optional[str]


DATA_FLAGS = {
    "--jq", "-q", "--json", "--template", "-t", "--search", "-S", "--label", "--limit", "-L",
    "--repo", "-R", "--method", "-X", "--header", "-H", "--field", "-F", "--raw-field", "-f",
    "--input", "--hostname", "--body", "-b", "--title", "--subject", "--base", "-B",
    "--head", "--author", "--assignee", "--state", "--match-head-commit",
}
# Values of these flags are data. They are ignored by the merge-word check.
MERGE_SKIP_FLAGS = {"--json", "--jq", "-q", "--template", "--search", "-S", "--label"}
SLOT_RISK_CHARS = GLOB_CHARS | {"$", "`"}
MERGE_WORD = re.compile(
    r"(?<![a-z0-9])merge(?![a-z0-9])|mergepullrequest|enablepullrequestautomerge"
)
# A `g` that bash may expand into `gh`: g?, g*, g[h], g{h,}.
GH_LIKE = re.compile(r"(?<![\w-])g[?*\[{]")


def _shlex_segments(command: str) -> list[list[str]]:
    """Split on shlex tokens, so a pipe inside a quoted value is not a separator."""
    segments: list[list[str]] = []
    for line in command.replace("\r", "\n").split("\n"):
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        segment: list[str] = []
        for token in lexer:
            if set(token) <= set("&|;()<>"):
                segments.append(segment)
                segment = []
            else:
                segment.append(token)
        segments.append(segment)
    return segments


def _is_gh_token(token: str) -> bool:
    name = os.path.basename(token.lstrip("\\`"))
    if name == "gh":
        return True
    return name.startswith("g") and len(name) <= 8 and any(c in GLOB_CHARS for c in name)


def _gh_slot_risk(command: str) -> bool:
    """True when a gh command slot (group, subcommand, api endpoint) holds an expansion.

    Values of known data flags such as --jq are skipped.
    """
    try:
        for seg in _shlex_segments(command):
            for at, token in enumerate(seg):
                if not _is_gh_token(token):
                    continue
                positionals: list[str] = []
                index = at + 1
                while index < len(seg) and len(positionals) < 2:
                    arg = seg[index]
                    if arg.startswith("-"):
                        index += 2 if arg in DATA_FLAGS else 1
                        continue
                    positionals.append(arg)
                    index += 1
                if any(c in SLOT_RISK_CHARS for p in positionals for c in p):
                    return True
    except ValueError:
        return True
    return False


def _scrubbed_text(command: str) -> str:
    """Decoded command text with the values of data flags removed after gh."""
    parts: list[str] = []
    for seg in _shlex_segments(command):
        kept: list[str] = []
        seen_gh = False
        index = 0
        while index < len(seg):
            token = seg[index]
            if seen_gh and token in MERGE_SKIP_FLAGS:
                index += 2
                continue
            if seen_gh and token.partition("=")[0] in MERGE_SKIP_FLAGS and "=" in token:
                index += 1
                continue
            seen_gh = seen_gh or _is_gh_token(token)
            kept.append(token)
            index += 1
        parts.append(" ".join(kept))
    return "\n".join(parts)


def needs_inspection(command: str) -> bool:
    """Cheap text gate. Quotes and backslashes are removed so split words are seen."""
    lowered = command.lower()
    plain = re.sub(r"[\\'\"]", "", lowered)
    try:
        text = re.sub(r"[\\'\"]", "", _scrubbed_text(command).lower())
    except ValueError:
        text = plain
    if MERGE_WORD.search(text):
        return True
    if GH_WORD.search(plain) or GH_LIKE.search(command):
        if _gh_slot_risk(command) or GH_API_RISK.search(plain):
            return True
    return False


def parse_plain_merge(command: str) -> MergeInvocation:
    """Return the invocation when the whole command is one plain merge, else raise."""
    if any(char in FORBIDDEN_CHARS for char in command):
        raise NotPlain("The command chains, redirects, or expands text.")
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError as exc:
        raise NotPlain("The quoting is unbalanced.") from exc
    if not tokens or os.path.basename(tokens[0]) != "gh":
        raise NotPlain("The command is not a single `gh pr merge` call.")
    positionals: list[str] = []
    repos: list[str] = []
    heads: list[str] = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            name, eq, inline = token.partition("=")
            if name in BOOLEAN_FLAGS and not eq:
                index += 1
                continue
            if name in VALUE_FLAGS:
                if eq:
                    value, step = inline, 1
                elif index + 1 < len(tokens):
                    value, step = tokens[index + 1], 2
                else:
                    raise NotPlain(f"`{name}` has no value.")
                if name in ("-R", "--repo"):
                    repos.append(value)
                elif name == "--match-head-commit":
                    heads.append(value)
                index += step
                continue
            raise NotPlain(f"The flag `{token}` is not allowed.")
        positionals.append(token)
        index += 1
    if len(positionals) != 3 or positionals[:2] != ["pr", "merge"]:
        raise NotPlain("The command is not `gh pr merge <number or URL>`.")
    if len(repos) > 1 or len(heads) > 1:
        raise NotPlain("A flag is given twice.")
    selector = positionals[2]
    repo = repos[0] if repos else None
    if repo is not None and not REPO_NAME.match(repo):
        raise NotPlain("`--repo` must look like OWNER/REPO.")
    url = PR_URL.match(selector)
    if url:
        if repo and repo.lower() != url.group(1).lower():
            raise NotPlain("`--repo` and the pull request URL name different repositories.")
        repo = None
    elif selector.isdigit():
        if not repo:
            raise NotPlain("`--repo OWNER/REPO` or a full pull request URL is required.")
    else:
        raise NotPlain("The selector must be a number or a full pull request URL.")
    return MergeInvocation(selector, repo, heads[0] if heads else None)


def _git_ok(args: list[str]) -> bool:
    if not args or args[0] not in GIT_OPTIONS:
        return False
    allowed = GIT_OPTIONS[args[0]]
    rest = args[1:]
    index = 0
    while index < len(rest):
        arg = rest[index]
        if arg == "-n" and args[0] == "log":
            if index + 1 >= len(rest) or not COUNT.match(rest[index + 1]):
                return False
            index += 2
            continue
        if args[0] == "log" and LINE_COUNT.match(arg):
            index += 1
            continue
        if arg.startswith("-") and arg not in allowed:
            return False
        index += 1
    return True


def _paths_ok(args: list[str]) -> bool:
    return not any(a.startswith("-") for a in args)


def _count_ok(args: list[str], clusters: Optional[re.Pattern] = None) -> bool:
    """Allow `-n N`, `-N`, and optional letter clusters. Everything else must be a path."""
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-n":
            if index + 1 >= len(args) or not COUNT.match(args[index + 1]):
                return False
            index += 2
            continue
        if LINE_COUNT.match(arg) or (clusters is not None and clusters.match(arg)):
            index += 1
            continue
        if arg.startswith("-"):
            return False
        index += 1
    return True


def _read_only_segment(tokens: list[str]) -> bool:
    program = tokens[0]
    args = tokens[1:]
    # Bash expands globs after parsing. Refuse them where they could build an option,
    # a program, or a git subcommand. Path arguments may keep globs.
    slots = [program] + ([args[0]] if program == "git" and args else [])
    if any(c in GLOB_CHARS for slot in slots for c in slot):
        return False
    if any(a.startswith("-") and any(c in GLOB_CHARS for c in a) for a in args):
        return False
    if program == "git":
        return _git_ok(args)
    if program == "grep":
        return all(
            not a.startswith("-") or GREP_CLUSTER.match(a) or GREP_LONG.match(a) for a in args
        )
    if program == "ls":
        return all(not a.startswith("-") or LS_CLUSTER.match(a) for a in args)
    if program == "cat":
        return _paths_ok(args)
    if program in ("head", "tail"):
        return _count_ok(args)
    if program == "wc":
        return _count_ok(args, WC_CLUSTER)
    if program in ("echo", "printf"):
        return True
    return False


def _stripped(text: str) -> str:
    return re.sub(r"[\\'\"]", "", text)


def is_read_only_chain(command: str) -> bool:
    """True when every segment is a small read-only command and nothing expands.

    Redirection, substitution, and subshell characters refuse the whole command.
    """
    if any(char in command for char in "$`<>(){}"):
        return False
    if GH_MERGE_TEXT.search(_stripped(command)):
        return False
    segments: list[list[str]] = []
    try:
        for line in command.replace("\r", "\n").split("\n"):
            lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            lexer.commenters = ""
            current: list[str] = []
            for token in lexer:
                if set(token) <= set("&|;"):
                    if current:
                        segments.append(current)
                    current = []
                elif set(token) <= set("&|;()<>"):
                    return False
                else:
                    current.append(token)
            if current:
                segments.append(current)
    except ValueError:
        return False
    # Programs are matched by exact name. A path or wrapper never qualifies.
    return bool(segments) and all(_read_only_segment(s) for s in segments)


def _plain_message(reason: str) -> str:
    return (
        f"Merge blocked by review-gate: {reason} "
        "This command mentions merge and is not the plain form. "
        f"Use exactly: {PLAIN_FORM}. "
        "For other commands that mention merge (for example a commit message), "
        "write the text to a file and pass the file (git commit -F <file>)."
    )


def _block_message(problem: str) -> str:
    return (
        f"Merge blocked by review-gate: {problem} "
        "Run `gh pr merge <number> --repo OWNER/REPO --match-head-commit <full head sha>` "
        f"only after a pull request comment contains a line exactly like "
        f"`{core.COMMENT_FORMAT}` for the current head commit. Get the head with "
        "`gh pr view <number> --repo OWNER/REPO --json headRefOid -q .headRefOid`. "
        "A new push changes the head and voids older approvals."
    )


def _resolve(invocation: MergeInvocation, runner: core.Runner) -> tuple[str, int, str]:
    args = ["pr", "view", invocation.selector]
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
    runner: Optional[core.Runner] = None,
    allowed_authors: Sequence[str] = (),
    cwd: Optional[str] = None,
) -> Optional[str]:
    """Return None to allow or a block message. Fails closed."""
    if not needs_inspection(command):
        return None
    try:
        invocation = parse_plain_merge(command)
    except NotPlain as exc:
        if is_read_only_chain(command):
            return None
        return _plain_message(str(exc))
    if runner is None:
        runner = functools.partial(
            core.run_gh, cwd=cwd if cwd and os.path.isdir(cwd) else None
        )
    try:
        if not invocation.match_head:
            return _block_message("`--match-head-commit <sha>` is missing.")
        if not FULL_SHA.match(invocation.match_head.lower()):
            return _block_message("`--match-head-commit` must be the full 40-character sha.")
        repo, number, head = _resolve(invocation, runner)
        if invocation.match_head.lower() != head:
            return _block_message(
                f"`--match-head-commit` is {invocation.match_head[:12]} but PR "
                f"#{number} head is {head[:12]}."
            )
        verdict = core.check_pull_request(repo, number, allowed_authors, runner)
        if not verdict.passed:
            return _block_message(f"PR #{number} has no valid review record ({verdict.reason}).")
    except Exception as exc:  # noqa: BLE001 - fail closed on any error
        return _block_message(f"the check could not complete ({exc}).")
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        command = payload["tool_input"]["command"]
        if payload.get("tool_name", "Bash") != "Bash" or not isinstance(command, str):
            return 0
        cwd = payload.get("cwd")
        cwd = cwd if isinstance(cwd, str) else None
    except Exception:  # noqa: BLE001 - not a Bash command call, nothing to inspect
        return 0
    allowed = core.split_authors(os.environ.get("REVIEW_GATE_ALLOWED_AUTHORS", ""))
    message = decide(command, allowed_authors=allowed, cwd=cwd)
    if message:
        print(message, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
