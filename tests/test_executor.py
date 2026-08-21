"""Day 7: the coordinator's executor - delegation with explicit context passing.

Driven entirely by fake runners injected through the `AgentRunner` protocol - no API key, no
network, no cost. The two done-when facts of the day each have a direct test here:

- **A subagent receives context it never inherited.** The recording runner captures every
  argument the executor hands it, and the test asserts the context is *exactly*
  `AgentInvocation.context_passed` - not the situation block, not an enriched blend, not the
  plan. What the coordinator knew but did not write into the plan never reaches the agent.
- **No fabricated responses.** A plan may select agents that do not exist yet (github and
  deployment land on Days 11/12; the fakes here keep exercising the path). The executor
  must answer with a `Gap` carrying
  ``resolvable: false`` and a weakened status, never a plausible placeholder
  `AgentResponse` - a placeholder is exactly the failure the null-vs-[] rule exists to
  prevent.
"""

from __future__ import annotations

from typing import Any

import pytest

from aioc.contracts import (
    AgentName,
    CoordinatorResponse,
    GapKind,
    IncidentAgentResponse,
    ResponseStatus,
)
from aioc.coordinator import Executor, SelectionPlan
from aioc.coordinator.executor import respond
from aioc.llm import Usage

# ------------------------------------------------------------------------- plan fixtures


def _assessment(value: str | None, confidence: float) -> dict[str, Any]:
    return {
        "value": value,
        "confidence": confidence,
        "evidence": [],
        "reasoning": "derived from the query wording",
        "detail": None,
    }


_CONTEXT = (
    "checkout-api 5xx rose from 0.1% to 2.1% between 14:00 and 14:15 UTC while payments-api "
    "p99 went 120ms -> 2100ms. inventory-api is nominal. No deploys in 24h. Determine "
    "whether payments-api is the origin."
)


def _invocation(agent: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "invocation_id": f"inv_{agent}",
        "agent": agent,
        "reason": f"{agent} will establish the operational facts behind the symptom",
        "mode": "parallel",
        "depends_on": [],
        "context_passed": _CONTEXT,
        "round": 0,
    }
    base.update(over)
    return base


def _skip(agent: str) -> dict[str, Any]:
    return {
        "agent": agent,
        "reason": f"the query names no {agent} artefact and needs no {agent} lookup to answer",
    }


def _plan(selected: list[dict[str, Any]], **over: Any) -> SelectionPlan:
    chosen = {inv["agent"] for inv in selected}
    payload: dict[str, Any] = {
        "intent": _assessment("incident_diagnosis", 0.9),
        "selected_agents": selected,
        "skipped_agents": [
            _skip(a) for a in ("incident", "docs", "github", "deployment") if a not in chosen
        ],
        "gaps": [],
    }
    payload.update(over)
    return SelectionPlan.model_validate(payload)


# ------------------------------------------------------------------------ agent fixtures


def _incident_response(
    request_id: str,
    invocation_id: str,
    *,
    status: str = "complete",
    overall_confidence: float = 0.72,
    with_evidence: bool = True,
) -> IncidentAgentResponse:
    """A minimal contract-valid incident report, built the way a real agent would."""
    if with_evidence:
        findings_assessments = {
            "severity": {
                "value": "sev2",
                "confidence": 0.8,
                "evidence": ["ev_1"],
                "reasoning": "customer-facing errors without a full outage",
                "detail": None,
            },
            "failure_mode": {
                "value": "downstream_latency",
                "confidence": 0.7,
                "evidence": ["ev_1"],
                "reasoning": "payments-api p99 rose 17x while checkout-api errored",
                "detail": None,
            },
            "root_cause": {
                "value": "payments-api latency breaching checkout-api's timeout",
                "confidence": 0.55,
                "evidence": ["ev_1"],
                "reasoning": "the 502 pattern matches downstream timeouts",
                "detail": None,
            },
        }
        evidence = [
            {
                "id": "ev_1",
                "source_type": "metric",
                "source_type_detail": None,
                "source_ref": 'http_request_duration_seconds{service="payments-api"}',
                "excerpt": "payments-api p99 went 120ms -> 2100ms.",
                "observed_at": "2026-08-08T14:15:00Z",
                "uri": None,
                "tool_call_id": None,
            }
        ]
        gaps: list[dict[str, Any]] = []
    else:
        null = {
            "value": None,
            "confidence": 0.2,
            "evidence": [],
            "reasoning": "nothing in the provided context supports a conclusion",
            "detail": None,
        }
        findings_assessments = {"severity": null, "failure_mode": null, "root_cause": null}
        evidence = []
        gaps = [
            {
                "id": f"gap_{name}",
                "description": f"{name} cannot be established from the provided context.",
                "kind": "missing_data",
                "kind_detail": None,
                "blocks_field": f"findings.{name}.value",
                "suggested_agent": None,
                "suggested_query": None,
                "resolvable": True,
            }
            for name in ("severity", "failure_mode", "root_cause")
        ]
    return IncidentAgentResponse.model_validate(
        {
            "agent": "incident",
            "request_id": request_id,
            "invocation_id": invocation_id,
            "status": status,
            "status_detail": None,
            "summary": "payments-api latency is degrading checkout-api; restart-and-watch.",
            "findings": {
                "incident_window": {"start": "2026-08-08T14:00:00Z", "end": None},
                "affected_services": ["checkout-api", "payments-api"],
                **findings_assessments,
                "contributing_factors": [],
                "timeline": [],
                "impact": {
                    "error_rate_before": None,
                    "error_rate_after": None,
                    "p50_latency_ms_before": None,
                    "p50_latency_ms_after": None,
                    "p99_latency_ms_before": None,
                    "p99_latency_ms_after": None,
                    "requests_affected": None,
                    "duration_seconds": None,
                },
                "recommended_actions": [],
                "similar_incidents": [],
            },
            "evidence": evidence,
            "gaps": gaps,
            "overall_confidence": overall_confidence,
            "generated_at": "2026-08-08T14:20:00Z",
        }
    )


