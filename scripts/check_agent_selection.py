"""Day 6 done-when: does the coordinator pick the right agents on the sample queries?

    uv run python scripts/check_agent_selection.py              # 2 live calls (default)
    uv run python scripts/check_agent_selection.py --all        # all 5 - 5 live calls
    uv run python scripts/check_agent_selection.py --case sequential_dependency

One Claude API call per case, so the default runs only the two most discriminating. Every case
is recorded under `test-results/`, including which agents were selected and why, so a routing
regression is diagnosable after the fact rather than only in scrollback.

The cases are ordered by how much they discriminate, not by complexity. A coordinator that
selects all four agents on everything passes a naive "did it pick incident?" check, so the
cases that matter are the ones where selecting too much is the failure: `narrow_incident`
should skip three agents, and `sequential_dependency` should produce an actual dependency edge
rather than two parallel invocations that happen to be in the right order.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.contracts import AgentName, Intent, InvocationMode  # noqa: E402
from aioc.coordinator import Coordinator, CoordinatorError  # noqa: E402
from aioc.llm import LLMSettings  # noqa: E402


@dataclass(frozen=True)
class Case:
    key: str
    query: str
    expect_selected: frozenset[AgentName]
    expect_intent: Intent | None
    # A case can require that at least one invocation be sequential - the parallel-vs-sequential
    # distinction is graded evidence, and "got the agents right by accident" is not evidence.
    expect_sequential: bool = False
    situation: str | None = None
    notes: str = ""
    discriminating: bool = field(default=False)


CASES: tuple[Case, ...] = (
    Case(
        key="narrow_incident",
        query="Checkout success rate has been dropping for ten minutes. What is happening?",
        expect_selected=frozenset({AgentName.INCIDENT}),
        expect_intent=Intent.INCIDENT_DIAGNOSIS,
        situation=(
            "checkout-api p99 975ms against a 5ms p50; payments-api p99 974ms; inventory-api "
            "nominal. No deploys in 24h."
        ),
        notes="Three agents must be skipped. Selecting docs or github here is over-selection.",
        discriminating=True,
    ),
    Case(
        key="sequential_dependency",
        query=(
            "PR 412 was merged yesterday. Did the release containing it change the rollout's "
            "health, and what exactly did it change?"
        ),
        expect_selected=frozenset({AgentName.GITHUB, AgentName.DEPLOYMENT}),
        expect_intent=None,  # code_change_review or deployment_check are both defensible
        expect_sequential=True,
        notes=(
            "The canonical sequential path: github must read the PR before deployment can diff "
            "the release it belongs to. Two parallel invocations would be wrong."
        ),
        discriminating=True,
    ),
    Case(
        key="pure_docs",
        query="What is our documented procedure for rolling back a bad payments deploy?",
        expect_selected=frozenset({AgentName.DOCS}),
        expect_intent=Intent.DOCUMENTATION_LOOKUP,
        notes="Asks what is written down, not what is happening. No live observation needed.",
    ),
    Case(
        key="incident_plus_docs_parallel",
        query=(
            "payments-api is throwing 500s right now. What is wrong, and what does the runbook "
            "say we should do about it?"
        ),
        expect_selected=frozenset({AgentName.INCIDENT, AgentName.DOCS}),
        expect_intent=Intent.MIXED,
        notes=(
            "Two independent questions - the canonical parallel pair. Neither waits on the other."
        ),
    ),
    Case(
        key="deployment_only",
        query="Is the 2026.7.3 rollout healthy enough to continue ramping?",
        expect_selected=frozenset({AgentName.DEPLOYMENT}),
        expect_intent=Intent.DEPLOYMENT_CHECK,
        notes="Rollout status only. No code question and nothing is reported broken.",
    ),
)


def _evaluate(case: Case, plan) -> tuple[bool, list[str]]:
    """Compare a plan against the case's expectations. Returns (passed, complaints)."""
    complaints: list[str] = []
    selected = {inv.agent for inv in plan.selected_agents}

    if selected != set(case.expect_selected):
        expected = sorted(a.value for a in case.expect_selected)
        got = sorted(a.value for a in selected)
        complaints.append(f"selected {got}, expected {expected}")

    # Dynamic-selection evidence: anything not selected must be explained.
    accounted = selected | {s.agent for s in plan.skipped_agents}
    if len(accounted) != 4:
        complaints.append("not all four agents accounted for")
    if len(selected) < 4 and not plan.skipped_agents:
        complaints.append("skipped_agents is empty - dynamic selection is not discriminating")

    if case.expect_intent is not None and plan.intent.value is not case.expect_intent:
        got = None if plan.intent.value is None else plan.intent.value.value
        complaints.append(f"intent {got}, expected {case.expect_intent.value}")

    if case.expect_sequential:
        if not any(i.mode is InvocationMode.SEQUENTIAL for i in plan.selected_agents):
            complaints.append("no sequential invocation - the dependency was not modelled")
        elif not any(i.depends_on for i in plan.selected_agents):
            complaints.append("sequential invocation has no depends_on edge")

    # Explicit context passing is validated by the model and the planner, but a context that is
    # merely short is a quality signal worth surfacing rather than an error.
    for inv in plan.selected_agents:
        if len(inv.context_passed.split()) < 12:
            complaints.append(f"{inv.agent.value} context_passed is suspiciously thin")

    return (not complaints, complaints)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--all", action="store_true", help="run all 5 cases (5 live calls)")
    parser.add_argument("--case", action="append", help="run only these case keys")
    args = parser.parse_args(argv)

    if args.case:
        chosen = [c for c in CASES if c.key in set(args.case)]
        unknown = set(args.case) - {c.key for c in CASES}
        if unknown:
            parser.error(f"unknown case(s): {sorted(unknown)}")
    elif args.all:
        chosen = list(CASES)
    else:
        chosen = [c for c in CASES if c.discriminating]

    if LLMSettings().anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2

    coordinator = Coordinator()
    passed = 0

    with RunRecorder(
        kind="llm",
        name="agent-selection",
        command="check_agent_selection.py " + " ".join(c.key for c in chosen),
        metadata={"cases": [c.key for c in chosen], "total_cases_defined": len(CASES)},
    ) as run:
        for case in chosen:
            start = time.monotonic()
            try:
                plan = coordinator.plan(case.query, situation=case.situation)
            except (CoordinatorError, ValueError) as exc:
                run.event(
                    case.key,
                    outcome="failed",
                    duration_ms=(time.monotonic() - start) * 1000,
                    type="llm_call",
                    message=f"{type(exc).__name__}: {exc}",
                    data={"query": case.query},
                )
                print(f"  FAIL  {case.key}: {type(exc).__name__}: {exc}")
                continue

            duration_ms = (time.monotonic() - start) * 1000
            good, complaints = _evaluate(case, plan)
            passed += good
            selected = sorted(i.agent.value for i in plan.selected_agents)

            run.event(
                case.key,
                outcome="passed" if good else "failed",
                duration_ms=duration_ms,
                type="llm_call",
                data={
                    "query": case.query,
                    "intent": None if plan.intent.value is None else plan.intent.value.value,
                    "intent_confidence": plan.intent.confidence,
                    "selected": selected,
                    "skipped": sorted(s.agent.value for s in plan.skipped_agents),
                    "modes": {i.agent.value: i.mode.value for i in plan.selected_agents},
                    "depends_on": {
                        i.agent.value: i.depends_on for i in plan.selected_agents if i.depends_on
                    },
                    "context_word_counts": {
                        i.agent.value: len(i.context_passed.split()) for i in plan.selected_agents
                    },
                    "complaints": complaints,
                },
                message="; ".join(complaints) if complaints else f"selected {selected}",
            )
            run.artifact(f"{case.key}.plan.json", plan.model_dump_json(indent=2))

            print(f"  {'PASS' if good else 'FAIL'}  {case.key}: selected {selected}")
            for skip in plan.skipped_agents:
                print(f"          skipped {skip.agent.value}: {skip.reason[:88]}")
            for complaint in complaints:
                print(f"          ! {complaint}")

    print(f"\n--- agent selection: {passed}/{len(chosen)} cases correct ---")
    if len(chosen) < len(CASES):
        skipped = [c.key for c in CASES if c not in chosen]
        print(f"not run (each costs one API call): {skipped}")
        print("run them with --all when you want the full done-when measured.")
    print(f"records: {run.dir}")
    return 0 if passed == len(chosen) else 1


if __name__ == "__main__":
    raise SystemExit(main())
