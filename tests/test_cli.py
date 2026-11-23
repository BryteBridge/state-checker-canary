from __future__ import annotations

import json

import pytest

from canary import __main__ as cli
from canary import batch, issue, pools, states
from canary.check import CaseResult, Checker, Report


def _fake_run(ok):
    def run(self, state, pool, *, count=5):
        r = Report(state.code.upper(), state.name, "2026-01-01T00:00:00+00:00", served=True)
        if not ok:
            r.problems.append("200: expected a verified answer, got technical (X)")
        r.warnings.append("a warning")
        return r

    return run


# plan


def test_plan_lists_every_state_in_order(capsys):
    cli.main(["plan"])
    plan = json.loads(capsys.readouterr().out)
    assert [p["code"] for p in plan] == [s.code for s in states.STATES]
    assert plan[0] == {"code": "ca", "name": "CA California"}


def test_plan_takes_a_comma_list_without_duplicates(capsys):
    cli.main(["plan", "--states", " OH, wa ,oh,"])
    assert [p["code"] for p in json.loads(capsys.readouterr().out)] == ["oh", "wa"]


def test_plan_refuses_an_unknown_state():
    with pytest.raises(SystemExit):
        cli.main(["plan", "--states", "oh,zz"])


# check


@pytest.mark.parametrize("ok, code", [(True, 0), (False, 1)])
def test_check_writes_report_summary_and_annotations(tmp_path, monkeypatch, capsys, ok, code):
    monkeypatch.setenv("CHECKER_BASE_URL", "https://checker.invalid")
    monkeypatch.setattr(Checker, "run", _fake_run(ok))
    out, summary = tmp_path / "out" / "r.json", tmp_path / "s.md"
    assert cli.main(["check", "--state", "AZ", "--json", str(out), "--summary", str(summary)]) == code
    assert json.loads(out.read_text())["ok"] is ok
    assert summary.read_text().startswith("## ")
    printed = capsys.readouterr().out
    assert "::add-mask::checker.invalid" in printed
    assert "::warning title=AZ check::a warning" in printed
    assert ("::error title=AZ check::" in printed) is (not ok)


def test_check_refuses_a_bad_base_url(monkeypatch):
    monkeypatch.setenv("CHECKER_BASE_URL", "http://plain.invalid")
    with pytest.raises(SystemExit):
        cli.main(["check", "--state", "az"])


def test_unknown_state_exits():
    with pytest.raises(SystemExit):
        cli.main(["check", "--state", "zz"])


@pytest.mark.parametrize("bad", ["many", "0", "21", "2.5"])
def test_count_must_be_1_to_20(bad):
    with pytest.raises(SystemExit):
        cli.main(["check", "--state", "az", "--count", bad])


def test_check_passes_the_state_pool_and_count(monkeypatch):
    monkeypatch.setenv("CHECKER_BASE_URL", "https://checker.invalid")
    seen = {}

    def run(self, state, pool, *, count=5):
        seen.update(pool=pool, count=count)
        return Report(state.code.upper(), state.name, "t", served=True)

    monkeypatch.setattr(Checker, "run", run)
    cli.main(["check", "--state", "or", "--count", "7"])
    assert seen["count"] == 7 and len(seen["pool"].ids) == 200 and seen["pool"].active


def test_check_redacts_the_host_from_report_summary_and_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHECKER_BASE_URL", "https://secret-host.invalid")

    def run(self, state, pool, *, count=5):
        r = Report(state.code.upper(), state.name, "t", served=True)
        r.problems.append(
            "no answer: certificate is not valid for 'secret-host.invalid' at https://secret-host.invalid"
        )
        return r

    monkeypatch.setattr(Checker, "run", run)
    out, summary = tmp_path / "r.json", tmp_path / "s.md"
    cli.main(["check", "--state", "az", "--json", str(out), "--summary", str(summary)])
    printed = capsys.readouterr().out
    assert "secret-host" not in out.read_text() and "secret-host" not in summary.read_text()
    assert "<checker>" in out.read_text()
    assert printed.count("secret-host") == 1 and "::add-mask::secret-host.invalid" in printed