class _RecordingRunner:
    """Captures exactly what the executor hands over, and answers with a valid report."""

    def __init__(self, *, tokens: tuple[int, int] = (120, 240), order_log: list[str] | None = None):
        self.calls: list[dict[str, Any]] = []
        self._tokens = tokens
        self._order_log = order_log

    def run(
        self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
    ) -> IncidentAgentResponse:
        self.calls.append(
            {
                "query": query,
                "context": context,
                "request_id": request_id,
                "invocation_id": invocation_id,
            }
        )
        if self._order_log is not None:
            self._order_log.append(invocation_id)
        usage.input_tokens += self._tokens[0]
        usage.output_tokens += self._tokens[1]
        return _incident_response(request_id, invocation_id)


class _FailingRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, query: str, **_: Any) -> IncidentAgentResponse:
        self.calls += 1
        raise RuntimeError("scripted agent failure")


# -------------------------------------------------- explicit context passing, the done-when


def test_subagent_receives_exactly_context_passed_and_nothing_else():
    # The coordinator "knew" more than it wrote into the plan (the query itself mentions a
    # deploy freeze the context block does not). The runner must see context_passed verbatim:
    # nothing added, nothing inherited.
    runner = _RecordingRunner()
    plan = _plan([_invocation("incident")])
    query = "Why is checkout failing? Note: there is a deploy freeze until Monday."

    Executor({AgentName.INCIDENT: runner}).execute(plan, query, request_id="req_d7")

    (call,) = runner.calls
    assert call["context"] == _CONTEXT  # exactly the plan's block, character for character
    assert "deploy freeze" not in call["context"]
    assert call["query"] == query
    assert call["request_id"] == "req_d7"
    assert call["invocation_id"] == "inv_incident"


def test_response_assembles_the_contract_envelope():
    runner = _RecordingRunner()
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "Why is checkout failing?")

    assert isinstance(resp, CoordinatorResponse)
    assert resp.intent == plan.intent
    assert resp.selected_agents == plan.selected_agents
    assert resp.skipped_agents == plan.skipped_agents
    assert [r.agent for r in resp.agent_responses] == [AgentName.INCIDENT]
    assert resp.refinement_rounds == 0  # the loop is Day 14
    assert resp.trace_id is None  # Langfuse is Day 9
    assert resp.status is ResponseStatus.COMPLETE
    assert resp.completed_at >= resp.received_at


def test_answer_cites_subagent_evidence_and_never_mints_ids():
    runner = _RecordingRunner()
    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "Why is checkout failing?")

    union = {e.id for r in resp.agent_responses for e in r.evidence}
    assert resp.answer.value is not None
    assert resp.answer.evidence, "a confident answer must cite evidence"
    assert set(resp.answer.evidence) <= union
    # Round-trips the wire, which re-runs every envelope validator on the way back in.
    assert CoordinatorResponse.model_validate_json(resp.model_dump_json()) == resp


# ------------------------------------------------- missing agents produce gaps, not fakes


