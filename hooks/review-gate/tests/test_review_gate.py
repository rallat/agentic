import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import review_gate as rg  # noqa: E402

HEAD = "a" * 40
OLD = "b" * 40


def c(body, author="alice"):
    return rg.Comment(author=author, body=body, created_at="2026-01-01T00:00:00Z")


def test_pass_on_exact_head():
    v = rg.evaluate(HEAD, [c(f"Review seat: APPROVE {HEAD}\nlooks good")])
    assert v.passed


def test_fail_on_wrong_sha():
    v = rg.evaluate(HEAD, [c(f"Review seat: APPROVE {'c' * 40}")])
    assert not v.passed and "only for" in v.reason


def test_stale_after_new_push():
    comments = [c(f"Review seat: APPROVE {OLD}")]
    assert rg.evaluate(OLD, comments).passed
    assert not rg.evaluate(HEAD, comments).passed


def test_no_comments():
    assert not rg.evaluate(HEAD, []).passed


def test_malformed_lines():
    bad = [
        f"review seat: APPROVE {HEAD}",
        f"Review seat: APPROVE {HEAD[:39]}",
        f"Review seat: APPROVE {HEAD}a",
        f"Review seat: APPROVE {HEAD.upper()}",
        f" Review seat: APPROVE {HEAD}",
        f"Review seat: REJECT {HEAD}",
        f"Review seat:  APPROVE {HEAD}",
    ]
    for body in bad:
        assert not rg.evaluate(HEAD, [c(body)]).passed, body


def test_trailing_text_and_crlf():
    assert rg.evaluate(HEAD, [c(f"intro\r\nReview seat: APPROVE {HEAD} (ok)\r\n")]).passed


def test_allowed_authors_filter():
    comments = [c(f"Review seat: APPROVE {HEAD}", author="mallory")]
    assert rg.evaluate(HEAD, comments).passed
    assert not rg.evaluate(HEAD, comments, ["alice"]).passed
    assert rg.evaluate(HEAD, comments, ["MALLORY"]).passed


def test_multiple_comments_any_valid_passes():
    comments = [
        c("hello"),
        c(f"Review seat: APPROVE {OLD}"),
        c(f"Review seat: APPROVE {HEAD}", author="bob"),
    ]
    assert rg.evaluate(HEAD, comments, ["bob"]).passed


def test_invalid_head_fails():
    assert not rg.evaluate("abc", [c("x")]).passed


def test_parse_paginated_stream():
    text = json.dumps([{"a": 1}]) + "\n" + json.dumps([{"a": 2}])
    assert rg.parse_json_stream(text) == [{"a": 1}, {"a": 2}]


def fake_runner(head, comments):
    def run(args):
        if args[1].endswith(f"pulls/7"):
            return head + "\n"
        return json.dumps(comments)

    return run


def test_check_pull_request_with_mock_gh():
    comments = [{"user": {"login": "alice"}, "body": f"Review seat: APPROVE {HEAD}"}]
    v = rg.check_pull_request("o/r", 7, [], fake_runner(HEAD, comments))
    assert v.passed


def test_cli_exit_codes(monkeypatch, capsys):
    comments = [{"user": {"login": "alice"}, "body": f"Review seat: APPROVE {HEAD}"}]
    monkeypatch.setattr(rg, "run_gh", fake_runner(HEAD, comments))
    monkeypatch.setattr(
        rg, "check_pull_request",
        lambda repo, pr, allowed, runner=None: rg.evaluate(
            HEAD, rg.fetch_comments(repo, pr, fake_runner(HEAD, comments)), allowed
        ),
    )
    assert rg.main(["check", "--repo", "o/r", "--pr", "7"]) == 0
    monkeypatch.setattr(
        rg, "check_pull_request", lambda *a, **k: rg.Verdict(False, "nope", HEAD)
    )
    assert rg.main(["check", "--repo", "o/r", "--pr", "7"]) == 1

    def boom(*a, **k):
        raise rg.GateError("api down")

    monkeypatch.setattr(rg, "check_pull_request", boom)
    assert rg.main(["check", "--repo", "o/r", "--pr", "7"]) == 2
    assert rg.main(["check", "--repo", "bad", "--pr", "7"]) == 2
    assert rg.main(["check"]) == 2