def test_check_prints_the_summary_with_commands_off(monkeypatch, capsys):
    monkeypatch.setenv("CHECKER_BASE_URL", "https://checker.invalid")
    monkeypatch.setattr(Checker, "run", _fake_run(False))
    cli.main(["check", "--state", "az"])
    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("::stop-commands::"))
    token = lines[start].split("::")[2]
    end = lines.index(f"::{token}::")
    assert any(line.startswith("## ") for line in lines[start:end])
    assert any(line.startswith("::error ") for line in lines[end:])  # annotations after commands resume


# batch


def _write(folder, report=None, outcome="success", raw=None):
    folder.mkdir(parents=True)
    if raw is not None or report is not None:
        (folder / "report.json").write_text(json.dumps(raw if raw is not None else report.to_dict()))
    if outcome is not None:
        (folder / "outcome.txt").write_text(outcome + "\n")


def _plan(*codes):
    return json.dumps([{"code": c, "name": c.upper()} for c in codes])


@pytest.fixture
def synced(monkeypatch):
    seen = {}

    def sync(gh, state, *, failed, body, run_url, assignee=None, sig="", when=""):
        seen[state.code] = {"failed": failed, "body": body, "assignee": assignee, "sig": sig}
        return "ok"

    def sync_outage(gh, *, active, title, body, run_url, assignee=None, sig="", when=""):
        seen["outage"] = {"active": active, "title": title, "body": body, "sig": sig}
        return "ok"

    monkeypatch.setattr(issue, "sync", sync)
    monkeypatch.setattr(issue, "sync_outage", sync_outage)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GH_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "someone")
    return seen


def test_the_assignee_variable_wins_over_the_owner(tmp_path, synced, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "SomeOrg")
    monkeypatch.setenv("CANARY_ASSIGNEE", " a-person ")
    cli.main(["batch", "--plan", _plan("ct"), "--reports", str(tmp_path), "--issues"])
    assert synced["ct"]["assignee"] == "a-person"


def test_an_empty_assignee_variable_falls_back_to_the_owner(tmp_path, synced, monkeypatch):
    monkeypatch.setenv("CANARY_ASSIGNEE", "")  # an unset repository variable arrives empty
    cli.main(["batch", "--plan", _plan("ct"), "--reports", str(tmp_path), "--issues"])
    assert synced["ct"]["assignee"] == "someone"


def test_batch_summarizes_and_syncs_every_planned_state(tmp_path, synced, capsys):
    good = Report("AZ", "Arizona", "t", served=True)
    bad = Report("CA", "California", "t", served=True, problems=["200: wrong"])
    _write(tmp_path / "az", good)
    _write(tmp_path / "ca", bad, outcome="failure")
    summary = tmp_path / "summary.md"
    code = cli.main(["batch", "--plan", _plan("az", "ca", "ct"), "--reports", str(tmp_path),
                     "--summary", str(summary), "--issues"])  # fmt: skip
    assert code == 0
    assert {k: v["failed"] for k, v in synced.items() if k != "outage"} == {"az": False, "ca": True, "ct": True}
    assert "outage" not in synced  # 3 states: a partial batch leaves the outage issue alone
    assert synced["ct"]["assignee"] == "someone" and "the job left no report" in synced["ct"]["body"]
    text = summary.read_text()
    assert "1 of 3 states passed" in text
    assert "| AZ Arizona | ✅ |" in text and "| CA California | ❌ |" in text and "200: wrong" in text
    assert "::error title=State checks failed::2 of 3: CA, CT" in capsys.readouterr().out


def test_batch_without_issues_flag_touches_no_issue(tmp_path, synced):
    _write(tmp_path / "az", Report("AZ", "Arizona", "t", served=True))
    cli.main(["batch", "--plan", _plan("az"), "--reports", str(tmp_path)])
    assert synced == {}


@pytest.mark.parametrize(
    "raw",
    [
        {"cases": [None], "problems": [], "warnings": []},
        {"cases": [], "problems": "x", "warnings": []},
        [],
        {"cases": [], "problems": [], "warnings": [], "state": "AZ"},  # missing name, started_at
        {"cases": [{"eid": "1", "expect": "dead", "problem": "wrong"}], "problems": [], "warnings": [],
         "state": "AZ", "name": "Arizona", "started_at": "t"},
        {"cases": [], "problems": [], "warnings": [], "state": "AZ", "name": "Arizona", "started_at": "t",
         "round_trip": {"checked": True, "passed": False}},
    ],
)  # fmt: skip
def test_an_unusable_or_contradictory_report_counts_as_failed(tmp_path, raw):
    _write(tmp_path / "az", raw=raw)
    result = batch.load(states.get("az"), tmp_path / "az")
    assert result.failed and result.report is None


