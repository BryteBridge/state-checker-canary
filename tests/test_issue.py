from __future__ import annotations

import io
import json
import urllib.error

import pytest

from canary import states
from canary.issue import GitHub, GitHubError, marker, sync, sync_outage

AZ = states.get("az")


class FakeGitHubAPI:
    """Records requests; answers from a small in-memory issue list."""

    def __init__(self, issues=None, fail=None):
        self.issues = issues or []
        self.requests: list[tuple[str, str, dict | None]] = []
        self.fail = fail

    def __call__(self, req, timeout):
        body = json.loads(req.data) if req.data else None
        path = req.full_url.split("/repos/o/r", 1)[1]
        self.requests.append((req.get_method(), path, body))
        assert req.headers["Authorization"] == "Bearer tok"
        if self.fail and self.fail in path:
            raise urllib.error.HTTPError(req.full_url, 403, "no", {}, io.BytesIO(b"forbidden"))
        if req.get_method() == "GET":
            return Resp(200, self.issues)
        if path == "/labels":
            return Resp(422, {"message": "exists"}) if body["name"] == "canary" else Resp(201, {})
        if path == "/issues":
            return Resp(201, {"number": 7})
        return Resp(200, {})


class Resp:
    def __init__(self, status, payload):
        self.status, self._raw = status, json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def gh(api):
    return GitHub("o/r", "tok", api="https://api.invalid", opener=api)


def test_failure_with_no_issue_opens_one_with_both_labels_assigned_to_the_owner():
    api = FakeGitHubAPI()
    assert sync(gh(api), AZ, failed=True, body="B", run_url="U", assignee="me") == "failing; opened #7"
    method, path, body = api.requests[-1]
    assert (method, path) == ("POST", "/issues")
    assert body["labels"] == ["canary", "canary-az"] and body["title"].startswith("AZ (Arizona)")
    assert body["assignees"] == ["me"]
    assert api.requests[0][1].startswith("/issues?") and "labels=canary-az" in api.requests[0][1]


def _marked(sig, runs=3, since="2026-11-10 06:00 UTC"):
    return f'old text\n<!-- canary-state: {{"since":"{since}","runs":{runs},"sig":"{sig}"}} -->\n'


def test_a_new_issue_carries_the_marker():
    api = FakeGitHubAPI()
    sync(gh(api), AZ, failed=True, body="B", run_url="U", sig="aaaaaaaaaaaa", when="2026-11-16 06:00 UTC")
    text = api.requests[-1][2]["body"]
    assert "B" in text and marker(text) == {"since": "2026-11-16 06:00 UTC", "runs": 1, "sig": "aaaaaaaaaaaa"}
    assert "Failing since 2026-11-16 06:00 UTC, 1 batch in a row" in text


def test_the_same_failure_again_rewrites_the_issue_without_a_comment():
    api = FakeGitHubAPI(issues=[{"number": 12, "body": _marked("aaaaaaaaaaaa")}, {"number": 3, "pull_request": {}}])
    did = sync(gh(api), AZ, failed=True, body="B2", run_url="U", sig="aaaaaaaaaaaa", when="2026-11-16 09:00 UTC")
    assert did == "failing; updated #12 (no comment: same failure)"
    assert not any(path.endswith("/comments") for _, path, _ in api.requests)
    method, path, body = api.requests[-1]
    assert (method, path) == ("PATCH", "/issues/12") and "B2" in body["body"]
    assert marker(body["body"]) == {"since": "2026-11-10 06:00 UTC", "runs": 4, "sig": "aaaaaaaaaaaa"}


def test_a_changed_failure_comments_and_rewrites():
    api = FakeGitHubAPI(issues=[{"number": 12, "body": _marked("aaaaaaaaaaaa")}])
    did = sync(gh(api), AZ, failed=True, body="B", run_url="U", sig="bbbbbbbbbbbb", when="2026-11-16 09:00 UTC")
    assert did == "failing; commented on #12 (the failure changed)"
    assert api.requests[-2] == ("POST", "/issues/12/comments", {"body": "**The failure changed.**\n\nB"})
    assert marker(api.requests[-1][2]["body"])["sig"] == "bbbbbbbbbbbb"


def test_an_issue_from_before_the_marker_comments_once_and_starts_counting():
    api = FakeGitHubAPI(issues=[{"number": 12, "body": "an old issue"}])
    sync(gh(api), AZ, failed=True, body="B", run_url="U", sig="aaaaaaaaaaaa", when="2026-11-16 09:00 UTC")
    assert api.requests[-2][1] == "/issues/12/comments"
    assert marker(api.requests[-1][2]["body"]) == {"since": "2026-11-16 09:00 UTC", "runs": 1, "sig": "aaaaaaaaaaaa"}


