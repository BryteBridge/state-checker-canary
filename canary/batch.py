"""One batch: every state's report, read back after all the state jobs ran.

Builds the batch summary (one table, then each state's detail) and keeps each
state's issue in step. A state whose job left no usable report, or whose check
step did not succeed, counts as failed.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

from .check import TRANSIENT_OUTCOMES, CaseResult, Report, markdown, tolerated_limit
from .states import State


@dataclass
class StateResult:
    state: State
    report: Report | None  # None: no usable report
    failed: bool
    why: str = ""  # why it failed beyond the report's own problems: no report, or a failed step

    @property
    def body(self) -> str:
        """The state's detail, as its issue and the batch summary show it."""
        if self.report is None:
            return f"## ❌ {self.state.code.upper()} ({self.state.name})\n\nNo usable report: {self.why}.\n"
        text = markdown(self.report)
        return f"{text}\n**Failed:** {self.why}.\n" if self.why else text


def report_from(data) -> Report:
    """A Report from its to_dict() form; ValueError when the shape is wrong or its
    parts contradict its problem list."""
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise ValueError("not a report")
    if not all(isinstance(c, dict) for c in data["cases"]):
        raise ValueError("a case is not an object")
    for c in data["cases"]:
        for key in ("problem", "reason", "outcome"):
            if c.get(key) is not None and not isinstance(c[key], str):
                raise ValueError(f"a case's {key} is not text")
        for key in ("tolerated", "checker_missed"):
            if key in c and not isinstance(c[key], bool):
                raise ValueError(f"a case's {key} is not true or false (tolerated)")
    for key in ("problems", "warnings"):
        if not isinstance(data.get(key), list) or not all(isinstance(i, str) for i in data[key]):
            raise ValueError(f"{key} is not a list of text")
    cases = [CaseResult(**{k: v for k, v in c.items() if k in CaseResult.__dataclass_fields__}) for c in data["cases"]]
    fields = {k: v for k, v in data.items() if k in Report.__dataclass_fields__ and k != "cases"}
    report = Report(cases=cases, **fields)
    trip = report.round_trip
    if trip is not None:
        tries = trip.get("tries", []) if isinstance(trip, dict) else None
        if (
            not isinstance(trip, dict)
            or not isinstance(trip.get("checked"), bool)
            or not any(trip.get("passed") is v for v in (True, False, None))  # 0 == False, but is not
            or not isinstance(tries, list)
            or not all(isinstance(t, dict) and {"basis", "error", "age_seconds"} <= t.keys() for t in tries)
        ):
            raise ValueError("round_trip is malformed")
    trip = trip or {}
    tolerated = [c for c in cases if c.tolerated]
    if any(c.tolerated is not True and c.tolerated is not False for c in cases) or any(
        not c.problem or c.outcome not in TRANSIENT_OUTCOMES or c.checker_missed is not False for c in tolerated
    ):
        raise ValueError("a case is tolerated that is not an unanswered lookup")
    if len(tolerated) > tolerated_limit(len(cases)):
        raise ValueError(f"{len(tolerated)} of {len(cases)} lookups tolerated, more than allowed")
    failed_case = any(c.problem and not c.tolerated for c in cases)
    if not report.problems and (failed_case or trip.get("passed") is False):
        raise ValueError("a case or the round trip failed, but the report lists no problem")
    return report


def load(state: State, folder: Path) -> StateResult:
    """The state's result from its job's folder: report.json and outcome.txt (the
    check step's outcome: success, failure, ...)."""
    try:
        report = report_from(json.loads((folder / "report.json").read_text()))
    except Exception as e:  # noqa: BLE001  any unusable report is a failure to show, not a crash
        reason = "the job left no report" if isinstance(e, FileNotFoundError) else f"{type(e).__name__}: {e}"
        return StateResult(state, None, True, f"{reason} (it failed before or while checking)")
    try:
        outcome = (folder / "outcome.txt").read_text().strip()
    except (OSError, ValueError):  # missing, or not text: an unknown outcome fails the state
        outcome = "unknown"
    # From the report's problems, never a stored `ok`, and red if the step was.
    # Named only when the report looks clean, as the report's problems say the rest.
    why = (
        ""
        if outcome == "success" or not report.ok
        else f"the check step ended as {outcome} (the report may be partial)"
    )
    return StateResult(state, report, not report.ok or outcome != "success", why)


def _norm(text: str) -> str:
    """Problem text without what changes from batch to batch: ids, ages, counts."""
    return re.sub(r"\S*\d\S*", "#", text)


def signature(r: StateResult) -> str:
    """What kind of failure this is, short and stable: the same while the same thing is
    wrong, whichever entities the batch drew. Every way a registry does not answer is
    one kind, as states switch between timing out and refusing."""
    kinds: set[str] = set()
    if r.report is None:
        kinds.add("no report")
    else:
        own = {f"{c.eid}: {c.problem}" for c in r.report.cases if c.problem}
        for c in r.report.cases:
            if c.problem and not c.tolerated:
                if c.checker_missed:
                    kinds.add("the checker did not answer")
                kinds.add("the registry did not answer" if c.outcome in TRANSIENT_OUTCOMES else _norm(c.problem))
        kinds |= {_norm(p) for p in r.report.problems if p not in own}
    if r.why:
        kinds.add(_norm(r.why))
    return hashlib.sha256("\n".join(sorted(kinds)).encode()).hexdigest()[:12]