def test_a_stored_ok_never_overrides_the_problems(tmp_path):
    report = Report("AZ", "Arizona", "t", served=True, problems=["p"])
    _write(tmp_path / "az", raw={**report.to_dict(), "ok": True})
    assert batch.load(states.get("az"), tmp_path / "az").failed


@pytest.mark.parametrize("outcome", ["failure", None])
def test_a_check_step_that_did_not_succeed_fails_the_state(tmp_path, outcome):
    _write(tmp_path / "az", Report("AZ", "Arizona", "t", served=True), outcome=outcome)
    assert batch.load(states.get("az"), tmp_path / "az").failed


def test_plan_refuses_an_empty_selection():
    with pytest.raises(SystemExit):
        cli.main(["plan", "--states", ",,,"])


def test_one_state_issue_error_does_not_stop_the_others(tmp_path, monkeypatch, capsys):
    seen = []

    def sync(gh, state, *, failed, body, run_url, assignee=None, sig="", when=""):
        seen.append(state.code)
        if state.code == "az":
            raise issue.GitHubError("POST /issues/3/comments: HTTP 403: locked")
        return "ok"

    monkeypatch.setattr(issue, "sync", sync)
    monkeypatch.setattr(issue, "sync_outage", lambda gh, **kw: "ok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GH_TOKEN", "tok")
    code = cli.main(["batch", "--plan", _plan("az", "ca"), "--reports", str(tmp_path), "--issues"])
    assert seen == ["az", "ca"] and code == 1
    assert "::error title=Issue not updated::AZ: POST /issues/3/comments: HTTP 403: locked" in capsys.readouterr().out


def test_a_round_trip_that_is_not_an_object_counts_as_failed(tmp_path):
    report = Report("AZ", "Arizona", "t", served=True)
    _write(tmp_path / "az", raw={**report.to_dict(), "round_trip": "broken"})
    result = batch.load(states.get("az"), tmp_path / "az")
    assert result.failed and result.report is None


def test_a_failed_step_with_a_passing_report_says_why_in_body_and_table(tmp_path):
    _write(tmp_path / "az", Report("AZ", "Arizona", "t", served=True), outcome="failure")
    result = batch.load(states.get("az"), tmp_path / "az")
    assert result.failed and "the check step ended as failure" in result.body
    assert "the check step ended as failure" in batch.summary([result], "t")


@pytest.mark.parametrize(
    "trip",
    [
        {"checked": True, "passed": []},
        {"checked": "yes", "passed": True},
        {"checked": True, "passed": True, "tries": "x"},
        {"checked": True, "passed": True, "tries": [{"basis": "store"}]},
    ],
)
def test_a_malformed_round_trip_counts_as_failed_and_the_summary_still_renders(tmp_path, trip):
    report = Report("AZ", "Arizona", "t", served=True)
    _write(tmp_path / "az", raw={**report.to_dict(), "round_trip": trip})
    result = batch.load(states.get("az"), tmp_path / "az")
    assert result.failed and result.report is None
    assert "AZ Arizona" in batch.summary([result], "t")


def test_a_failed_step_with_problems_does_not_repeat_itself(tmp_path):
    _write(tmp_path / "az", Report("AZ", "Arizona", "t", served=True, problems=["200: wrong"]), outcome="failure")
    result = batch.load(states.get("az"), tmp_path / "az")
    assert result.failed and "check step ended" not in result.body


@pytest.mark.parametrize("passed", [0, 1, 0.0])
def test_a_round_trip_passed_that_is_not_a_boolean_counts_as_failed(tmp_path, passed):
    report = Report("AZ", "Arizona", "t", served=True)
    _write(tmp_path / "az", raw={**report.to_dict(), "round_trip": {"checked": True, "passed": passed, "tries": []}})
    assert batch.load(states.get("az"), tmp_path / "az").failed


def test_an_outcome_file_that_is_not_text_fails_the_state_without_crashing(tmp_path):
    _write(tmp_path / "az", Report("AZ", "Arizona", "t", served=True), outcome=None)
    (tmp_path / "az" / "outcome.txt").write_bytes(b"\xff\xfe")
    result = batch.load(states.get("az"), tmp_path / "az")
    assert result.failed and "AZ Arizona" in batch.summary([result], "t")