def test_unimplemented_agent_yields_a_gap_not_a_fabricated_response():
    plan = _plan([_invocation("docs")])
    resp = Executor({}).execute(plan, "What is the rollback procedure for checkout?")

    assert resp.agent_responses == []  # nothing was fabricated
    gap = next(g for g in resp.unresolved_gaps if g.kind_detail == "agent_not_implemented")
    assert gap.kind is GapKind.OTHER
    assert gap.resolvable is False  # the Day 14 loop must not retry an absent agent
    assert resp.status is ResponseStatus.INSUFFICIENT_EVIDENCE
    assert resp.answer.value is None
    assert resp.answer.confidence == 0.0


def test_partial_when_some_agents_ran_and_some_do_not_exist():
    runner = _RecordingRunner()
    plan = _plan([_invocation("incident"), _invocation("docs")])
    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "Why is checkout failing?")

    assert [r.agent for r in resp.agent_responses] == [AgentName.INCIDENT]
    assert resp.status is ResponseStatus.PARTIAL
    assert any(g.kind_detail == "agent_not_implemented" for g in resp.unresolved_gaps)


# --------------------------------------------------------------------- failed invocations


def test_a_failing_agent_becomes_a_retryable_gap():
    failing = _FailingRunner()
    plan = _plan([_invocation("incident")])
    query = "Why is checkout failing?"
    resp = Executor({AgentName.INCIDENT: failing}).execute(plan, query)

    gap = next(g for g in resp.unresolved_gaps if g.kind_detail == "agent_invocation_failed")
    # Machine-consumable by the Day 14 loop: the retry target is spelled out, not implied.
    assert gap.resolvable is True
    assert gap.suggested_agent is AgentName.INCIDENT
    assert gap.suggested_query == query
    assert resp.agent_responses == []
    assert resp.status is ResponseStatus.ERROR


def test_one_failure_does_not_kill_the_other_agents():
    runner = _RecordingRunner()
    plan = _plan(
        [
            _invocation("incident"),
            _invocation("docs", invocation_id="inv_docs"),
        ]
    )
    resp = Executor({AgentName.INCIDENT: runner, AgentName.DOCS: _FailingRunner()}).execute(
        plan, "Why is checkout failing, and what does the runbook say?"
    )
    assert [r.agent for r in resp.agent_responses] == [AgentName.INCIDENT]
    assert resp.status is ResponseStatus.PARTIAL


# ------------------------------------------------------------ parallel vs sequential order


def test_sequential_invocation_runs_after_its_dependency():
    order: list[str] = []
    github = _RecordingRunner(order_log=order)
    deployment = _RecordingRunner(order_log=order)
    plan = _plan(
        [
            _invocation("github", invocation_id="inv_gh"),
            _invocation(
                "deployment",
                invocation_id="inv_dep",
                mode="sequential",
                depends_on=["inv_gh"],
                context_passed="Diff the release containing the PR github reports; "
                "the suspect window is 14:00-14:15 UTC.",
            ),
        ]
    )
    Executor({AgentName.GITHUB: github, AgentName.DEPLOYMENT: deployment}).execute(
        plan, "Did the last PR break the rollout?"
    )
    assert order == ["inv_gh", "inv_dep"]


def test_dependent_of_a_failed_dependency_is_not_run():
    deployment = _RecordingRunner()
    plan = _plan(
        [
            _invocation("github", invocation_id="inv_gh"),
            _invocation(
                "deployment",
                invocation_id="inv_dep",
                mode="sequential",
                depends_on=["inv_gh"],
                context_passed="Diff the release containing the PR github reports; "
                "the suspect window is 14:00-14:15 UTC.",
            ),
        ]
    )
    resp = Executor({AgentName.GITHUB: _FailingRunner(), AgentName.DEPLOYMENT: deployment}).execute(
        plan, "Did the last PR break the rollout?"
    )
    assert deployment.calls == []  # never run against input that did not arrive
    unmet = next(g for g in resp.unresolved_gaps if g.kind is GapKind.MISSING_DATA)
    assert "inv_gh" in unmet.description
    assert unmet.resolvable is False


# ------------------------------------------------------------------------- honest numbers


def test_cost_is_accumulated_from_usage_not_estimated():
    runner = _RecordingRunner(tokens=(120, 240))
    plan = _plan([_invocation("incident")])
    seeded = Usage(input_tokens=300, output_tokens=180)  # the planning call's tokens

    resp = Executor({AgentName.INCIDENT: runner}).execute(plan, "q?", usage=seeded)

    assert resp.cost.input_tokens == 300 + 120
    assert resp.cost.output_tokens == 180 + 240
    assert resp.cost.usd is None  # not measured, so not invented


