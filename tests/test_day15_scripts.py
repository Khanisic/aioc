"""Day 15 dev tooling, offline: the four-agent check's parallel-path evaluator and the cost
review's tally. Neither touches the network - the live halves are the scripts themselves."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from check_day13_sequential import _evaluate as _evaluate_sequential
from check_day15_integration import _evaluate_parallel
from cost_review import review, tally_run

from aioc.contracts import AgentName, InvocationMode
from aioc.coordinator.handoff import HANDOFF_HEADER, REFINEMENT_HEADER

# -- the parallel-path evaluator -------------------------------------------------------------


def _inv(agent: AgentName, *, depends_on: list[str] | None = None, round_no: int = 0) -> Any:
    deps = depends_on or []
    return SimpleNamespace(
        invocation_id=f"inv_{agent.value}_{round_no}",
        agent=agent,
        mode=InvocationMode.SEQUENTIAL if deps else InvocationMode.PARALLEL,
        depends_on=deps,
        round=round_no,
    )


def _resp(invocations: list[Any], answered: list[AgentName] | None = None) -> Any:
    agents = answered if answered is not None else [inv.agent for inv in invocations]
    return SimpleNamespace(
        selected_agents=invocations,
        agent_responses=[SimpleNamespace(agent=agent) for agent in agents],
    )


def _span(inv: Any, start: float, end: float) -> dict[str, Any]:
    return {"invocation_id": inv.invocation_id, "start": start, "end": end}


def _four() -> tuple[Any, Any, Any, Any]:
    incident, docs, github = (
        _inv(a) for a in (AgentName.INCIDENT, AgentName.DOCS, AgentName.GITHUB)
    )
    deployment = _inv(AgentName.DEPLOYMENT, depends_on=[github.invocation_id])
    return incident, docs, github, deployment


def test_four_agents_with_an_overlapping_parallel_group_pass() -> None:
    incident, docs, github, deployment = _four()
    timings = [
        _span(incident, 0.0, 20.0),
        _span(docs, 0.1, 15.0),
        _span(github, 0.2, 30.0),
        _span(deployment, 30.5, 60.0),
    ]
    complaints, overlapping = _evaluate_parallel(
        _resp([incident, docs, github, deployment]), timings
    )
    assert complaints == []
    assert ["incident", "docs"] in overlapping
    # The dependent is not a member of the parallel group, however its span falls.
    assert all("deployment" not in pair for pair in overlapping)


def test_a_missing_agent_is_named() -> None:
    incident, _docs, github, deployment = _four()
    timings = [_span(incident, 0, 5), _span(github, 0, 5), _span(deployment, 6, 9)]
    complaints, _ = _evaluate_parallel(_resp([incident, github, deployment]), timings)
    assert complaints == ["the plan did not select docs"]


def test_a_planned_agent_that_never_answered_is_named() -> None:
    incident, docs, github, deployment = _four()
    timings = [_span(i, 0, 5) for i in (incident, docs, github)] + [_span(deployment, 6, 9)]
    resp = _resp(
        [incident, docs, github, deployment],
        answered=[AgentName.INCIDENT, AgentName.GITHUB, AgentName.DEPLOYMENT],
    )
    complaints, _ = _evaluate_parallel(resp, timings)
    assert complaints == ["docs produced no response"]


def test_a_serial_parallel_group_fails() -> None:
    """The Day 9 barrier test's live twin: `mode: parallel` on paper is not enough, the
    measured intervals have to overlap."""
    incident, docs, github, deployment = _four()
    timings = [
        _span(incident, 0.0, 10.0),
        _span(docs, 10.0, 20.0),
        _span(github, 20.0, 30.0),
        _span(deployment, 30.0, 40.0),
    ]
    complaints, overlapping = _evaluate_parallel(
        _resp([incident, docs, github, deployment]), timings
    )
    assert overlapping == []
    assert complaints == ["no two invocations of the parallel group overlapped in time"]


def test_a_fully_chained_plan_has_no_parallel_path() -> None:
    incident = _inv(AgentName.INCIDENT)
    docs = _inv(AgentName.DOCS, depends_on=[incident.invocation_id])
    github = _inv(AgentName.GITHUB, depends_on=[docs.invocation_id])
    deployment = _inv(AgentName.DEPLOYMENT, depends_on=[github.invocation_id])
    complaints, _ = _evaluate_parallel(_resp([incident, docs, github, deployment]), [])
    assert complaints == ["the parallel group has 1 member(s); a parallel path needs at least two"]


def test_refinement_rounds_do_not_count_as_the_plan() -> None:
    """An agent that only ran as a re-delegation was not planned, and says so."""
    incident, _docs, github, deployment = _four()
    late_docs = _inv(AgentName.DOCS, round_no=1)
    timings = [_span(incident, 0, 5), _span(github, 0, 5), _span(deployment, 6, 9)]
    complaints, _ = _evaluate_parallel(_resp([incident, github, deployment, late_docs]), timings)
    assert "the plan did not select docs" in complaints


# -- the sequential evaluator, on the run that broke it ---------------------------------------


def test_an_agent_refined_in_two_rounds_is_compared_with_the_plans_block() -> None:
    """The first four-agent run re-delegated Deployment in round 1 and again in round 2. The
    evaluator took "the planner's block" from the last invocation that answered (round 2's,
    refinement header included), so round 1 could never start with it - the assertion was
    the bug, not the run."""
    planner = "Service under review: checkout-api. Diff the release PR #11 shipped."
    digest_of_github = f'{HANDOFF_HEADER}\n\n<handoff from="github">PR #11</handoff>'

    def refined(round_no: int) -> str:
        header = REFINEMENT_HEADER.format(round=round_no)
        return f"{planner}\n\n{header}\n- gap_x\n\n{digest_of_github}"

    def inv(iid: str, agent: AgentName, round_no: int, deps: list[str], context: str) -> Any:
        return SimpleNamespace(
            invocation_id=iid,
            agent=agent,
            round=round_no,
            mode=InvocationMode.SEQUENTIAL if deps else InvocationMode.PARALLEL,
            depends_on=deps,
            context_passed=context,
        )

    invocations = [
        inv("inv_gh", AgentName.GITHUB, 0, [], "Read PR #11."),
        inv("inv_dep", AgentName.DEPLOYMENT, 0, ["inv_gh"], f"{planner}\n\n{digest_of_github}"),
        inv("inv_dep_r1", AgentName.DEPLOYMENT, 1, ["inv_gh"], refined(1)),
        inv("inv_dep_r2", AgentName.DEPLOYMENT, 2, ["inv_gh"], refined(2)),
    ]
    resp = SimpleNamespace(
        selected_agents=invocations,
        agent_responses=[
            SimpleNamespace(agent=i.agent, invocation_id=i.invocation_id) for i in invocations
        ],
        refinement_rounds=2,
        cost=SimpleNamespace(input_tokens=1),
    )
    timings = [
        {"invocation_id": "inv_gh", "start": 0.0, "end": 10.0},
        {"invocation_id": "inv_dep", "start": 10.5, "end": 20.0},
        {"invocation_id": "inv_dep_r1", "start": 21.0, "end": 30.0},
        {"invocation_id": "inv_dep_r2", "start": 31.0, "end": 40.0},
    ]
    assert _evaluate_sequential(resp, timings, 11) == []

    # And the rule still bites: a re-delegation that lost the planner's block is named.
    invocations[2].context_passed = refined(1).removeprefix(planner).lstrip()
    assert _evaluate_sequential(resp, timings, 11) == [
        "inv_dep_r1 does not start with the planner's block for deployment"
    ]


# -- the cost review -------------------------------------------------------------------------


def _record(
    results: Path,
    name: str,
    day: str,
    events: list[dict[str, Any]],
    *,
    kind: str = "llm",
    model: str | None = None,
) -> None:
    run_dir = (
        results / "runs" / day / f"{day}__{kind}__{name}__{len(list(results.rglob('run.json')))}"
    )
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"env": {"aioc_model": model}}), encoding="utf-8")
    (run_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8"
    )
    row = {
        "kind": kind,
        "name": name,
        "started_at": f"{day}T12:00:00.000Z",
        "path": run_dir.relative_to(results).as_posix(),
    }
    with (results / "index.jsonl").open("a", encoding="utf-8") as index:
        index.write(json.dumps(row) + "\n")


def test_both_recorded_usage_shapes_are_read_and_priced(tmp_path: Path) -> None:
    _record(tmp_path, "new", "2026-09-17", [{"data": {"usage": {"in": 1_000_000, "out": 100_000}}}])
    _record(
        tmp_path,
        "old",
        "2026-08-09",
        [{"data": {"cost": {"input_tokens": 500_000, "output_tokens": 0}}}],
    )
    result = review(tmp_path, since=None)
    assert result["by_check"]["new"].usd == pytest.approx(2.00 + 1.00)  # Sonnet 5: $2 / $10
    assert result["by_check"]["old"].usd == pytest.approx(1.00)
    assert result["total"].input_tokens == 1_500_000
    assert result["total"].unmeasured_runs == 0


def test_a_run_without_usage_is_unmeasured_not_free(tmp_path: Path) -> None:
    _record(tmp_path, "early", "2026-07-29", [{"data": {"intent": "incident_diagnosis"}}])
    _record(tmp_path, "traced-fake", "2026-08-22", [{"data": {"cost": {"in": 0, "out": 0}}}])
    total = review(tmp_path, since=None)["total"]
    assert (total.runs, total.unmeasured_runs, total.usd) == (2, 2, 0.0)


def test_the_model_comes_from_the_event_then_the_run_then_the_default(tmp_path: Path) -> None:
    usage = {"usage": {"in": 1_000_000, "out": 0}}
    _record(
        tmp_path, "matrix", "2026-07-29", [{"name": "claude-haiku-4-5-20251001", "data": usage}]
    )
    _record(
        tmp_path,
        "pinned",
        "2026-07-29",
        [{"name": "diagnose", "data": usage}],
        model="claude-opus-5",
    )
    _record(tmp_path, "default", "2026-07-29", [{"name": "diagnose", "data": usage}])
    by_check = review(tmp_path, since=None)["by_check"]
    assert by_check["matrix"].usd == pytest.approx(1.00)  # the dated id prices as its family
    assert by_check["pinned"].usd == pytest.approx(5.00)
    assert by_check["default"].usd == pytest.approx(2.00)


def test_an_unknown_model_is_counted_but_never_priced(tmp_path: Path) -> None:
    _record(
        tmp_path,
        "future",
        "2026-09-18",
        [{"data": {"usage": {"in": 10, "out": 5}, "model": "claude-next-9"}}],
    )
    total = review(tmp_path, since=None)["total"]
    assert (total.input_tokens, total.output_tokens, total.usd) == (10, 5, 0.0)
    assert total.unpriced_models == {"claude-next-9"}


def test_since_and_kind_filter_the_index(tmp_path: Path) -> None:
    usage = [{"data": {"usage": {"in": 100, "out": 10}}}]
    _record(tmp_path, "before", "2026-08-01", usage)
    _record(tmp_path, "after", "2026-09-01", usage)
    _record(tmp_path, "all", "2026-09-01", usage, kind="pytest")
    result = review(tmp_path, since="2026-09-01")
    assert set(result["by_check"]) == {"after"}
    assert set(result["by_day"]) == {"2026-09-01"}


def test_a_run_directory_with_no_files_is_unmeasured(tmp_path: Path) -> None:
    tally = tally_run(tmp_path / "missing")
    assert (tally.runs, tally.unmeasured_runs) == (1, 1)