# pools


def test_every_state_has_a_pool_of_200_distinct_ids_with_active_ones():
    for s in states.STATES:
        pool = pools.load(s)
        assert len(pool.ids) == 200 == len(set(pool.ids)), s.code
        assert len(pool.active) >= 50 and set(pool.active) <= set(pool.ids), s.code


@pytest.mark.parametrize(
    "text, why",
    [
        ("# c\n1 active\n1\n", "appears twice"),
        ("1 active\nbad id\n", "not an entity id"),
        ("1 active\n-x\n", "not an entity id"),
        ("1\n2\n", "no id is marked active"),
    ],
)
def test_a_bad_pool_file_is_refused(tmp_path, text, why):
    (tmp_path / "az.txt").write_text(text)
    with pytest.raises(SystemExit, match=why):
        pools.load(states.get("az"), tmp_path)


def test_a_pool_line_may_end_with_a_comment(tmp_path):
    (tmp_path / "az.txt").write_text("# head\n1047374 active # verified 2026-11-03\n22  # inactive\n")
    pool = pools.load(states.get("az"), tmp_path)
    assert pool.ids == ("1047374", "22") and pool.active == ("1047374",)


def test_a_missing_pool_is_refused(tmp_path):
    with pytest.raises(SystemExit, match="no pool for AZ"):
        pools.load(states.get("az"), tmp_path)


# reading back a report with a tolerated lookup


def _tolerated_report(**case_changes):
    case = {"eid": "300", "expect": "verified", "outcome": "technical", "verdict": "unknown",
            "reason": "TECHNICAL_UNREACHABLE", "attempts": 2, "tolerated": True,
            "problem": "the registry did not answer (TECHNICAL_UNREACHABLE), twice"} | case_changes  # fmt: skip
    ok = {"eid": "200", "expect": "verified", "outcome": "verified", "verdict": "eligible", "attempts": 1}
    return {"state": "AZ", "name": "Arizona", "started_at": "2026-01-01T00:00:00+00:00",
            "cases": [ok, case, {**ok, "eid": "100"}, {**ok, "eid": "400"}, {**ok, "eid": "500"}],
            "problems": [], "warnings": ["300: tolerated"], "round_trip": None}  # fmt: skip


def test_a_report_with_one_tolerated_lookup_in_five_reads_back_as_passing():
    report = batch.report_from(_tolerated_report())
    assert report.ok and report.cases[1].tolerated is True


@pytest.mark.parametrize(
    "change",
    [{"outcome": "verified"}, {"problem": None}, {"tolerated": "yes"}, {"outcome": "not_found"}],
)
def test_a_tolerance_that_is_not_an_unanswered_lookup_is_refused(change):
    with pytest.raises(ValueError, match="tolerated"):
        batch.report_from(_tolerated_report(**change))


def test_more_tolerated_lookups_than_allowed_are_refused():
    data = _tolerated_report()
    data["cases"][2] = {**data["cases"][1], "eid": "100"}
    with pytest.raises(ValueError, match="more than allowed"):
        batch.report_from(data)


# outage


def _batch_of(tmp_path, failing, codes=None):
    codes = codes or [st.code for st in states.STATES]
    for c in codes:
        st = states.get(c)
        problems = ["200: the registry did not answer (TECHNICAL_UNREACHABLE), twice"] if c in failing else []
        _write(tmp_path / c, Report(c.upper(), st.name, "t", served=st.served, problems=problems),
               outcome="failure" if c in failing else "success")  # fmt: skip
    return _plan(*codes)


TWELVE = {"ca", "ct", "fl", "nc", "ny", "pa", "va", "wa", "wi", "wy", "ga", "mo"}


def test_half_the_states_failing_is_one_outage_and_their_issues_are_left_alone(tmp_path, synced, capsys):
    plan = _batch_of(tmp_path, failing=TWELVE)
    summary = tmp_path / "s.md"
    cli.main(["batch", "--plan", plan, "--reports", str(tmp_path), "--issues", "--summary", str(summary)])
    assert synced["outage"]["active"] is True and len(synced["outage"]["sig"]) == 12
    assert synced["outage"]["title"] == "Checker-wide outage: 12 of 24 states failing"
    assert "| CA California | 200: the registry did not answer" in synced["outage"]["body"]
    assert not (set(synced) & TWELVE)  # the failing states' issues untouched
    assert "**Outage:**" in summary.read_text()
    assert "::error title=Outage::12 states failing together" in capsys.readouterr().out


