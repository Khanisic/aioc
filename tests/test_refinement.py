"""Day 14: the coordinator's refinement loop.

Driven by scripted runners through the `AgentRunner` protocol - no API key, no network, no
cost. The loop consumes `Gap.suggested_agent` + `Gap.suggested_query` directly (the
coordinator rule in `.claude/rules/coordinator.md`), so every test here scripts a response
carrying a gap and asserts what the executor did with it: the re-delegated invocation's
query is the suggested query verbatim, its context is the planner's block plus the
refinement block plus the digest of the response that raised the gap, `round` is stamped,
`refinement_rounds` is bumped, the closed gap leaves `unresolved_gaps`, and the loop stops
where the rule says it must - `resolvable: false`, an unregistered agent, an identical gap
coming back, or the round cap.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from aioc.contracts import (
    AgentInvocation,
    AgentName,
    AgentResponse,
    Assessment,
    CoordinatorResponse,
    IncidentAgentResponse,
    InvocationMode,
    ResponseStatus,
)
from aioc.coordinator import Executor
from aioc.coordinator.handoff import HANDOFF_HEADER, REFINEMENT_HEADER, ROSTER_HEADER
from aioc.coordinator.synthesis import Synthesis, SynthesisError, SynthesisRequest
from aioc.llm import Usage
from tests.test_executor import (
    _CONTEXT,
    _FakeTracer,
    _github_response,
    _incident_response,
    _invocation,
    _plan,
    _RecordingRunner,
)

# ------------------------------------------------------------------------------ fixtures

LOOKBACK_QUERY = "Re-check payments-api rollout health with a 30 minute lookback window."


def _gap(
    gap_id: str = "gap_lookback",
    *,
    agent: str | None = "incident",
    query: str | None = LOOKBACK_QUERY,
    resolvable: bool = True,
    blocks_field: str | None = "findings.root_cause.value",
) -> dict[str, Any]:
    return {
        "id": gap_id,
        "description": "The baseline window was too short to compare against; widen it.",
        "kind": "missing_data",
        "kind_detail": None,
        "blocks_field": blocks_field,
        "suggested_agent": agent,
        "suggested_query": query,
        "resolvable": resolvable,
    }


def _with_gaps(response: AgentResponse, gaps: list[dict[str, Any]]) -> IncidentAgentResponse:
    """The fixture report, weakened to `partial` and carrying ``gaps`` - re-validated, so
    the scripted response is a contract-valid one."""
    payload = response.model_dump(mode="json")
    payload["status"] = "partial"
    payload["gaps"] = gaps
    return IncidentAgentResponse.model_validate(payload)


Step = Callable[[str, str], AgentResponse] | Exception


class _ScriptedRunner:
    """Answers each call with the next scripted step: a response factory, or an exception
    to raise. Records exactly what the executor handed over."""

    def __init__(self, steps: list[Step], *, tokens: tuple[int, int] = (100, 50)) -> None:
        self._steps = list(steps)
        self._tokens = tokens
        self.calls: list[dict[str, Any]] = []

    def run(
        self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
    ) -> AgentResponse:
        self.calls.append({"query": query, "context": context, "invocation_id": invocation_id})
        usage.input_tokens += self._tokens[0]
        usage.output_tokens += self._tokens[1]
        if not self._steps:
            raise AssertionError("scripted runner ran out of steps")
        step = self._steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(request_id, invocation_id)


def _gappy(gaps: list[dict[str, Any]]) -> Callable[[str, str], AgentResponse]:
    return lambda req, inv: _with_gaps(_incident_response(req, inv), gaps)


def _clean(req: str, inv: str) -> AgentResponse:
    return _incident_response(req, inv)


def _round(resp: CoordinatorResponse, n: int) -> list[AgentInvocation]:
    return [inv for inv in resp.selected_agents if inv.round == n]


# ---------------------------------------------------------- the done-when: re-delegation


def test_a_resolvable_gap_is_re_delegated_with_its_suggested_query_verbatim():
    runner = _ScriptedRunner([_gappy([_gap()]), _clean])
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "Why is checkout failing?")

    assert resp.refinement_rounds == 1
    first, second = runner.calls
    assert first["query"] == "Why is checkout failing?"
    assert second["query"] == LOOKBACK_QUERY  # consumed directly, never re-derived
    (refined,) = _round(resp, 1)
    assert refined.agent is AgentName.INCIDENT
    assert refined.invocation_id == second["invocation_id"]
    assert refined.invocation_id != "inv_incident"  # a new invocation, not a rerun
    assert "gap_lookback" in refined.reason
    # Machine bookkeeping the model never wrote: the round is stamped by the executor.
    assert [inv.round for inv in resp.selected_agents] == [0, 1]


def test_the_re_delegated_context_is_planner_block_then_digest_then_refinement_block():
    """Explicit context passing survives the loop: the re-delegated agent receives the
    planner's block for its agent, then the digest of the response that raised the gaps,
    then a block naming the gaps it is closing (with the suggested query verbatim), last
    and next to the query since Day 22 - and the response records that block verbatim."""
    runner = _ScriptedRunner([_gappy([_gap()]), _clean])
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "Why is checkout failing?")

    (refined,) = _round(resp, 1)
    context = runner.calls[1]["context"]
    assert context == refined.context_passed  # recorded verbatim
    assert context.startswith(_CONTEXT)  # the planner's block, unchanged, first
    header = REFINEMENT_HEADER.format(round=1)
    assert header in context
    assert "- gap_lookback [blocks findings.root_cause.value] (raised by incident, " in context
    assert f'asked: "{LOOKBACK_QUERY}"' in context
    assert HANDOFF_HEADER in context
    assert '<handoff from="incident" invocation_id="inv_incident"' in context
    assert context.index(_CONTEXT) < context.index(HANDOFF_HEADER) < context.index(header), (
        "planner block, then the digest, then the gaps to close closest to the query"
    )
    assert context.rstrip().endswith(f'asked: "{LOOKBACK_QUERY}"')
    # The re-delegation depends on the response that raised the gap, and says so.
    assert refined.mode is InvocationMode.SEQUENTIAL
    assert refined.depends_on == ["inv_incident"]


def test_a_closed_gap_leaves_unresolved_and_the_new_report_gaps_take_its_place():
    dead_end = _gap("gap_dead_end", agent=None, query=None, resolvable=False, blocks_field=None)
    runner = _ScriptedRunner([_gappy([_gap()]), _gappy([dead_end])])
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "q?")

    assert resp.refinement_rounds == 1
    assert [g.id for g in resp.unresolved_gaps] == ["gap_dead_end"]
    assert [r.invocation_id for r in resp.agent_responses] == [
        inv.invocation_id for inv in resp.selected_agents
    ]
    assert resp.status is ResponseStatus.PARTIAL
    # The round-0 report is still in the response, gap and all - nothing is rewritten.
    assert [g.id for g in resp.agent_responses[0].gaps] == ["gap_lookback"]


def test_a_clean_refinement_round_makes_the_run_complete():
    runner = _ScriptedRunner([_gappy([_gap()]), _clean])
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "q?")

    assert resp.unresolved_gaps == []
    assert resp.agent_responses[0].status is ResponseStatus.PARTIAL  # kept as reported
    assert resp.agent_responses[1].status is ResponseStatus.COMPLETE
    assert resp.status is ResponseStatus.COMPLETE  # judged on where the agent ended up
    # Round-trips the wire: two invocations of one agent, two responses, all validators.
    assert CoordinatorResponse.model_validate_json(resp.model_dump_json()) == resp


# ------------------------------------------------------------------------ stopping rules


def test_an_unresolvable_gap_is_never_re_delegated():
    stuck = _gap("gap_stuck", resolvable=False)  # names an agent and a query, but says no
    runner = _ScriptedRunner([_gappy([stuck])])
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "q?")

    assert resp.refinement_rounds == 0
    assert len(runner.calls) == 1
    assert [g.id for g in resp.unresolved_gaps] == ["gap_stuck"]
    assert resp.status is ResponseStatus.PARTIAL


def test_a_gap_naming_an_unregistered_agent_stays_open():
    runner = _ScriptedRunner([_gappy([_gap(agent="docs", query="What does the runbook say?")])])
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "q?")

    assert resp.refinement_rounds == 0
    assert len(runner.calls) == 1
    assert [g.id for g in resp.unresolved_gaps] == ["gap_lookback"]


def test_an_identical_gap_coming_back_is_not_asked_twice():
    runner = _ScriptedRunner([_gappy([_gap()]), _gappy([_gap()]), _gappy([_gap()])])
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "q?")

    assert resp.refinement_rounds == 1  # not 2, though the cap allows it
    assert len(runner.calls) == 2
    assert [g.id for g in resp.unresolved_gaps] == ["gap_lookback"]  # the round-1 copy


def test_the_round_cap_bounds_the_loop():
    # Every round comes back with a new, different question, so only the cap stops it.
    steps = [_gappy([_gap(f"gap_{i}", query=f"{LOOKBACK_QUERY} ({i})")]) for i in range(5)]
    plan = _plan([_invocation("incident")])

    runner = _ScriptedRunner(list(steps))
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "q?")
    assert resp.refinement_rounds == 2  # the default cap
    assert len(runner.calls) == 3
    assert [g.id for g in resp.unresolved_gaps] == ["gap_2"]

    runner = _ScriptedRunner(list(steps))
    resp = Executor({AgentName.INCIDENT: runner}, max_refinement_rounds=0).execute(plan, "q?")
    assert resp.refinement_rounds == 0
    assert len(runner.calls) == 1

    with pytest.raises(ValueError, match="max_refinement_rounds"):
        Executor({}, max_refinement_rounds=-1)


# ------------------------------------------------------------------- failures and retries


def test_a_failed_invocation_is_retried_once_and_a_good_retry_closes_the_failure():
    runner = _ScriptedRunner([RuntimeError("scripted agent failure"), _clean])
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "Why is checkout failing?")

    assert resp.refinement_rounds == 1
    assert runner.calls[1]["query"] == "Why is checkout failing?"  # the failed query, again
    (retry,) = _round(resp, 1)
    # Nothing to hand off from a failure: the retry is independent, and its context is
    # the planner's block plus the refinement block - no handoff header.
    assert retry.mode is InvocationMode.PARALLEL and retry.depends_on == []
    assert retry.context_passed.startswith(_CONTEXT)
    assert REFINEMENT_HEADER.format(round=1) in retry.context_passed
    assert HANDOFF_HEADER not in retry.context_passed
    assert "(raised by the coordinator)" in retry.context_passed
    assert resp.unresolved_gaps == []  # the failure gap was consumed by the good retry
    assert resp.status is ResponseStatus.COMPLETE
    assert [r.invocation_id for r in resp.agent_responses] == [retry.invocation_id]


def test_a_retry_that_fails_again_is_not_retried_a_third_time():
    runner = _ScriptedRunner([RuntimeError("down"), RuntimeError("still down")])
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "q?")

    assert resp.refinement_rounds == 1
    assert len(runner.calls) == 2
    assert resp.agent_responses == []
    assert resp.status is ResponseStatus.ERROR
    descriptions = [g.description for g in resp.unresolved_gaps]
    assert any("down" in d for d in descriptions) and any("still down" in d for d in descriptions)


# ---------------------------------------------------------------- grouping and parallelism


def test_gaps_from_two_agents_run_in_one_round():
    incident = _ScriptedRunner([_gappy([_gap()]), _clean])
    docs = _ScriptedRunner(
        [_gappy([_gap("gap_docs", agent="docs", query="Which runbook covers this?")]), _clean]
    )
    plan = _plan([_invocation("incident"), _invocation("docs", invocation_id="inv_docs")])
    resp = Executor({AgentName.INCIDENT: incident, AgentName.DOCS: docs}).execute(plan, "q?")

    assert resp.refinement_rounds == 1
    refined = _round(resp, 1)
    assert {inv.agent for inv in refined} == {AgentName.INCIDENT, AgentName.DOCS}
    assert docs.calls[1]["query"] == "Which runbook covers this?"
    assert incident.calls[1]["query"] == LOOKBACK_QUERY
    assert resp.unresolved_gaps == [] and resp.status is ResponseStatus.COMPLETE
    # A re-delegated planned agent opens with exactly what round 0 told it - the planner's
    # block and the roster of its siblings - before the refinement block (Day 16).
    for runner in (incident, docs):
        first, again = runner.calls[0]["context"], runner.calls[1]["context"]
        assert ROSTER_HEADER in first
        assert again.startswith(first)
        assert again.index(ROSTER_HEADER) < again.index(REFINEMENT_HEADER.format(round=1))


def test_several_gaps_for_one_agent_share_one_invocation_with_their_queries_listed():
    two = [_gap("gap_a", query="First question?"), _gap("gap_b", query="Second question?")]
    runner = _ScriptedRunner([_gappy(two), _clean])
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "q?")

    assert resp.refinement_rounds == 1 and len(runner.calls) == 2
    assert runner.calls[1]["query"] == (
        "Resolve each of the following:\n1. First question?\n2. Second question?"
    )
    (refined,) = _round(resp, 1)
    assert "gap_a" in refined.reason and "gap_b" in refined.reason
    assert resp.unresolved_gaps == []


def test_a_cross_agent_gap_hands_the_raising_agents_digest_to_the_target():
    """GitHub reports a gap only Deployment can close. Deployment was not in the plan, so
    there is no planner block for it; its context is the refinement block then GitHub's
    digest, and the invocation depends on GitHub's."""

    def _github_with_gap(req: str, inv: str) -> AgentResponse:
        payload = _github_response(req, inv).model_dump(mode="json")
        payload["status"] = "partial"
        payload["gaps"] = [
            _gap(
                "gap_rollout",
                agent="deployment",
                query="Is the rollout that shipped PR #412 healthy?",
                blocks_field=None,
            )
        ]
        return type(_github_response(req, inv)).model_validate(payload)

    github = _ScriptedRunner([_github_with_gap])
    deployment = _RecordingRunner()
    plan = _plan([_invocation("github", invocation_id="inv_gh")])
    resp = Executor({AgentName.GITHUB: github, AgentName.DEPLOYMENT: deployment}).execute(
        plan, "What did PR #412 change?"
    )

    assert resp.refinement_rounds == 1
    (call,) = deployment.calls
    assert call["query"] == "Is the rollout that shipped PR #412 healthy?"
    (refined,) = _round(resp, 1)
    assert refined.agent is AgentName.DEPLOYMENT
    assert refined.mode is InvocationMode.SEQUENTIAL and refined.depends_on == ["inv_gh"]
    # No planner's block for an agent the plan skipped: the digest leads, with nothing
    # blank before it, and the gaps to close come last.
    assert refined.context_passed.startswith(HANDOFF_HEADER)
    assert refined.context_passed.index(HANDOFF_HEADER) < refined.context_passed.index(
        REFINEMENT_HEADER.format(round=1)
    )
    # Not a planned agent, so no roster: nothing in the plan assigned it a part.
    assert ROSTER_HEADER not in refined.context_passed
    assert "(raised by github, invocation inv_gh)" in refined.context_passed
    assert '<handoff from="github" invocation_id="inv_gh"' in refined.context_passed
    assert "#412" in refined.context_passed  # GitHub's facts reached Deployment explicitly
    assert resp.unresolved_gaps == []
    # Deployment was skipped by the plan and is still recorded as skipped - the plan's
    # decision stands; the loop's invocation is visible in selected_agents with round 1.
    assert any(s.agent is AgentName.DEPLOYMENT for s in resp.skipped_agents)


