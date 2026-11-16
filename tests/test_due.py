from __future__ import annotations

import urllib.parse
from datetime import UTC, datetime, timedelta, timezone

import pytest

from canary.due import SPACING, due
from canary.issue import GitHubError

NOW = datetime(2026, 11, 16, 9, 17, tzinfo=UTC)


class FakeRuns:
    """GET /actions/workflows/<f>/runs and /actions/runs/<id>/jobs from a list of runs."""

    def __init__(self, runs, jobs=None, fail=False):
        self.runs, self.jobs, self.fail = runs, jobs or {}, fail
        self.paths: list[str] = []

    def call(self, method, path, payload=None, ok=(200, 201)):
        self.paths.append(path)
        if self.fail:
            raise GitHubError("GET: HTTP 403")
        if "/jobs" in path:
            run_id = int(path.split("/actions/runs/")[1].split("/")[0])
            return 200, {"jobs": self.jobs.get(run_id, [])}
        page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["page"][0])
        return 200, {"workflow_runs": self.runs[(page - 1) * 100 : page * 100]}


def _run(run_id, minutes_ago, event="schedule", title="Scheduled batch"):
    started = (NOW - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")
    return {"id": run_id, "event": event, "display_title": title, "run_started_at": started}


CHECKED = [{"name": "Plan the batch", "conclusion": "success"}, {"name": "CA California", "conclusion": "failure"}]
SKIPPED = [{"name": "Plan the batch", "conclusion": "success"}, {"name": "check", "conclusion": "skipped"}]
CANCELLED = [
    {"name": "Plan the batch", "conclusion": "success"},
    {"name": "CA California", "conclusion": "cancelled"},
    {"name": "Batch summary", "conclusion": "success"},
]


def go(gh, event="schedule"):
    return due(gh, event=event, workflow="check.yml", current_run="99", now=NOW)


def test_a_manual_or_push_run_is_always_due():
    assert go(None, event="workflow_dispatch")[0] is True


def test_no_batch_recently_is_due():
    gh = FakeRuns([_run(99, 0)])
    is_due, why = go(gh)
    assert is_due and "no batch" in why
    assert "branch=main" in gh.paths[0] and "created=%3E%3D2026-11-15T18%3A47%3A00Z" in gh.paths[0]


def test_a_batch_that_checked_recently_makes_the_next_start_not_due():
    gh = FakeRuns([_run(99, 0), _run(5, 60)], jobs={5: CHECKED})
    is_due, why = go(gh)
    assert not is_due and "60 minutes ago" in why


def test_a_start_that_was_not_due_does_not_count():
    gh = FakeRuns([_run(99, 0), _run(6, 60), _run(5, 160)], jobs={6: SKIPPED, 5: CHECKED})
    is_due, why = go(gh)
    assert is_due and "no batch" in why  # the one at 160 minutes is older than the spacing


def test_a_manual_run_of_all_states_counts_and_one_of_a_few_does_not():
    assert go(FakeRuns([_run(7, 30, "workflow_dispatch", "Manual batch (all)")], jobs={7: CHECKED}))[0] is False
    assert go(FakeRuns([_run(7, 30, "workflow_dispatch", "Manual batch (oh)")], jobs={7: CHECKED}))[0] is True


@pytest.mark.parametrize(
    "jobs",
    [
        [],  # cancelled while pending: no job ever ran
        [{"name": "Plan the batch", "conclusion": "failure"}],  # its plan failed
        CANCELLED,  # cancelled during the checks (the summary job does not count)
    ],
)
def test_a_run_that_checked_no_state_does_not_count(jobs):
    for event, title in (("schedule", "Scheduled batch"), ("workflow_dispatch", "Manual batch (all)")):
        assert go(FakeRuns([_run(7, 30, event, title)], jobs={7: jobs}))[0] is True


def test_a_run_still_checking_counts():
    jobs = [{"name": "CA California", "status": "in_progress", "conclusion": None}]
    assert go(FakeRuns([_run(7, 30)], jobs={7: jobs}))[0] is False


def test_a_batch_listed_on_a_later_page_is_found():
    runs = [_run(1000 + i, 1, "push", "x") for i in range(100)] + [_run(5, 60)]
    gh = FakeRuns(runs, jobs={5: CHECKED})
    assert go(gh)[0] is False
    assert sum("/runs?" in p for p in gh.paths) == 2


def test_a_run_created_long_ago_but_started_recently_counts():
    run = _run(5, 20) | {"created_at": (NOW - timedelta(hours=5)).isoformat()}
    assert go(FakeRuns([run], jobs={5: CHECKED}))[0] is False


def test_a_now_with_another_offset_is_read_as_utc():
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    gh = FakeRuns([])
    due(gh, event="schedule", workflow="check.yml", current_run="99", now=local)
    assert "created=%3E%3D2026-11-15T18%3A47%3A00Z" in gh.paths[0]


def test_the_spacing_is_under_the_interval():
    assert timedelta(hours=2) < SPACING < timedelta(hours=3)


def test_unreadable_runs_answer_due():
    is_due, why = go(FakeRuns([], fail=True))
    assert is_due and "could not be read" in why
    assert go(None)[0] is True


def test_only_runs_newer_than_the_newest_found_are_looked_into():
    gh = FakeRuns([_run(5, 40), _run(4, 100)], jobs={5: CHECKED, 4: CHECKED})
    go(gh)
    assert [p for p in gh.paths if "/jobs" in p] == ["/actions/runs/5/jobs?per_page=100"]


def test_a_batch_mostly_cancelled_does_not_count():
    jobs = [{"name": "CA California", "conclusion": "failure"}] + [
        {"name": f"S{i}", "conclusion": "cancelled"} for i in range(5)
    ]
    assert go(FakeRuns([_run(7, 30)], jobs={7: jobs}))[0] is True
