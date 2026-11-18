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
PLAIN_FORM = "gh pr merge <n> --repo OWNER/REPO --squash --match-head-commit <sha>"


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


def good(n=5, extra="", sha=HEAD):
    return f"gh pr merge {n} --repo o/r --squash --match-head-commit {sha}{extra}"


def blocked(cmd, runner=None):
    return hook.decide(cmd, runner or make_runner(approved=None))


def test_ordinary_commands_pass_without_runner():
    runner = make_runner(fail=True)
    for cmd in [
        "ls", "ls -la", "gh pr view 5", "gh pr list", "git commit -F msg.txt",
        "git status", "gh api repos/o/r/pulls/5", "gh api repos/o/r/pulls/5/comments",
    ]:
        assert hook.decide(cmd, runner) is None, cmd
    assert runner.calls == []


def test_safe_readonly_commands_that_mention_merge_pass_without_runner():
    runner = make_runner(fail=True)
    for cmd in [
        "git merge main", "git log --merges --oneline", "git log | grep merge",
        "grep -rn merge .", "git status && git merge-base main HEAD",
        "git diff --stat | grep merge", "echo merge", "ls | wc -l; git merge main",
        "git merge --no-ff feature", "git log -n 5 --merges", "git log -5",
        "grep -rn merge *.py", "cat notes.txt | grep -i merge",
        "ls -la && git status -s", "git show --stat HEAD", "git rev-parse --abbrev-ref HEAD",
    ]:
        assert hook.decide(cmd, runner) is None, cmd
    assert runner.calls == []


def test_commit_message_with_merge_blocks_with_file_hint():
    msg = blocked('git commit -m "fix merge"')
    assert msg and "git commit -F <file>" in msg
    assert PLAIN_FORM in msg
    assert "This command mentions merge and is not the plain form." in msg


def test_valid_merge_passes_and_uses_explicit_repo():
    runner = make_runner()
    assert hook.decide(good(), runner) is None
    assert ["pr", "view", "5", "--repo", "o/r", "--json", "number,headRefOid,url"] in runner.calls
    assert hook.decide(f"gh -R o/r pr merge 5 --match-head-commit {HEAD}", make_runner()) is None
    assert hook.decide(f"gh pr --repo=o/r merge 5 --match-head-commit={HEAD}", make_runner()) is None
    assert hook.decide(
        f"/opt/homebrew/bin/gh pr merge 5 -R o/r -d --rebase --match-head-commit {HEAD}",
        make_runner(),
    ) is None
    url = f"gh pr merge https://github.com/o/r/pull/5 --match-head-commit {HEAD}"
    assert hook.decide(url, make_runner()) is None
    assert hook.decide(good(extra=' --subject "t" --body "b"'), make_runner()) is None


def test_repo_or_url_is_required():
    msg = hook.decide(f"gh pr merge 5 --squash --match-head-commit {HEAD}", make_runner())
    assert msg and "--repo" in msg


def test_url_and_repo_must_agree():
    cmd = f"gh pr merge https://github.com/a/b/pull/5 --repo o/r --match-head-commit {HEAD}"
    assert hook.decide(cmd, make_runner())


def test_merge_without_match_head_blocks():
    msg = hook.decide("gh pr merge 5 --repo o/r --squash", make_runner())
    assert msg and "--match-head-commit" in msg and "Review seat: APPROVE" in msg


def test_mismatched_sha_blocks():
    msg = hook.decide(good(sha=OTHER), make_runner())
    assert msg and "head is" in msg


def test_abbreviated_sha_blocks():
    assert hook.decide("gh pr merge 5 --repo o/r --match-head-commit abc1234", make_runner())


def test_missing_review_blocks():
    msg = hook.decide(good(), make_runner(approved=None))
    assert msg and "no review record" in msg


def test_stale_review_blocks():
    assert hook.decide(good(), make_runner(approved=OTHER))


def test_fail_closed_on_api_error():
    msg = hook.decide(good(), make_runner(fail=True))
    assert msg and "could not complete" in msg


