"""One GitHub issue per state, kept in step with its checks, and one for an outage.

A failing check opens the state's issue. While it stays open, a later failure
comments on it only when the failure changed (another kind of problem); the
same failure again rewrites the issue's text in place, with how long it has
been failing, which notifies nobody. The first passing check closes it. The
issue carries the labels `canary` and `canary-<code>`, which is how it is found
again; a hidden marker at the end of its text keeps when it started failing,
how many batches in a row, and the failure's signature.

When half or more of the served states (six at least) fail in one batch of all of
them, they likely share a cause (the proxies, the checker's server, R2): one
outage issue (`canary-outage`) says so, and the failing states' own issues are
left as they are. Issue texts are trusted as far as the repository's writers are.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from .states import State

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repo: str, token: str, *, api: str = API, opener: Callable = urllib.request.urlopen):
        if not repo or "/" not in repo:
            raise SystemExit("GITHUB_REPOSITORY must be owner/name")
        if not token:
            raise SystemExit("GH_TOKEN is not set: the issue step needs the workflow's token")
        self.repo, self.token, self.api, self._open = repo, token, api, opener

    def call(self, method: str, path: str, payload: dict | None = None, ok: tuple[int, ...] = (200, 201)):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"{self.api}/repos/{self.repo}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "state-checker-canary",
            },
        )
        try:
            with self._open(req, timeout=30) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GitHubError(f"{method} {path}: no answer from GitHub ({e})") from e
        if status not in ok:
            raise GitHubError(f"{method} {path}: HTTP {status}: {raw[:300].decode('utf-8', 'replace')}")
        return status, (json.loads(raw) if raw else None)

    def ensure_label(self, name: str, color: str, description: str) -> None:
        # 422: the label exists already
        self.call("POST", "/labels", {"name": name, "color": color, "description": description}, ok=(201, 422))

    def open_issue(self, label: str) -> dict | None:
        """The oldest open issue with the label: {"number", "body"}, or None."""
        query = urllib.parse.urlencode({"state": "open", "labels": label, "per_page": 20})
        _, issues = self.call("GET", f"/issues?{query}")
        found = sorted((i for i in issues if "pull_request" not in i), key=lambda i: i["number"])
        return {"number": found[0]["number"], "body": found[0].get("body") or ""} if found else None


GROUP = "canary"
OUTAGE = "canary-outage"
# The marker: one flat JSON object, short, at the very end of the text. Anything else
# (nested, long, repeated, not last) reads as no marker, and the issue starts counting again.
_MARK = re.compile(r"<!-- canary-state: (\{[^{}<>]{1,300}\}) -->\s*\Z")
_SINCE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
_SIG = re.compile(r"[0-9a-f]{12}")


def labels(state: State) -> tuple[str, str]:
    return GROUP, f"canary-{state.code}"


def marker(body: str) -> dict:
    """What the hidden marker at the end of an issue's text says: since, runs, sig.
    Empty when there is none (an issue opened before the marker existed) or it is unreadable."""
    body = body or ""
    found = _MARK.search(body)
    if found is None or body.count("<!-- canary-state:") != 1:
        return {}
    try:
        data = json.loads(found.group(1))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    since, runs, sig = data.get("since"), data.get("runs"), data.get("sig")
    if not (isinstance(since, str) and _SINCE.fullmatch(since)):
        return {}
    if not (isinstance(runs, int) and not isinstance(runs, bool) and 0 < runs < 100_000):
        return {}
    if not (isinstance(sig, str) and _SIG.fullmatch(sig)):
        return {}
    return {"since": since, "runs": runs, "sig": sig}


def _text(body: str, *, since: str, runs: int, sig: str) -> str:
    head = (
        f"**Failing since {since}, {runs} batch{'es' if runs != 1 else ''} in a row.** The detail below is the "
        "latest batch's. A comment is added only when the failure changes, so this issue stays quiet while "
        "the same failure repeats.\n\n"
    )
    mark = json.dumps({"since": since, "runs": runs, "sig": sig}, separators=(",", ":"))
    return f"{head}{body}\n<!-- canary-state: {mark} -->\n"


def _keep_open(gh: GitHub, found: dict, *, body: str, sig: str, when: str, comment_on_change: bool) -> str:
    """Update an open issue for one more failing batch. Returns what it did."""
    number, old = found["number"], marker(found["body"])
    runs = old.get("runs")
    runs = runs + 1 if isinstance(runs, int) and not isinstance(runs, bool) and runs > 0 else 1
    since = old.get("since") if isinstance(old.get("since"), str) else when
    changed = old.get("sig") != sig
    if changed and comment_on_change:
        gh.call("POST", f"/issues/{number}/comments", {"body": f"**The failure changed.**\n\n{body}"})
    gh.call("PATCH", f"/issues/{number}", {"body": _text(body, since=since, runs=runs, sig=sig)})
    if changed and comment_on_change:
        return f"failing; commented on #{number} (the failure changed)"
    return f"failing; updated #{number} (no comment: {'same failure' if not changed else 'quiet issue'})"


def _close(gh: GitHub, found: dict | None, text: str) -> str:
    if found is None:
        return "no issue open"
    number = found["number"]
    gh.call("POST", f"/issues/{number}/comments", {"body": text})
    gh.call("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"})
    return f"closed #{number}"


def _open(gh: GitHub, *, title: str, body: str, label_list: list[str], assignee: str | None, sig: str, when: str):
    """The new issue's number. An assignee GitHub refuses (an organization, say, once the
    repository moved to one: HTTP 422 on `assignees`) never costs the alert: the issue
    is opened without one."""
    new = {"title": title, "body": _text(body, since=when, runs=1, sig=sig), "labels": label_list}
    if assignee:
        status, issue = gh.call("POST", "/issues", dict(new, assignees=[assignee]), ok=(201, 422))
        if status == 201:
            return issue["number"]
        if not _refused_assignee(issue):
            raise GitHubError(f"POST /issues: HTTP 422: {json.dumps(issue)[:300]}")
    _, issue = gh.call("POST", "/issues", new)
    return issue["number"]


def _refused_assignee(answer) -> bool:
    """A 422 whose only complaint is the assignee."""
    errors = answer.get("errors") if isinstance(answer, dict) else None
    return bool(errors) and all(isinstance(e, dict) and e.get("field") == "assignees" for e in errors)


def sync(
    gh: GitHub,
    state: State,
    *,
    failed: bool,
    body: str,
    run_url: str,
    assignee: str | None = None,
    sig: str = "",
    when: str = "",
) -> str:
    """Open, update or close the state's issue. Returns what it did. A new issue is
    assigned to `assignee` (when GitHub accepts it), so GitHub notifies them; a later
    failure comments (and so notifies) only when its signature `sig` changed."""
    group, own = labels(state)
    found = gh.open_issue(own)
    if not failed:
        return "passing; " + _close(gh, found, f"Passing again: {run_url}")
    gh.ensure_label(group, "B60205", "Opened and closed by the scheduled state checks")
    gh.ensure_label(own, "D93F0B", f"{state.name} checker check")
    if found is not None:
        return _keep_open(gh, found, body=body, sig=sig, when=when, comment_on_change=True)
    title = f"{state.code.upper()} ({state.name}) checker check failing"
    number = _open(gh, title=title, body=body, label_list=[group, own], assignee=assignee, sig=sig, when=when)
    return f"failing; opened #{number}"


def sync_outage(
    gh: GitHub,
    *,
    active: bool,
    title: str,
    body: str,
    run_url: str,
    assignee: str | None = None,
    sig: str = "",
    when: str = "",
) -> str:
    """Open, update or close the one outage issue. While it stays open its text is
    rewritten each batch; it is commented on (and so notifies) only when the kinds of
    failure change, not when states join or leave it."""
    found = gh.open_issue(OUTAGE)
    if not active:
        return "no outage; " + _close(gh, found, f"Most states pass again: {run_url}")
    gh.ensure_label(GROUP, "B60205", "Opened and closed by the scheduled state checks")
    gh.ensure_label(OUTAGE, "000000", "Half or more of the states failing in one batch")
    if found is not None:
        gh.call("PATCH", f"/issues/{found['number']}", {"title": title})
        return "outage; " + _keep_open(gh, found, body=body, sig=sig, when=when, comment_on_change=True)
    number = _open(gh, title=title, body=body, label_list=[GROUP, OUTAGE], assignee=assignee, sig=sig, when=when)
    return f"outage; opened #{number}"
