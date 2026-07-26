"""Contract conformance tests.

The anchor test validates the canonical worked example embedded in docs/CONTRACTS.md sec 8
against the models. That example is the frozen source of truth ("when prose in this
document and this example disagree, the example wins"), so if the models parse it and
round-trip it, they match the contract. The remaining tests pin the validated invariants
by asserting that violations are rejected.
"""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from aioc.contracts import (
    AgentInvocation,
    ApprovalRequirement,
    Assessment,
    Claim,
    CoordinatorResponse,
    Coverage,
    Evidence,
    Gap,
    IncidentAgentResponse,
    RecommendedAction,
    Severity,
    ToolCallRef,
    ToolError,
)

_CONTRACTS = Path(__file__).resolve().parents[1] / "docs" / "CONTRACTS.md"


def _worked_example() -> dict:
    text = _CONTRACTS.read_text(encoding="utf-8")
    for block in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL):
        if '"req_8f21"' in block:
            return json.loads(block)
    raise AssertionError("worked-example JSON (req_8f21) not found in CONTRACTS.md")


def _assessment(value: object, confidence: float, evidence: list[str]) -> dict:
    """A minimal Assessment payload for envelope tests."""
    return {
        "value": value,
        "confidence": confidence,
        "evidence": evidence,
        "reasoning": "r",
        "detail": None,
    }


# --------------------------------------------------------------------------- anchor


def test_worked_example_validates_and_round_trips():
    data = _worked_example()
    resp = CoordinatorResponse.model_validate(data)

    assert resp.request_id == "req_8f21"
    assert resp.status.value == "partial"
    assert [r.agent.value for r in resp.agent_responses] == ["incident", "docs"]
    assert [s.agent.value for s in resp.skipped_agents] == ["github", "deployment"]
    assert resp.refinement_rounds == 0

    # Discriminated union dispatched to the right concrete types.
    incident = resp.agent_responses[0]
    assert isinstance(incident, IncidentAgentResponse)
    assert incident.findings.failure_mode.value.value == "resource_exhaustion"

    # Re-validates after a JSON round-trip.
    again = CoordinatorResponse.model_validate(resp.model_dump(mode="json"))
    assert again.request_id == "req_8f21"


# --------------------------------------------------------- `other` + detail pairing


def test_other_requires_detail():
    with pytest.raises(ValidationError):
        Assessment[Severity](value=Severity.OTHER, confidence=0.8, evidence=["ev_1"], detail=None)


def test_non_other_forbids_detail():
    with pytest.raises(ValidationError):
        Assessment[Severity](value=Severity.SEV1, confidence=0.8, evidence=["ev_1"], detail="nope")


def test_other_with_detail_ok():
    a = Assessment[Severity](
        value=Severity.OTHER, confidence=0.8, evidence=["ev_1"], detail="sev0-ish"
    )
    assert a.detail == "sev0-ish"


# ------------------------------------------------------------- confidence floor


def test_low_confidence_forbids_value():
    with pytest.raises(ValidationError):
        Assessment[str](value="a root cause", confidence=0.1, evidence=[])


def test_low_confidence_null_value_ok():
    a = Assessment[str](value=None, confidence=0.1, evidence=[])
    assert a.value is None


# ------------------------------------------------------------- evidence + gaps


def test_evidence_other_needs_detail():
    with pytest.raises(ValidationError):
        Evidence(
            id="ev_1", source_type="other", source_type_detail=None, source_ref="x", excerpt="y"
        )


def test_gap_agent_needs_query():
    with pytest.raises(ValidationError):
        Gap(
            id="gap_1",
            description="d",
            kind="missing_data",
            suggested_agent="deployment",
            suggested_query=None,
            resolvable=True,
        )


# ----------------------------------------------------------- tool call / error taxonomy


def test_toolcall_error_class_must_match_ok():
    with pytest.raises(ValidationError):
        ToolCallRef(
            id="tc_1",
            tool_name="t",
            server="s",
            started_at="2026-07-23T14:07:03Z",
            duration_ms=10,
            ok=False,
            error_class=None,
            truncated=False,
        )


def test_only_transient_is_retryable():
    with pytest.raises(ValidationError):
        ToolError.model_validate(
            {
                "class": "validation",
                "code": "X",
                "message": "m",
                "retryable": True,
                "remediation": "r",
            }
        )


def test_transient_needs_retry_after():
    with pytest.raises(ValidationError):
        ToolError.model_validate(
            {
                "class": "transient",
                "code": "X",
                "message": "m",
                "retryable": True,
                "retry_after_ms": None,
                "remediation": "r",
            }
        )


def test_transient_ok():
    err = ToolError.model_validate(
        {
            "class": "transient",
            "code": "PROMETHEUS_TIMEOUT",
            "message": "m",
            "retryable": True,
            "retry_after_ms": 2000,
            "remediation": "retry",
        }
    )
    assert err.error_class.value == "transient"


# ------------------------------------------------------------- approval rule