def test_admin_auto_and_unknown_flags_block():
    for extra in [" --admin", " --auto", " --body-file x", " --unknown"]:
        assert hook.decide(good(extra=extra), make_runner()), extra


def test_finding_1_quoted_and_escaped_words_are_seen():
    for cmd in [
        "gh pr 'mer'ge 5 --repo o/r --squash",
        "gh pr mer\\ge 5 --repo o/r --squash",
        'gh pr "me"rge 5 --repo o/r --squash',
        "gh pr $(echo mer)ge 5 --repo o/r --squash",
        "g\\h pr merge 5 --repo o/r --squash",
    ]:
        assert blocked(cmd), cmd


def test_finding_2_other_programs_carrying_merge_fail_closed():
    for cmd in [
        "curl -X PUT https://api.github.com/repos/o/r/pulls/5/merge",
        "find . -exec gh pr merge 5 ;",
        "ssh host gh pr merge 5",
        "watch gh pr merge 5",
        "timeout 5 gh pr merge 5",
        "nice gh pr merge 5",
        "git rebase -x 'gh pr merge 5' main",
        "awk 'BEGIN{system(\"gh pr merge 5\")}'",
        "alias gm='gh pr merge'",
        "gh alias set m 'pr merge'",
    ]:
        assert blocked(cmd), cmd


def test_finding_3_env_and_sudo_options_block_even_with_valid_record():
    for cmd in [
        f"env -S 'gh pr merge 5 --repo o/r --match-head-commit {HEAD}'",
        f"env --split-string='gh pr merge 5 --repo o/r' --match-head-commit {HEAD}",
        f"env -C /tmp gh pr merge 5 --repo o/r --match-head-commit {HEAD}",
        f"sudo -D /tmp gh pr merge 5 --repo o/r --match-head-commit {HEAD}",
        f"env GH_REPO=a/b gh pr merge 5 --repo o/r --match-head-commit {HEAD}",
        f"command gh pr merge 5 --repo o/r --match-head-commit {HEAD}",
        f"xargs gh pr merge --repo o/r --match-head-commit {HEAD}",
    ]:
        assert hook.decide(cmd, make_runner()), cmd


def test_finding_4_substitution_in_tokens_and_flag_values_blocks():
    for cmd in [
        good(extra=' --body "$(gh pr merge 6 --repo o/r)"'),
        good(extra=" --body '`gh pr merge 6`'"),
        good(extra=' --subject "x$(id)"'),
        f"gh pr merge 5 --repo $(echo o/r) --match-head-commit {HEAD}",
    ]:
        assert hook.decide(cmd, make_runner()), cmd


def test_finding_5_api_merge_graphql_files_and_expansions_block():
    for cmd in [
        "gh api -X PUT repos/o/r/pulls/5/merge",
        "gh api --method PUT repos/o/r/pulls/5/merge",
        "gh api -XPUT repos/o/r/pulls/5/merge",
        "gh api repos/$REPO/pulls/5/merge",
        "gh api graphql --input q.json",
        "gh api graphql -f query=@q.graphql",
        "gh api graphql -f query='mutation { mergePullRequest(input: {}) { x } }'",
        "gh api graphql -f query='mutation { enablePullRequestAutoMerge(input: {}) { x } }'",
    ]:
        assert blocked(cmd), cmd


def test_finding_6_gh_runs_in_payload_cwd(monkeypatch, tmp_path):
    seen = []

    def fake(args, cwd=None):
        seen.append(cwd)
        return make_runner()(args)

    monkeypatch.setattr(hook.core, "run_gh", fake)
    assert hook.decide(good(), cwd=str(tmp_path)) is None
    assert seen and set(seen) == {str(tmp_path)}


def test_chaining_and_redirection_block():
    for cmd in [
        f"cd x && {good()}",
        f"{good()} && echo done",
        f"{good()}; ls",
        f"{good()} | cat",
        f"{good()} > out.txt",
        f"{good()} &",
        f"git status\n{good()}",
        f"GH_REPO=a/b {good()}",
        f"({good()})",
        f"pushd ../x && {good()}",
    ]:
        assert hook.decide(cmd, make_runner()), cmd


