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

Day 13 added the sequential handoff section: a dependent's context is the planner's block
plus a structured digest of its dependency's response, composed by the executor at the
moment the dependency returns and recorded verbatim in the response's `context_passed` -
so the explicit-passing assertion above holds for the composed block too.
"""

from __future__ import annotations

from typing import Any

import pytest

from aioc.contracts import (
    AgentName,
    CoordinatorResponse,
    GapKind,
    GitHubAgentResponse,
    IncidentAgentResponse,
    ResponseStatus,
)
from aioc.coordinator import Executor, SelectionPlan
from aioc.coordinator.executor import respond
from aioc.coordinator.handoff import HANDOFF_HEADER
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
    tool_calls: list[dict[str, Any]] | None = None,
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
            "tool_calls": tool_calls or [],
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


def _github_response(
    request_id: str, invocation_id: str, *, commits: int = 1
) -> GitHubAgentResponse:
    """A minimal contract-valid GitHub report: one merged PR with its facts, ``commits``
    commits behind it, one suspect change, and evidence the judgements cite."""
    return GitHubAgentResponse.model_validate(
        {
            "agent": "github",
            "request_id": request_id,
            "invocation_id": invocation_id,
            "status": "complete",
            "status_detail": None,
            "summary": "PR #412 rewrote the payments-api retry budget; merged yesterday.",
            "findings": {
                "repository": "acme/shop",
                "ref": "main",
                "pull_requests": [
                    {
                        "number": 412,
                        "title": "Tighten payments-api retry budget",
                        "state": "merged",
                        "state_detail": None,
                        "merged_at": "2026-08-07T16:40:00Z",
                        "head_sha": "9f3c2a1d4e5b6c7a8b9c0d1e2f3a4b5c6d7e8f90",
                        "files_changed": 3,
                        "additions": 41,
                        "deletions": 12,
                        "touched_paths": [
                            "services/payments/retry.py",
                            "deploy/payments-api.env",
                            "docker-compose.yml",
                        ],
                        "risk": {
                            "value": "medium",
                            "confidence": 0.7,
                            "evidence": ["ev_pr"],
                            "reasoning": "changes a production timeout",
                            "detail": None,
                        },
                        "summary": {
                            "value": "Halves the retry budget and lowers the timeout.",
                            "confidence": 0.8,
                            "evidence": ["ev_pr"],
                            "reasoning": "read from the patch",
                            "detail": None,
                        },
                    }
                ],
                "commits": [
                    {
                        "sha": f"{i:040x}",
                        "short_sha": f"{i:07x}",
                        "message": f"commit {i}\n\nlonger body that must not be digested",
                        "authored_at": "2026-08-07T15:00:00Z",
                        "touched_paths": ["services/payments/retry.py"],
                        "pull_request_number": 412,
                    }
                    for i in range(1, commits + 1)
                ],
                "suspect_changes": [
                    {
                        "change_ref": "#412",
                        "change_type": "config",
                        "change_type_detail": None,
                        "symptom_link": {
                            "value": "A shorter timeout would surface as 502s under load.",
                            "confidence": 0.55,
                            "evidence": ["ev_pr"],
                            "reasoning": "timeout lowered in the same PR",
                            "detail": None,
                        },
                    }
                ],
                "diff_summary": {
                    "value": "Retry budget and timeout tightened in payments-api config.",
                    "confidence": 0.8,
                    "evidence": ["ev_pr"],
                    "reasoning": "the diff is small and readable",
                    "detail": None,
                },
            },
            "evidence": [
                {
                    "id": "ev_pr",
                    "source_type": "pull_request",
                    "source_type_detail": None,
                    "source_ref": "#412",
                    "excerpt": "-RETRY_BUDGET=6\n+RETRY_BUDGET=3",
                    "observed_at": "2026-08-07T16:40:00Z",
                    "uri": None,
                    "tool_call_id": "tc_1",
                }
            ],
            "gaps": [
                {
                    "id": "gap_base",
                    "description": "The base ref the PR was cut from was not fetched.",
                    "kind": "missing_data",
                    "kind_detail": None,
                    "blocks_field": None,
                    "suggested_agent": None,
                    "suggested_query": None,
                    "resolvable": True,
                }
            ],
            "overall_confidence": 0.78,
            "tool_calls": [],
            "generated_at": "2026-08-08T14:20:00Z",
        }
    )


class _GitHubRunner:
    def __init__(self, *, commits: int = 1) -> None:
        self._commits = commits

    def run(
        self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
    ) -> GitHubAgentResponse:
        return _github_response(request_id, invocation_id, commits=self._commits)


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
    assert resp.refinement_rounds == 0  # a clean run needs no re-delegation
    assert resp.trace_id is None  # no tracer was configured, so the field is honestly null
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
    # No refinement here: this test is about the gap's shape, the loop has its own section.
    resp = Executor({AgentName.INCIDENT: failing}, max_refinement_rounds=0).execute(plan, query)

    gap = next(g for g in resp.unresolved_gaps if g.kind_detail == "agent_invocation_failed")
    # Machine-consumable by the refinement loop: the retry target is spelled out, not implied.
    assert gap.resolvable is True
    assert gap.suggested_agent is AgentName.INCIDENT
    assert gap.suggested_query == query
    assert resp.agent_responses == []
    assert resp.status is ResponseStatus.ERROR
    assert failing.calls == 1


def test_a_failed_invocation_gap_keeps_the_whole_error_not_its_first_line():
    """A pydantic ValidationError says "1 validation error for X" on line one and puts the
    rule that failed on line two. The first live sequential run lost line two to a
    first-line cut, so the response could not say what went wrong. The gap keeps the whole
    message, whitespace-collapsed and capped."""
    from pydantic import ValidationError

    class _InvalidRunner:
        def run(self, query: str, **_: Any) -> IncidentAgentResponse:
            payload = _incident_response("req", "inv_incident").model_dump()
            payload["findings"]["root_cause"]["value"] = None  # null under status complete
            return IncidentAgentResponse.model_validate(payload)

    plan = _plan([_invocation("incident")])
    resp = Executor({AgentName.INCIDENT: _InvalidRunner()}).execute(plan, "q?")
    gap = next(g for g in resp.unresolved_gaps if g.kind_detail == "agent_invocation_failed")
    assert "ValidationError" in gap.description
    assert "status 'complete' is invalid when a findings Assessment.value is null" in (
        gap.description
    )
    assert "\n" not in gap.description and len(gap.description) < 800
    with pytest.raises(ValidationError):
        _InvalidRunner().run("q?")


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


# ------------------------------------------------- Day 13: the sequential handoff itself


_DEP_CONTEXT = (
    "Diff the release containing the PR github reports for payments-api in production; "
    "the suspect window is 14:00-14:15 UTC. Report changed keys, images, and rollout health."
)


def _sequential_plan() -> SelectionPlan:
    return _plan(
        [
            _invocation("github", invocation_id="inv_gh"),
            _invocation(
                "deployment",
                invocation_id="inv_dep",
                mode="sequential",
                depends_on=["inv_gh"],
                context_passed=_DEP_CONTEXT,
            ),
        ]
    )


def test_dependent_receives_the_planner_block_plus_the_dependency_digest():
    """The done-when: Deployment is told what GitHub found, explicitly, and the response
    records the block it was told verbatim. Nothing is inherited - the digest is in the
    context because the executor wrote it there, and the response shows it."""
    deployment = _RecordingRunner()
    resp = Executor({AgentName.GITHUB: _GitHubRunner(), AgentName.DEPLOYMENT: deployment}).execute(
        _sequential_plan(), "Did PR 412 break the payments rollout?"
    )

    (call,) = deployment.calls
    ctx = call["context"]
    # The planner's block is kept, first and unchanged; the handoff is appended after it.
    assert ctx.startswith(_DEP_CONTEXT)
    assert HANDOFF_HEADER in ctx
    assert ctx.index(_DEP_CONTEXT) < ctx.index(HANDOFF_HEADER) < ctx.index("<handoff")
    # The facts a Deployment agent acts on are in it, from GitHub's report.
    assert '<handoff from="github" invocation_id="inv_gh"' in ctx
    assert "#412" in ctx and "9f3c2a1d4e5b6c7a8b9c0d1e2f3a4b5c6d7e8f90" in ctx
    assert "deploy/payments-api.env" in ctx
    assert "risk: medium @0.70" in ctx
    assert "gap_base [resolvable]" in ctx
    # ...and it is a digest, not the serialised response.
    assert '"schema_version"' not in ctx and "generated_at" not in ctx
    assert "longer body that must not be digested" not in ctx
    # Recorded verbatim on the executed invocation - the contract's sec 5 promise that
    # context_passed IS the block embedded in the prompt, character for character.
    dep_inv = next(i for i in resp.selected_agents if i.invocation_id == "inv_dep")
    assert dep_inv.context_passed == ctx
    assert dep_inv.mode.value == "sequential" and dep_inv.depends_on == ["inv_gh"]
    # The parallel invocation is untouched - it had nothing handed to it.
    gh_inv = next(i for i in resp.selected_agents if i.invocation_id == "inv_gh")
    assert gh_inv.context_passed == _CONTEXT
    assert [i.invocation_id for i in resp.selected_agents] == ["inv_gh", "inv_dep"]
    assert resp.status is ResponseStatus.PARTIAL  # GitHub reported an honest gap
    # Round-trips the wire with the composed block in place.
    assert CoordinatorResponse.model_validate_json(resp.model_dump_json()) == resp


def test_the_handoff_is_bounded_not_a_raw_dump():
    # Sixty commits behind the PR: the digest lists ten, says how many it left out, and
    # stays under its ceiling. A raw dump of the same response is many times larger.
    from aioc.coordinator.handoff import MAX_DIGEST_CHARS

    deployment = _RecordingRunner()
    github = _GitHubRunner(commits=60)
    Executor({AgentName.GITHUB: github, AgentName.DEPLOYMENT: deployment}).execute(
        _sequential_plan(), "q?"
    )
    (call,) = deployment.calls
    handoff = call["context"][call["context"].index("<handoff") :]
    assert "commits (60):" in handoff
    assert "(+50 more)" in handoff
    assert handoff.rstrip().endswith("</handoff>")
    assert len(handoff) <= MAX_DIGEST_CHARS + len("\n</handoff>")
    raw = _github_response("req", "inv_gh", commits=60).model_dump_json()
    assert len(handoff) < len(raw) / 3


def test_a_two_hop_chain_hands_each_link_its_direct_dependency_only():
    # incident (parallel) -> github (sequential on incident) -> deployment (sequential on
    # github). Deployment sees GitHub's digest; it does not see Incident's digest nor
    # GitHub's composed context - a handoff is a digest of the response, not a snowball.
    github = _RecordingRunner()  # records what it was handed; answers as incident
    deployment = _RecordingRunner()
    plan = _plan(
        [
            _invocation("incident"),
            _invocation(
                "github",
                invocation_id="inv_gh",
                mode="sequential",
                depends_on=["inv_incident"],
                context_passed="Find the change that explains the failure incident reports; "
                "repository acme/shop, window 14:00-14:15 UTC.",
            ),
            _invocation(
                "deployment",
                invocation_id="inv_dep",
                mode="sequential",
                depends_on=["inv_gh"],
                context_passed=_DEP_CONTEXT,
            ),
        ]
    )
    Executor(
        {
            AgentName.INCIDENT: _RecordingRunner(),
            AgentName.GITHUB: github,
            AgentName.DEPLOYMENT: deployment,
        }
    ).execute(plan, "q?")

    (gh_call,) = github.calls
    assert '<handoff from="incident" invocation_id="inv_incident"' in gh_call["context"]
    assert "failure_mode: downstream_latency @0.70" in gh_call["context"]
    (dep_call,) = deployment.calls
    assert 'invocation_id="inv_gh"' in dep_call["context"]
    assert 'invocation_id="inv_incident"' not in dep_call["context"]
    # One handoff block, one header: GitHub's composed context did not ride along.
    assert dep_call["context"].count("<handoff ") == 1
    assert dep_call["context"].count(HANDOFF_HEADER) == 1


def test_the_dependent_span_carries_the_composed_context():
    # What the trace shows as the agent's input must be what the agent actually saw.
    tracer = _FakeTracer()
    deployment = _RecordingRunner()
    Executor(
        {AgentName.GITHUB: _GitHubRunner(), AgentName.DEPLOYMENT: deployment}, tracer=tracer
    ).execute(_sequential_plan(), "q?")
    (call,) = deployment.calls
    span = next(s for s in tracer.traces[0].spans if s.name == "agent:deployment")
    assert span.input_text == call["context"]
    assert "<handoff" in span.input_text


def test_a_dependent_of_a_failed_dependency_records_the_planner_block_unchanged():
    # No handoff happened, so nothing is composed: the response must not claim a context
    # the agent never received.
    resp = Executor(
        {AgentName.GITHUB: _FailingRunner(), AgentName.DEPLOYMENT: _RecordingRunner()}
    ).execute(_sequential_plan(), "q?")
    dep_inv = next(i for i in resp.selected_agents if i.invocation_id == "inv_dep")
    assert dep_inv.context_passed == _DEP_CONTEXT


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


def test_default_runners_register_all_four_agents():
    """The easy-to-forget step each agent day has: wiring the agent into the default
    executor. Nothing else forces this registration (HANDOFF calls it out). As of Day 12
    the set is complete, so a default executor never produces an agent_not_implemented gap
    - that path stays for partial runner sets (the test above proves it still works).
    """
    from aioc.coordinator.executor import default_runners

    assert set(default_runners()) == set(AgentName)


# ---------------------------------------------------- Day 9: the parallel group is parallel


def test_parallel_group_runs_concurrently():
    """The done-when, offline: two agents provably in flight at the same time.

    Each runner blocks on a shared barrier until the *other* runner arrives. Under the old
    serial loop the first runner would wait alone until the 5s timeout broke the barrier
    and failed its invocation - so two clean responses are proof of overlap, not luck.
    """
    import threading

    barrier = threading.Barrier(2)

    class _MeetingRunner:
        def run(
            self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
        ) -> IncidentAgentResponse:
            barrier.wait(timeout=5.0)
            return _incident_response(request_id, invocation_id)

    plan = _plan([_invocation("incident"), _invocation("docs", invocation_id="inv_docs")])
    resp = Executor(
        {AgentName.INCIDENT: _MeetingRunner(), AgentName.DOCS: _MeetingRunner()}
    ).execute(plan, "Why is checkout failing, and what does the runbook say?")

    assert [r.invocation_id for r in resp.agent_responses] == ["inv_incident", "inv_docs"]
    assert resp.status is ResponseStatus.COMPLETE


def test_parallel_results_keep_plan_order_not_completion_order():
    import time

    class _SlowRunner:
        def run(
            self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
        ) -> IncidentAgentResponse:
            time.sleep(0.25)
            return _incident_response(request_id, invocation_id)

    plan = _plan([_invocation("incident"), _invocation("docs", invocation_id="inv_docs")])
    resp = Executor(
        {AgentName.INCIDENT: _SlowRunner(), AgentName.DOCS: _RecordingRunner()}
    ).execute(plan, "q?")
    # docs finished first, but the response is assembled in plan order - determinism must
    # not depend on scheduling.
    assert [r.invocation_id for r in resp.agent_responses] == ["inv_incident", "inv_docs"]


def test_parallel_runners_get_isolated_usage_accumulators_summed_into_cost():
    """The HANDOFF's named race: `Usage` is a plain object, so handing the shared
    accumulator to concurrent runners would lose token counts silently. The executor must
    hand every runner its own accumulator and merge after the join - exact totals, and
    never the shared instance."""
    seeded = Usage(input_tokens=300, output_tokens=180)
    seen: list[Usage] = []

    class _TokenRunner:
        def __init__(self, tokens: tuple[int, int]) -> None:
            self._tokens = tokens

        def run(
            self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
        ) -> IncidentAgentResponse:
            seen.append(usage)
            usage.input_tokens += self._tokens[0]
            usage.output_tokens += self._tokens[1]
            return _incident_response(request_id, invocation_id)

    plan = _plan(
        [
            _invocation("incident"),
            _invocation("docs", invocation_id="inv_docs"),
            _invocation("github", invocation_id="inv_gh"),
            _invocation("deployment", invocation_id="inv_dep"),
        ]
    )
    resp = Executor(
        {
            AgentName.INCIDENT: _TokenRunner((100, 10)),
            AgentName.DOCS: _TokenRunner((200, 20)),
            AgentName.GITHUB: _TokenRunner((400, 40)),
            AgentName.DEPLOYMENT: _TokenRunner((800, 80)),
        }
    ).execute(plan, "q?", usage=seeded)

    assert resp.cost.input_tokens == 300 + 100 + 200 + 400 + 800
    assert resp.cost.output_tokens == 180 + 10 + 20 + 40 + 80
    assert all(u is not seeded for u in seen)
    assert len({id(u) for u in seen}) == len(seen) == 4


def test_a_failure_in_the_parallel_group_does_not_kill_its_peers():
    # The Day 7 isolation guarantee, re-proven on the concurrent path: the future's
    # exception is data, not a crash that hides the other runner's clean response.
    plan = _plan([_invocation("incident"), _invocation("docs", invocation_id="inv_docs")])
    resp = Executor(
        {AgentName.INCIDENT: _FailingRunner(), AgentName.DOCS: _RecordingRunner()}
    ).execute(plan, "q?")
    assert [r.invocation_id for r in resp.agent_responses] == ["inv_docs"]
    gap = next(g for g in resp.unresolved_gaps if g.kind_detail == "agent_invocation_failed")
    assert gap.suggested_agent is AgentName.INCIDENT
    assert resp.status is ResponseStatus.PARTIAL


# ------------------------------------------------------------------- Day 9: request tracing


class _FakeSpan:
    def __init__(self, name: str, input_text: str, metadata: dict[str, Any] | None) -> None:
        self.name = name
        self.input_text = input_text
        self.metadata = metadata or {}
        self.tool_calls: list[Any] = []
        self.ended: dict[str, Any] | None = None

    def record_tool_call(self, ref: Any) -> None:
        self.tool_calls.append(ref)

    def end(
        self,
        *,
        output: str | None,
        status: str,
        input_tokens: int,
        output_tokens: int,
        error: str | None = None,
    ) -> None:
        self.ended = {
            "output": output,
            "status": status,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "error": error,
        }


class _FakeTrace:
    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []
        self.end_calls: list[dict[str, Any]] = []

    @property
    def trace_id(self) -> str | None:
        return "trace_fake"

    def start_span(
        self, name: str, *, input_text: str, metadata: dict[str, Any] | None = None
    ) -> _FakeSpan:
        span = _FakeSpan(name, input_text, metadata)
        self.spans.append(span)  # list.append is atomic; worker threads share this safely
        return span

    def end(
        self,
        *,
        output: str | None,
        status: str,
        input_tokens: int,
        output_tokens: int,
        error: str | None = None,
    ) -> None:
        self.end_calls.append(
            {
                "output": output,
                "status": status,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "error": error,
            }
        )


class _FakeTracer:
    def __init__(self) -> None:
        self.traces: list[_FakeTrace] = []

    def start_request(self, name: str, *, request_id: str, query: str) -> _FakeTrace:
        trace = _FakeTrace()
        self.traces.append(trace)
        return trace

    def flush(self) -> None:
        return None


def test_executor_traces_one_span_per_agent_and_sets_trace_id():
    tracer = _FakeTracer()
    plan = _plan([_invocation("incident"), _invocation("docs", invocation_id="inv_docs")])
    resp = Executor(
        {
            AgentName.INCIDENT: _RecordingRunner(tokens=(120, 240)),
            AgentName.DOCS: _RecordingRunner(tokens=(75, 30)),
        },
        tracer=tracer,
    ).execute(plan, "Why is checkout failing, and what does the runbook say?")

    assert resp.trace_id == "trace_fake"
    (trace,) = tracer.traces
    by_name = {span.name: span for span in trace.spans}
    assert set(by_name) == {"agent:incident", "agent:docs"}
    # Each span carries the context the agent actually saw and the tokens it actually spent.
    incident = by_name["agent:incident"]
    assert incident.input_text == _CONTEXT
    assert incident.metadata["invocation_id"] == "inv_incident"
    assert incident.ended is not None
    assert incident.ended["status"] == "complete"
    assert (incident.ended["input_tokens"], incident.ended["output_tokens"]) == (120, 240)
    assert by_name["agent:docs"].ended is not None
    # A standalone execute owns the trace, so it closes it - once, with the full totals.
    (end,) = trace.end_calls
    assert end["status"] == "complete"
    assert (end["input_tokens"], end["output_tokens"]) == (120 + 75, 240 + 30)


def test_a_failed_agent_span_ends_with_the_error():
    tracer = _FakeTracer()
    plan = _plan([_invocation("incident")])
    Executor({AgentName.INCIDENT: _FailingRunner()}, tracer=tracer).execute(plan, "q?")

    (trace,) = tracer.traces
    # Day 14: the failure is retried once by the refinement loop (round 1), and the retry
    # fails identically - so two error spans, and no third, because an identical gap
    # coming back is not asked again.
    first, retry = trace.spans
    for span in (first, retry):
        assert span.ended is not None
        assert span.ended["status"] == "error"
        assert "scripted agent failure" in span.ended["error"]
    assert (first.metadata["round"], retry.metadata["round"]) == (0, 1)


def test_tool_call_refs_are_recorded_on_the_agent_span():
    ref = {
        "id": "tc_1",
        "tool_name": "search_corpus",
        "server": "aioc-docs",
        "started_at": "2026-08-08T14:20:00Z",
        "duration_ms": 42,
        "ok": True,
        "error_class": None,
        "tokens_returned": 850,
        "truncated": False,
    }

    class _ToolCallingRunner:
        def run(
            self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
        ) -> IncidentAgentResponse:
            return _incident_response(request_id, invocation_id, tool_calls=[ref])

    tracer = _FakeTracer()
    plan = _plan([_invocation("incident")])
    Executor({AgentName.INCIDENT: _ToolCallingRunner()}, tracer=tracer).execute(plan, "q?")

    (span,) = tracer.traces[0].spans
    (recorded,) = span.tool_calls
    assert recorded.tool_name == "search_corpus"
    assert recorded.duration_ms == 42


def test_respond_traces_the_planning_call_and_owns_the_trace():
    """One trace covers the whole request: a `plan` span with the planning call's own
    tokens, the agent spans, and exactly one trace close carrying the full totals."""
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
    tracer = _FakeTracer()

    resp = respond(
        "Why is checkout failing?",
        coordinator=coordinator,
        executor=Executor({AgentName.INCIDENT: _RecordingRunner(tokens=(120, 240))}),
        tracer=tracer,
    )

    assert resp.trace_id == "trace_fake"
    (trace,) = tracer.traces
    assert [span.name for span in trace.spans] == ["plan", "agent:incident"]
    plan_span = trace.spans[0]
    assert plan_span.ended is not None
    assert (plan_span.ended["input_tokens"], plan_span.ended["output_tokens"]) == (300, 180)
    assert "selected: incident" in plan_span.ended["output"]
    # respond owns the trace: closed exactly once, with planning + agent totals.
    (end,) = trace.end_calls
    assert (end["input_tokens"], end["output_tokens"]) == (300 + 120, 180 + 240)
    assert end["status"] == "complete"