# ----------------------------------------------------------------- tracing and cost


def test_refinement_spans_carry_the_round_and_the_query_and_cost_adds_up():
    tracer = _FakeTracer()
    runner = _ScriptedRunner([_gappy([_gap()]), _clean], tokens=(100, 50))
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}, tracer=tracer).execute(plan, "q?")

    (trace,) = tracer.traces
    first, second = trace.spans
    assert (first.metadata["round"], second.metadata["round"]) == (0, 1)
    assert first.metadata["query"] == "q?"
    assert second.metadata["query"] == LOOKBACK_QUERY
    assert second.input_text == _round(resp, 1)[0].context_passed
    assert (resp.cost.input_tokens, resp.cost.output_tokens) == (200, 100)
    assert "synthesis" not in {s.name for s in trace.spans}  # deterministic: no call


# ------------------------------------------------------------------ the synthesis seam


class _FakeSynthesiser:
    def __init__(self, result: Synthesis | Exception) -> None:
        self._result = result
        self.requests: list[SynthesisRequest] = []

    def synthesise(self, request: SynthesisRequest, *, usage: Usage) -> Synthesis:
        self.requests.append(request)
        usage.input_tokens += 700
        usage.output_tokens += 90
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_the_synthesiser_sees_every_round_and_its_answer_is_adopted():
    answer = Assessment[str](
        value="payments-api latency is the origin; hold the rollback until the lookback confirms.",
        confidence=0.7,
        evidence=["ev_1"],
        reasoning="Both incident reports cite ev_1.",
        detail=None,
    )
    synthesiser = _FakeSynthesiser(Synthesis(synthesis="Merged prose.", answer=answer))
    tracer = _FakeTracer()
    runner = _ScriptedRunner([_gappy([_gap()]), _clean], tokens=(100, 50))
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}, tracer=tracer, synthesiser=synthesiser).execute(
        plan, "q?"
    )

    (request,) = synthesiser.requests
    assert [(inv.round, r.invocation_id) for inv, r in request.reports] == [
        (0, "inv_incident"),
        (1, _round(resp, 1)[0].invocation_id),
    ]
    assert request.refinement_rounds == 1 and request.unresolved_gaps == []
    assert resp.synthesis == "Merged prose." and resp.answer == answer
    assert (resp.cost.input_tokens, resp.cost.output_tokens) == (200 + 700, 100 + 90)
    span = next(s for s in tracer.traces[0].spans if s.name == "synthesis")
    assert span.ended is not None and span.ended["status"] == "ok"
    assert span.ended["input_tokens"] == 700
    assert CoordinatorResponse.model_validate_json(resp.model_dump_json()) == resp


def test_a_rejected_model_synthesis_falls_back_to_the_deterministic_form_and_says_so():
    synthesiser = _FakeSynthesiser(SynthesisError("answer cites evidence id(s) ['ev_ghost']"))
    tracer = _FakeTracer()
    runner = _ScriptedRunner([_clean])
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}, tracer=tracer, synthesiser=synthesiser).execute(
        plan, "q?"
    )

    # The deterministic answer: the report's own summary and its own evidence ids.
    assert resp.answer.value == resp.agent_responses[0].summary
    assert resp.answer.evidence == ["ev_1"]
    assert resp.answer.reasoning is not None
    assert "deterministic form is used instead" in resp.answer.reasoning
    assert "ev_ghost" in resp.answer.reasoning
    assert resp.synthesis.startswith("Synthesis of 1 agent response(s)")
    span = next(s for s in tracer.traces[0].spans if s.name == "synthesis")
    assert span.ended is not None and span.ended["status"] == "error"
    assert resp.cost.input_tokens == 100 + 700  # the rejected call still cost
    assert resp.status is ResponseStatus.COMPLETE  # a synthesis fallback is not a gap