def test_unbalanced_quotes_fail_closed():
    assert hook.decide("gh pr merge 5 --repo o/r --body 'oops", make_runner())


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


def test_stdin_contract_probes_exit_two():
    for cmd in [
        "gh pr merge 5",
        "/opt/homebrew/bin/gh pr merge 5 --squash",
        "gh -R o/r pr merge 5 --squash",
        "gh pr --repo o/r merge 5",
        "gh api -X PUT repos/o/r/pulls/5/merge",
        "cd ../other && gh pr merge 5 --squash",
        'git commit -m "fix merge"',
    ]:
        proc = run_hook({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": "/"})
        assert proc.returncode == 2, cmd


def test_review_git_option_abbreviations_and_unlisted_subcommands_block():
    for cmd in [
        "git rebase --exe \"gh pr m'e'rge 5 --repo o/r --squash\" HEAD~1",
        "git rebase --ex \"gh pr m'e'rge 5 --repo o/r --squash\" HEAD~1",
        "git rebase --exec \"gh pr merge 5\" HEAD~1",
        "git rebase -x merge HEAD~1",
        "git fetch --upload-p \"gh pr m'e'rge 5 --repo o/r\" origin",
        "git fetch --upload-pack=x origin merge",
        "git -c alias.x='!gh pr merge 5' x",
        "git -C ../other merge main",
        "git --git-dir=x merge main",
        "git --exec-path=x merge main",
        "git cherry-pick merge",
        "git checkout merge",
        "git branch merge-x",
        "git worktree add merge-x",
        "git switch merge",
        "git log --merge --pretty=x",
        "git log --format=%H --merge",
        "git log --output=out.txt --merge",
        "git log --merge",
        "git diff --ext-diff merge",
        "git show --ext HEAD merge",
        "git merge --st main",
        "git merge -- main",
    ]:
        assert blocked(cmd), cmd


def test_review_dropped_programs_and_redirection_block():
    for cmd in [
        "rg merge src", "rg --pre ./x merge src", "sed -n 1p merge.txt",
        "awk '{print}' merge.txt", "python3 -m pytest -k merge",
        "grep -f patterns merge file", "grep -e merge file", "grep merge file > out.txt",
        "grep merge file < in.txt", "git log --merge >> out.txt",
        "cat <<EOF merge", "cat <(echo merge)", "echo merge >(cat)",
        "git log --merge &> out", "tail -f merge.log", "head -n x merge.txt",
    ]:
        assert blocked(cmd), cmd


def test_review_chain_separators_need_every_segment_allowed():
    runner = make_runner(fail=True)
    assert hook.decide("git log --merges && ls; git status", runner) is None
    assert hook.decide("git log --merges | wc -l", runner) is None
    for cmd in [
        "git log --merges && rm -rf merge",
        "git log --merges; make merge",
        "git log --merges | xargs echo merge",
        "git log --merges || curl merge",
        "ls &\ngit log --merges\nrm merge",
    ]:
        assert hook.decide(cmd, runner), cmd


def test_review_split_words_and_globs_are_seen():
    for cmd in [
        "gh pr m'e'rge 5 --repo o/r --squash",
        "gh pr m\\erge 5 --repo o/r --squash",
        "gh pr [m]erge 5 --repo o/r --squash",
        "gh pr m?rge 5 --repo o/r --squash",
        "gh pr mer* 5 --repo o/r --squash",
        "gh pr m{e,}rge 5 --repo o/r --squash",
        "gh pr merge 5 --repo o/r --body 'a?' --match-head-commit " + HEAD,
        "git log --merges | gh pr m'e'rge 5",
        "echo gh pr m'e'rge 5",
    ]:
        assert blocked(cmd), cmd


def test_review_valid_plain_merge_still_passes():
    assert hook.decide(good(), make_runner()) is None
    assert hook.decide("gh pr view 5", make_runner(fail=True)) is None


def test_data_flag_values_do_not_trigger_the_gate():
    runner = make_runner(fail=True)
    for cmd in [
        "gh pr checks 500 --json name,state --jq '.[] | .name'",
        "gh pr list --json number --jq '.[].number'",
        "gh api repos/o/r/pulls --jq '.[0].number'",
        "gh pr list --search 'is:open *x*' --label 'a?b'",
        "gh pr view 5 --json files -q '.files[] | select(.path == \"x\")'",
        "gh pr checks 5 --jq '.[] | select(.name == $n)'",
    ]:
        assert hook.decide(cmd, runner) is None, cmd
    assert runner.calls == []


def test_glob_or_expansion_in_command_slots_blocks():
    for cmd in [
        "gh pr m*e 5 --repo o/r",
        "gh p[r] merge 5",
        "gh pr [m]erge 5 --repo o/r",
        "gh pr m?rge 5 --repo o/r",
        "gh pr m{e,}rge 5 --repo o/r",
        "gh pr $(echo mer)ge 5 --repo o/r",
        "gh pr `echo mer`ge 5 --repo o/r",
        "gh $(echo pr) m 5",
        "gh api repos/o/r/pulls/5/me[r]ge",
        "gh api repos/o/r/pulls/5/m*rge",
        "xargs gh p[r] m*e 5",
        "gh pr ${X}ge 5",
    ]:
        assert blocked(cmd), cmd


def test_merge_field_names_in_data_flags_pass_without_runner():
    runner = make_runner(fail=True)
    for cmd in [
        "gh pr view 497 --json mergeable,mergeStateStatus",
        "gh pr view 497 --json mergeCommit",
        "gh pr view 497 --jq .mergeStateStatus",
        "gh pr view 497 --json autoMergeRequest",
        "gh pr view 497 --json number -q .mergeStateStatus",
        "gh pr view 497 --json=mergeable",
        "gh pr list --search 'merge in:title' --label merge",
        "echo mergeable",
        "git log --merges",
    ]:
        assert hook.decide(cmd, runner) is None, cmd
    assert runner.calls == []


def test_merge_word_still_blocks_next_to_data_flags():
    for cmd in [
        "gh pr merge 5 --repo o/r --squash",
        "gh pr view 5 --json number && gh pr merge 5 --repo o/r",
        "gh pr --json x merge 5 --repo o/r",
        "gh pr view 5 --json mergeable ; gh pr m'e'rge 5 --repo o/r",
        "gh pr view 5 --jq .merge_method && git merge-base a b",
    ][:4]:
        assert blocked(cmd), cmd
    assert "--match-head-commit" in blocked("gh pr merge 5 --repo o/r --squash")


def test_read_only_chain_refuses_braces_and_option_globs():
    runner = make_runner(fail=True)
    for cmd in [
        "git log --merge {--exe,x}",
        "git log --merge --{exe,x}",
        "git log --merge --o?tput=x",
        "git log --merge --exe*",
        "git log --merge --[e]xec",
        "git lo? --merge",
        "g[i]t merge main",
        "grep -rn merge {a,b}",
        "grep -rn merge . --include=*.py",
        "ls {a,b} | grep merge",
    ]:
        assert hook.decide(cmd, runner), cmd


def test_read_only_chain_allows_glob_path_arguments():
    runner = make_runner(fail=True)
    for cmd in ["grep -rn merge *.py", "grep -n merge docs/?.md", "cat notes/[a-c]*.txt | grep merge"]:
        assert hook.decide(cmd, runner) is None, cmd


def test_gh_hidden_behind_glob_quote_or_backslash_blocks():
    for cmd in [
        "/opt/*/bin/g? pr m?rge 5",
        "g* pr m?rge 5 --repo o/r",
        "g[h] pr [m]erge 5 --repo o/r",
        "g{h,} pr m{e,}rge 5 --repo o/r",
        "g'h' pr m'erge' 5",
        "g\\h pr m\\erge 5",
        "/opt/homebrew/*/g? pr m*e 5",
    ]:
        assert blocked(cmd), cmd