GOOD = '{"since":"2026-11-10 06:00 UTC","runs":3,"sig":"aaaaaaaaaaaa"}'


@pytest.mark.parametrize(
    "body",
    [
        "",
        "no marker",
        "<!-- canary-state: {not json} -->",
        "<!-- canary-state: [1] -->",
        '<!-- canary-state: {"a":' * 5000 + "1" + "}" * 5000 + " -->",  # deep nesting
        f"<!-- canary-state: {GOOD} -->\n<!-- canary-state: {GOOD} -->\n",  # two markers
        f"<!-- canary-state: {GOOD} -->\nmore text after it",  # not at the end
        '<!-- canary-state: {"since":"**bold** [x](y)","runs":3,"sig":"aaaaaaaaaaaa"} -->',
        '<!-- canary-state: {"since":"2026-11-10 06:00 UTC","runs":true,"sig":"aaaaaaaaaaaa"} -->',
        '<!-- canary-state: {"since":"2026-11-10 06:00 UTC","runs":3,"sig":"not hex"} -->',
        "<!-- canary-state: {" + '"x":1,' * 100 + '"since":"2026-11-10 06:00 UTC"} -->',  # too long
    ],
)
def test_an_unreadable_or_forged_marker_reads_as_none(body):
    assert marker(body) == {}


def test_a_well_formed_marker_at_the_end_reads():
    assert marker(f"text\n<!-- canary-state: {GOOD} -->\n") == {
        "since": "2026-11-10 06:00 UTC",
        "runs": 3,
        "sig": "aaaaaaaaaaaa",
    }


def test_an_outage_opens_one_issue():
    api = FakeGitHubAPI()
    did = sync_outage(
        gh(api), active=True, title="T", body="B", run_url="U", assignee="me", sig="S", when="2026-11-16 09:00 UTC"
    )
    assert did == "outage; opened #7"
    method, path, body = api.requests[-1]
    assert (method, path) == ("POST", "/issues") and body["labels"] == ["canary", "canary-outage"]
    assert body["title"] == "T" and body["assignees"] == ["me"]


def test_an_ongoing_outage_of_the_same_kind_does_not_comment():
    api = FakeGitHubAPI(issues=[{"number": 9, "body": _marked("aaaaaaaaaaaa")}])
    did = sync_outage(
        gh(api), active=True, title="T2", body="B", run_url="U", sig="aaaaaaaaaaaa", when="2026-11-16 09:00 UTC"
    )
    assert did == "outage; failing; updated #9 (no comment: same failure)"
    assert not any(path.endswith("/comments") for _, path, _ in api.requests)
    assert ("PATCH", "/issues/9", {"title": "T2"}) in api.requests


def test_an_outage_whose_kind_changes_comments():
    api = FakeGitHubAPI(issues=[{"number": 9, "body": _marked("aaaaaaaaaaaa")}])
    did = sync_outage(
        gh(api), active=True, title="T", body="B", run_url="U", sig="bbbbbbbbbbbb", when="2026-11-16 09:00 UTC"
    )
    assert did == "outage; failing; commented on #9 (the failure changed)"


def test_the_outage_issue_closes_when_it_is_over():
    api = FakeGitHubAPI(issues=[{"number": 9, "body": ""}])
    assert sync_outage(gh(api), active=False, title="T", body="B", run_url="U") == "no outage; closed #9"
    assert api.requests[-1] == ("PATCH", "/issues/9", {"state": "closed", "state_reason": "completed"})
    assert "labels=canary-outage" in api.requests[0][1]


def test_pass_closes_the_open_issue():
    api = FakeGitHubAPI(issues=[{"number": 12}])
    assert sync(gh(api), AZ, failed=False, body="B", run_url="U") == "passing; closed #12"
    assert api.requests[-2] == ("POST", "/issues/12/comments", {"body": "Passing again: U"})
    assert api.requests[-1] == ("PATCH", "/issues/12", {"state": "closed", "state_reason": "completed"})


def test_pass_with_no_issue_does_nothing_else():
    api = FakeGitHubAPI()
    assert sync(gh(api), AZ, failed=False, body="B", run_url="U") == "passing; no issue open"
    assert len(api.requests) == 1


def test_github_errors_raise_so_the_step_fails():
    with pytest.raises(GitHubError, match="HTTP 403"):
        sync(gh(FakeGitHubAPI(fail="/issues")), AZ, failed=True, body="B", run_url="U")


def test_missing_token_or_repo_exits():
    with pytest.raises(SystemExit):
        GitHub("o/r", "")
    with pytest.raises(SystemExit):
        GitHub("", "tok")