def test_fewer_than_half_failing_is_no_outage(tmp_path, synced):
    plan = _batch_of(tmp_path, failing=TWELVE - {"mo"})
    cli.main(["batch", "--plan", plan, "--reports", str(tmp_path), "--issues"])
    assert synced["outage"]["active"] is False
    assert synced["ca"]["failed"] is True and synced["ca"]["sig"]


def test_a_batch_of_a_few_states_neither_starts_nor_ends_an_outage(tmp_path, synced):
    plan = _batch_of(tmp_path, failing={"ca", "ct", "fl", "nc", "ny", "pa"}, codes=["ca", "ct", "fl", "nc", "ny", "pa"])
    cli.main(["batch", "--plan", plan, "--reports", str(tmp_path), "--issues"])
    assert "outage" not in synced  # the outage issue is not touched either way
    assert synced["ca"]["failed"] is True  # each state's own issue as usual


def test_the_outage_signature_follows_the_kinds_of_failure_not_the_states(tmp_path):
    def results(failing, problem):
        out = []
        for c in failing:
            rep = Report(c.upper(), states.get(c).name, "t", served=True, problems=[f"200: {problem}"])
            out.append(batch.StateResult(states.get(c), rep, True))
        return out

    unanswered = "the registry did not answer (TECHNICAL_UNREACHABLE), twice"
    a = batch.outage_signature(results(["ca", "ct", "fl"], unanswered), 6)
    assert a == batch.outage_signature(results(["ca", "ct", "fl", "nc"], unanswered), 6)
    assert a != batch.outage_signature(results(["ca", "ct", "fl"], "storage did not serve this run's answer back"), 6)
    assert a != batch.outage_signature(results(["ca", "ct", "fl", "nc", "ny", "pa"], unanswered), 6)  # nearly all


def test_the_signature_ignores_which_entities_and_how_the_registry_failed():
    def result(problems, reason):
        cases = [
            CaseResult(p.split(":")[0], "verified", outcome="technical", reason=reason, problem=p.split(": ", 1)[1])
            for p in problems
        ]
        rep = Report("AZ", "Arizona", "t", served=True, cases=cases, problems=problems)
        return batch.StateResult(states.get("az"), rep, True)

    a = result(["100: the registry did not answer (TECHNICAL_TIMEOUT), twice"], "TECHNICAL_TIMEOUT")
    unreachable = "the registry did not answer (TECHNICAL_UNREACHABLE), twice"
    b = result([f"555: {unreachable}", f"777: {unreachable}"], "TECHNICAL_UNREACHABLE")
    assert batch.signature(a) == batch.signature(b)
    storage = Report("AZ", "Arizona", "t", served=True,
                     problems=["200: storage did not serve this run's answer back after 3 tries (R2)"])  # fmt: skip
    assert batch.signature(batch.StateResult(states.get("az"), storage, True)) != batch.signature(a)


# due


def test_due_writes_the_output_and_a_notice(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    out = tmp_path / "out"
    assert cli.main(["due", "--event", "workflow_dispatch", "--github-output", str(out)]) == 0
    assert out.read_text() == "due=true\n"
    assert "::notice title=Batch due::" in capsys.readouterr().out


def test_a_tolerated_lookup_the_checker_itself_missed_is_refused():
    with pytest.raises(ValueError, match="tolerated"):
        batch.report_from(_tolerated_report(checker_missed=True))


@pytest.mark.parametrize("change", [{"problem": ["bad"]}, {"reason": 3}, {"outcome": {"x": 1}}])
def test_a_case_field_that_is_not_text_is_refused(change):
    with pytest.raises(ValueError, match="not text"):
        batch.report_from(_tolerated_report(**change))


def test_a_checker_that_did_not_answer_changes_the_signature():
    def result(missed):
        case = CaseResult("1", "verified", outcome="technical", problem="the registry did not answer (X), twice",
                          checker_missed=missed)  # fmt: skip
        rep = Report("AZ", "Arizona", "t", served=True, cases=[case], problems=[f"1: {case.problem}"])
        return batch.StateResult(states.get("az"), rep, True)

    assert batch.signature(result(True)) != batch.signature(result(False))
