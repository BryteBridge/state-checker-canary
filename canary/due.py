"""Whether a scheduled batch is due: GitHub starts the workflow every hour, and only
one run in about three goes on to check the states.

GitHub delays and drops scheduled runs when it is busy. On a plain 3-hour cron the
batches came about three or four a day instead of eight (2026-11), and every gap
past four hours made the checker's console say the checks had stopped reporting.
Asking every hour and running when the last batch is old enough keeps them close
to 3 hours apart when GitHub drops some of the hourly starts.

A batch counts when it checked the states: a scheduled run, or a manual run of all
states, in which at least half the state jobs ran to an end (passed or failed) or
are running. A run cancelled before its checks, a start that was not due, and a
manual run of a few states do not count. Anything unreadable answers "due": an
extra batch costs a few minutes, a missing one leaves the states unwatched.

The spacing is best effort: when GitHub drops every hourly start for a while,
nothing here can run a batch.
"""

from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta

from .issue import GitHub, GitHubError
from .states import INTERVAL_HOURS

# Run when the last batch started this long ago or more: under the interval, so an
# hourly start that GitHub delays a little still runs.
SPACING = timedelta(hours=INTERVAL_HOURS) - timedelta(minutes=30)
PLAN_JOB = "Plan the batch"
SUMMARY_JOB = "Batch summary"
MANUAL_ALL = "Manual batch (all)"
# Runs are listed by when they were created; a run can start well after (queued behind
# another, delayed by GitHub) or start again (a rerun). Look this much further back.
CREATED_SLACK = timedelta(hours=12)
PAGES = 3


def _when(text) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else None


def _checked_states(gh: GitHub, run: dict) -> bool:
    """Whether this run checked the states (or is checking them): half or more of its
    state jobs passed, failed or are running. Cancelled and skipped ones checked nothing."""
    if run.get("event") == "workflow_dispatch":
        if run.get("display_title") != MANUAL_ALL:
            return False
    elif run.get("event") != "schedule":
        return False
    _, data = gh.call("GET", f"/actions/runs/{int(run['id'])}/jobs?per_page=100")
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    states = [j for j in jobs if isinstance(j, dict) and j.get("name") not in (PLAN_JOB, SUMMARY_JOB)]
    ran = [j for j in states if j.get("conclusion") in ("success", "failure") or j.get("status") == "in_progress"]
    return bool(states) and 2 * len(ran) >= len(states)


def last_batch(gh: GitHub, *, workflow: str, current_run: str, now: datetime) -> datetime | None:
    """When the newest batch other than this run started, looking back one spacing."""
    since = (now - SPACING - CREATED_SLACK).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs: list[dict] = []
    for page in range(1, PAGES + 1):
        query = urllib.parse.urlencode({"branch": "main", "per_page": 100, "page": page, "created": f">={since}"})
        _, data = gh.call("GET", f"/actions/workflows/{urllib.parse.quote(workflow)}/runs?{query}")
        found = data.get("workflow_runs", []) if isinstance(data, dict) else []
        runs += [r for r in found if isinstance(r, dict)]
        if len(found) < 100:
            break
    recent = []
    for run in runs:
        started = _when(run.get("run_started_at") or run.get("created_at"))
        if str(run.get("id")) != str(current_run) and started is not None and now - started < SPACING:
            recent.append((started, run))
    for started, run in sorted(recent, key=lambda x: x[0], reverse=True):  # newest first: one that counts ends it
        if _checked_states(gh, run):
            return started
    return None


def due(
    gh: GitHub | None, *, event: str, workflow: str, current_run: str, now: datetime | None = None
) -> tuple[bool, str]:
    """(due, why). Only scheduled runs are ever not due."""
    if event != "schedule":
        return True, f"a {event} run always checks"
    now = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        if gh is None:
            raise GitHubError("no token to read the earlier runs")
        last = last_batch(gh, workflow=workflow, current_run=current_run, now=now)
    except (GitHubError, KeyError, TypeError, ValueError) as e:
        return True, f"due: the earlier runs could not be read ({e})"
    if last is None:
        return True, f"due: no batch in the last {SPACING.total_seconds() / 3600:g} hours"
    age = now - last
    if age >= SPACING:
        return True, f"due: the last batch started {age.total_seconds() / 60:.0f} minutes ago"
    return False, f"not due: the last batch started {age.total_seconds() / 60:.0f} minutes ago"