# An outage: half or more of the served states failing in one batch, out of at least
# this many (a manual batch of a few states never counts as one).
OUTAGE_MIN_STATES = 6


def covers_all(results: list[StateResult]) -> bool:
    """Whether the batch checked every served state: only such a batch can say an outage
    began or ended (a manual batch of a few states says neither)."""
    from .states import STATES

    return {r.state.code for r in results if r.state.served} >= {s.code for s in STATES if s.served}


def outage_signature(failed: list[StateResult], served: int) -> str:
    """The outage's kinds of failure, and whether nearly every state is in it: changes
    when, say, storage starts failing too, or a partial outage takes in nearly all the
    states, not when one more state joins or leaves."""
    size = "nearly all" if 10 * len(failed) >= 9 * served else "half or more"
    return hashlib.sha256(",".join([size, *sorted({signature(r) for r in failed})]).encode()).hexdigest()[:12]


def outage(results: list[StateResult]) -> list[StateResult]:
    """The failing served states, when they are an outage; empty otherwise."""
    served = [r for r in results if r.state.served]
    failed = [r for r in served if r.failed]
    return failed if len(served) >= OUTAGE_MIN_STATES and 2 * len(failed) >= len(served) else []


def outage_text(failed: list[StateResult], results: list[StateResult], run_url: str | None) -> tuple[str, str]:
    """The outage issue's title and text."""
    served = sum(1 for r in results if r.state.served)
    title = f"Checker-wide outage: {len(failed)} of {served} states failing"
    seconds = [
        c.seconds
        for r in failed
        if r.report is not None
        for c in r.report.cases
        if c.problem and c.outcome in TRANSIENT_OUTCOMES and isinstance(c.seconds, (int, float))
    ]
    lines = [
        f"## ❌ {len(failed)} of {served} states failing in the same batch",
        "",
        "When this many states fail together the likeliest cause is something they share rather than each "
        "state's registry. To look at first:",
        "- **the proxies**: an account out of bandwidth or unpaid refuses every request (HTTP 402), and every "
        "lookup then ends unanswered within a second or two;",
        "- **the checker's server** or its network;",
        "- **storage (R2)**, when the round trips fail while the lookups answer.",
        "",
    ]
    if seconds:
        lines += [
            f"The unanswered lookups took a median of {statistics.median(seconds):.1f} s "
            "(around a second each points at the proxies).",
            "",
        ]
    lines += ["| State | First problem |", "|---|---|"]
    for r in failed:
        first = (r.report.problems if r.report is not None else []) or [r.why]
        lines.append(f"| {r.state.code.upper()} {r.state.name} | {_cell(first[0])} |")
    lines += ["", "The states' own issues are left as they are until the outage ends; this issue closes then."]
    if run_url:
        lines += ["", f"[Batch run]({run_url})"]
    return title, "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def summary(results: list[StateResult], title: str, run_url: str | None = None) -> str:
    failed = [r for r in results if r.failed]
    head = f"## {'❌' if failed else '✅'} {title}: {len(results) - len(failed)} of {len(results)} states passed"
    lines = [head, ""]
    if outage(results):
        lines += ["**Outage:** half or more of the states failed together, likely from a shared cause (see the "
                  "outage issue).", ""]  # fmt: skip
    lines += [ "| State | Result | Live answers | Storage round trip | First problem or warning |",
             "|---|---|---|---|---|"]  # fmt: skip
    for r in results:
        rep = r.report
        if rep is None:
            lines.append(f"| {r.state.code.upper()} {r.state.name} | ❌ | | | {_cell(r.why)} |")
            continue
        good = sum(1 for c in rep.cases if not c.problem)
        trip = rep.round_trip or {}
        storage = "not checked"
        if trip.get("checked"):
            storage = {True: "✅", False: "❌"}.get(trip.get("passed"), "⚠️ inconclusive")
        note = (rep.problems or ([r.why] if r.why else []) or rep.warnings or [""])[0]
        mark = "❌" if r.failed else ("⚠️" if rep.warnings else "✅")
        answers = f"{good} of {len(rep.cases)}" if rep.cases else "not checked"
        lines.append(f"| {r.state.code.upper()} {r.state.name} | {mark} | {answers} | {storage} | {_cell(note)} |")
    if run_url:
        lines += ["", f"[This batch's run]({run_url})"]
    lines += ["", "### Each state", ""]
    for r in results:
        lines += ["<details>", f"<summary>{r.state.code.upper()} {r.state.name}: {'failed' if r.failed else 'passed'}"
                  "</summary>", "", r.body, "</details>", ""]  # fmt: skip
    return "\n".join(lines) + "\n"