def test_high_risk_requires_approval():
    with pytest.raises(ValidationError):
        RecommendedAction(
            id="act_1",
            action="a",
            rationale="r",
            risk="high",
            reversible=True,
            requires_approval=False,
        )


def test_deployment_approval_is_const_true():
    with pytest.raises(ValidationError):
        ApprovalRequirement(requires_approval=False, risk="low")


# ------------------------------------------------------------- docs invariants


def test_unsupported_claim_when_no_sources():
    with pytest.raises(ValidationError):
        Claim(id="claim_1", statement="s", supported=True, sources=[], confidence=0.1)


def test_coverage_must_partition_sub_questions():
    with pytest.raises(ValidationError):
        Coverage(
            sub_questions=["q1", "q2"],
            answered=["q1"],
            unanswered=[],
            documents_searched=1,
            documents_retrieved=1,
            documents_cited=1,
        )


# ------------------------------------------------------------- orchestration invariants


def test_context_passed_must_be_non_empty():
    with pytest.raises(ValidationError):
        AgentInvocation(
            invocation_id="inv_1",
            agent="incident",
            reason="r",
            mode="parallel",
            depends_on=[],
            context_passed="   ",
            round=0,
        )


def test_parallel_has_no_dependencies():
    with pytest.raises(ValidationError):
        AgentInvocation(
            invocation_id="inv_1",
            agent="incident",
            reason="r",
            mode="parallel",
            depends_on=["inv_0"],
            context_passed="ctx",
            round=0,
        )


def test_sequential_requires_dependencies():
    with pytest.raises(ValidationError):
        AgentInvocation(
            invocation_id="inv_2",
            agent="deployment",
            reason="r",
            mode="sequential",
            depends_on=[],
            context_passed="ctx",
            round=0,
        )


# ------------------------------------------------------------- envelope invariants


def _incident_response() -> dict:
    return {
        "schema_version": "1.0.0",
        "agent": "incident",
        "request_id": "req_1",
        "invocation_id": "inv_1",
        "status": "complete",
        "status_detail": None,
        "summary": "s",
        "findings": {
            "incident_window": {"start": "2026-07-23T13:52:00Z", "end": None},
            "affected_services": ["checkout-api"],
            "severity": _assessment("sev2", 0.84, ["ev_1"]),
            "failure_mode": _assessment("resource_exhaustion", 0.72, ["ev_1"]),
            "root_cause": _assessment("pool exhaustion", 0.68, ["ev_1"]),
            "contributing_factors": [],
            "timeline": [],
            "impact": {},
            "recommended_actions": [],
            "similar_incidents": [],
        },
        "evidence": [
            {
                "id": "ev_1",
                "source_type": "metric",
                "source_type_detail": None,
                "source_ref": "x",
                "excerpt": "y",
                "observed_at": None,
                "uri": None,
                "tool_call_id": None,
            }
        ],
        "gaps": [],
        "overall_confidence": 0.7,
        "tool_calls": [],
        "generated_at": "2026-07-23T14:07:16Z",
    }


def test_incident_baseline_valid():
    assert IncidentAgentResponse.model_validate(_incident_response()).agent.value == "incident"


def test_assessment_referencing_unknown_evidence_rejected():
    data = _incident_response()
    data["findings"]["severity"]["evidence"] = ["ev_missing"]
    with pytest.raises(ValidationError):
        IncidentAgentResponse.model_validate(data)


def test_complete_status_rejected_when_value_null():
    data = _incident_response()
    data["findings"]["root_cause"] = _assessment(None, 0.2, [])
    data["gaps"] = [
        {
            "id": "gap_1",
            "description": "d",
            "kind": "missing_data",
            "kind_detail": None,
            "blocks_field": "findings.root_cause.value",
            "suggested_agent": None,
            "suggested_query": None,
            "resolvable": False,
        }
    ]
    # value is null -> status must be partial or weaker.
    with pytest.raises(ValidationError):
        IncidentAgentResponse.model_validate(data)


def test_null_value_partial_with_gap_ok():
    data = _incident_response()
    data["status"] = "partial"
    data["findings"]["root_cause"] = _assessment(None, 0.2, [])
    data["gaps"] = [
        {
            "id": "gap_1",
            "description": "d",
            "kind": "missing_data",
            "kind_detail": None,
            "blocks_field": "findings.root_cause.value",
            "suggested_agent": "deployment",
            "suggested_query": "diff the release",
            "resolvable": True,
        }
    ]
    assert IncidentAgentResponse.model_validate(data).status.value == "partial"


def test_null_value_without_gap_rejected():
    data = _incident_response()
    data["status"] = "partial"
    data["findings"]["root_cause"] = _assessment(None, 0.2, [])
    with pytest.raises(ValidationError):
        IncidentAgentResponse.model_validate(data)


def test_unknown_field_is_rejected():
    data = _incident_response()
    data["surprise"] = True
    with pytest.raises(ValidationError):
        IncidentAgentResponse.model_validate(data)
