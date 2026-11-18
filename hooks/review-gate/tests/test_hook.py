import json
import os
import subprocess
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "hooks"))
sys.path.insert(0, os.path.join(HERE, ".."))

import claude_code_pre_merge as hook  # noqa: E402
import review_gate as rg  # noqa: E402

HEAD = "a" * 40
OTHER = "b" * 40


def make_runner(head=HEAD, approved=HEAD, repo="o/r", fail=False):
    calls = []

    def run(args):
        calls.append(list(args))
        if fail:
            raise rg.GateError("api down")
        if args[0] == "pr":
            return json.dumps(
                {"number": 5, "headRefOid": head, "url": f"https://github.com/{repo}/pull/5"}
            )
        if args[1].startswith(f"repos/{repo}/pulls/"):
            return head + "\n"
        body = f"Review seat: APPROVE {approved}" if approved else "hi"
        return json.dumps([{"user": {"login": "alice"}, "body": body}])

    run.calls = calls
    return run


def test_non_merge_passes_without_network():
    runner = make_runner(fail=True)
    for cmd in ["ls -la", "gh pr view 5", "git commit -m 'x'", "gh pr list"]:
        assert hook.decide(cmd, runner) is None
    assert runner.calls == []


def test_quoted_echo_is_not_a_merge():
    assert hook.find_merge_invocations('echo "gh pr merge 5"') == []


def test_merge_without_match_head_blocks():
    msg = hook.decide("gh pr merge 5 --squash", make_runner())
    assert msg and "--match-head-commit" in msg and "Review seat: APPROVE" in msg


def test_mismatched_sha_blocks():
    msg = hook.decide(f"gh pr merge 5 --match-head-commit {OTHER}", make_runner())
    assert msg and "head is" in msg


def test_abbreviated_sha_blocks():
    assert hook.decide("gh pr merge 5 --match-head-commit abc1234", make_runner())


def test_missing_review_blocks():
    msg = hook.decide(f"gh pr merge 5 --match-head-commit {HEAD}", make_runner(approved=None))
    assert msg and "no review record" in msg


def test_stale_review_blocks():
    assert hook.decide(f"gh pr merge 5 --match-head-commit {HEAD}", make_runner(approved=OTHER))


def test_valid_merge_passes():
    assert hook.decide(f"gh pr merge 5 --squash --match-head-commit {HEAD}", make_runner()) is None
    assert hook.decide(f"gh pr merge 5 --match-head-commit={HEAD}", make_runner()) is None


def test_repo_flag_parsed_and_used():
    cmd = f"gh pr merge 5 --repo acme/widgets --match-head-commit {HEAD}"
    inv = hook.find_merge_invocations(cmd)[0]
    assert inv.repo == "acme/widgets" and inv.selector == "5"
    runner = make_runner(repo="acme/widgets")
    assert hook.decide(cmd, runner) is None
    assert ["pr", "view", "5", "--repo", "acme/widgets", "--json", "number,headRefOid,url"] in runner.calls
    inv = hook.find_merge_invocations("gh pr merge -R acme/widgets 9")[0]
    assert inv.repo == "acme/widgets" and inv.selector == "9"


def test_chained_commands():
    cmd = f"cd x && gh pr merge 5 --squash --match-head-commit {HEAD}"
    assert hook.decide(cmd, make_runner()) is None
    assert hook.decide("cd x && gh pr merge 5 --squash", make_runner())
    assert hook.decide(f"git push; gh pr merge 5 --match-head-commit {OTHER}", make_runner())


def test_body_value_not_taken_as_selector():
    inv = hook.find_merge_invocations('gh pr merge --body "fix it" 12 --squash')[0]
    assert inv.selector == "12"


def test_shell_wrapper_is_inspected():
    assert hook.decide("bash -c 'gh pr merge 5 --squash'", make_runner())


def test_fail_closed_on_api_error():
    msg = hook.decide(f"gh pr merge 5 --match-head-commit {HEAD}", make_runner(fail=True))
    assert msg and "could not complete" in msg


def test_unbalanced_quotes_fail_closed():
    assert hook.decide("gh pr merge 5 --body 'oops", make_runner())


def run_hook(payload):
    return subprocess.run(
        [sys.executable, os.path.join(HERE, "..", "hooks", "claude_code_pre_merge.py")],
        input=json.dumps(payload), capture_output=True, text=True,
    )


def test_stdin_contract_non_merge_exit_zero():
    proc = run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert proc.returncode == 0 and proc.stderr == ""


def test_stdin_contract_other_tool_and_garbage():
    assert run_hook({"tool_name": "Read", "tool_input": {"file_path": "x"}}).returncode == 0
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, "..", "hooks", "claude_code_pre_merge.py")],
        input="not json", capture_output=True, text=True,
    )
    assert proc.returncode == 0


def test_stdin_contract_merge_without_flag_exit_two():
    proc = run_hook({"tool_name": "Bash", "tool_input": {"command": "gh pr merge 5"}})
    assert proc.returncode == 2 and "Review seat: APPROVE" in proc.stderr