def test_answer_confidence_is_capped_when_the_report_cites_nothing():
    class _UncitedRunner:
        def run(
            self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
        ) -> IncidentAgentResponse:
            return _incident_response(
                request_id,
                invocation_id,
                status="insufficient_evidence",
                overall_confidence=0.8,
                with_evidence=False,
            )

    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: _UncitedRunner()}).execute(plan, "q?")

    # value is stated but uncited, so it must not claim the >= 0.5 band the contract
    # reserves for evidenced conclusions.
    assert resp.answer.value is not None
    assert resp.answer.evidence == []
    assert resp.answer.confidence < 0.5


def test_agent_gaps_surface_as_unresolved_and_weaken_status():
    class _GappyRunner:
        def run(
            self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
        ) -> IncidentAgentResponse:
            return _incident_response(
                request_id, invocation_id, status="insufficient_evidence", with_evidence=False
            )

    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: _GappyRunner()}).execute(plan, "q?")

    assert any(g.blocks_field == "findings.root_cause.value" for g in resp.unresolved_gaps)
    assert resp.status is ResponseStatus.PARTIAL  # ran, but not clean


# --------------------------------------------------------------------------- respond glue


def test_respond_plans_then_executes_with_one_usage_accumulator():
    # The planning call is scripted through the same fake-anthropic pattern the coordinator
    # tests use; the agent is a recording runner. Cost must cover both.
    from types import SimpleNamespace

    from anthropic.types import ToolUseBlock

    from aioc.coordinator import SELECT_TOOL_NAME, Coordinator
    from aioc.llm import LLMClient, LLMSettings

    plan_payload = {
        "intent": _assessment("incident_diagnosis", 0.92),
        "selected_agents": [_invocation("incident")],
        "skipped_agents": [_skip("docs"), _skip("github"), _skip("deployment")],
        "gaps": [],
    }
    scripted = SimpleNamespace(
        stop_reason="tool_use",
        model="claude-sonnet-5",
        content=[
            ToolUseBlock(type="tool_use", id="toolu_1", name=SELECT_TOOL_NAME, input=plan_payload)
        ],
        usage=SimpleNamespace(input_tokens=300, output_tokens=180),
    )

    class _FakeMessages:
        def create(self, **kwargs: Any) -> Any:
            return scripted

    fake = SimpleNamespace(messages=_FakeMessages())
    coordinator = Coordinator(LLMClient(LLMSettings(model="claude-sonnet-5"), client=fake))  # type: ignore[arg-type]
    runner = _RecordingRunner(tokens=(120, 240))

    resp = respond(
        "Why is checkout failing?",
        situation="payments-api p99 is 2100ms",
        coordinator=coordinator,
        executor=Executor({AgentName.INCIDENT: runner}),
    )

    assert resp.cost.input_tokens == 300 + 120
    assert resp.cost.output_tokens == 180 + 240
    assert resp.request_id.startswith("req_")
    (call,) = runner.calls
    assert call["request_id"] == resp.request_id  # one id threads the whole request


# ----------------------------------------------------------------------- plan-level guard


def test_a_cyclic_plan_is_rejected_at_validation():
    # Belt for the executor's braces: a cycle would leave every member waiting on another,
    # so SelectionPlan refuses it before the executor can deadlock (Day 7 addition).
    with pytest.raises(ValueError, match="circular depends_on"):
        _plan(
            [
                _invocation(
                    "github",
                    invocation_id="inv_gh",
                    mode="sequential",
                    depends_on=["inv_dep"],
                    context_passed="Read the PR the deployment diff points at; window 14:00Z.",
                ),
                _invocation(
                    "deployment",
                    invocation_id="inv_dep",
                    mode="sequential",
                    depends_on=["inv_gh"],
                    context_passed="Diff the release the PR belongs to; window 14:00Z.",
                ),
            ]
        )


# ------------------------------------------------------------------------- registration


def test_default_runners_register_incident_and_docs():
    """Day 8's easy-to-forget step: the Docs agent is wired into the default executor.

    Nothing else forces this registration (HANDOFF called it out) - until Days 11/12,
    github and deployment are the only honest agent_not_implemented gaps left.
    """
    from aioc.coordinator.executor import default_runners

    assert set(default_runners()) == {AgentName.INCIDENT, AgentName.DOCS}
