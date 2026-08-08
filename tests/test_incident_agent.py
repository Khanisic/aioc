"""Unit tests for the Incident agent skeleton (Day 3).

Same pattern as the harness tests: the Anthropic client is a scripted fake injected through
`LLMClient`, so the tests are deterministic, offline, and key-free. What is under test is the
agent's plumbing - explicit context passing, the system prompt, prose extraction, accounting -
not the model's judgment (that is the Day 19 eval harness's job).
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock, ToolUseBlock
from pydantic import ValidationError

from aioc.agents import (
    EMIT_TOOL_NAME,
    INCIDENT_STRUCTURED_SYSTEM_PROMPT,
    INCIDENT_SYSTEM_PROMPT,
    IncidentAgent,
    IncidentAgentError,
    IncidentReport,
)

# The annotation layer is internal to the agent, but its drift guard is exactly the kind of
# thing that must have a test, so it is imported directly rather than through the package API.
from aioc.agents.incident import _EMIT_SCHEMA, _apply_guidance
from aioc.contracts import AgentName, FailureMode, IncidentAgentResponse, ResponseStatus, Severity
from aioc.llm import LLMClient, LLMSettings

# --------------------------------------------------------------------------- fakes


def _message(text: str, *, in_tokens: int = 40, out_tokens: int = 25) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[TextBlock(type="text", text=text)],
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
    )


class _FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake client ran out of scripted responses")
        return self._responses.pop(0)


class _FakeAnthropic:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _FakeMessages(responses)


def _agent(responses: list[Any]) -> tuple[IncidentAgent, _FakeMessages]:
    fake = _FakeAnthropic(responses)
    settings = LLMSettings(model="claude-opus-5", max_tokens=1024)
    client = LLMClient(settings, client=fake)  # type: ignore[arg-type]
    return IncidentAgent(client), fake.messages


_CONTEXT = (
    "Service: payments-api. Since 14:02 UTC error_rate rose from 0.1% to 7.4%; "
    "p99 latency 2100ms. No deploys in the window."
)


# --------------------------------------------------------------------------- behaviour


def test_returns_prose_and_accounting():
    agent, _ = _agent([_message("**Summary** - payments-api is degraded.")])
    result = agent.investigate("Why is payments-api slow?", context=_CONTEXT)
    assert result.text == "**Summary** - payments-api is degraded."
    assert result.stop_reason == "end_turn"
    assert result.model == "claude-opus-5"
    assert (result.input_tokens, result.output_tokens) == (40, 25)


def test_context_is_passed_explicitly_in_the_prompt():
    agent, messages = _agent([_message("ok")])
    agent.investigate("What broke?", context=_CONTEXT)
    call = messages.calls[0]
    prompt = call["messages"][0]["content"]
    # The literal context block appears in the prompt - nothing is assumed inherited.
    assert _CONTEXT in prompt
    assert "<context>" in prompt and "</context>" in prompt
    assert "What broke?" in prompt


def test_system_prompt_carries_the_graded_behaviours():
    agent, messages = _agent([_message("ok")])
    agent.investigate("What broke?", context=_CONTEXT)
    system = messages.calls[0]["system"]
    assert system == INCIDENT_SYSTEM_PROMPT
    # The three behaviours Day 3 is graded on: SRE persona, evidence citation, confidence.
    assert "Site" in system and "Reliability" in system
    assert "evidence" in system.lower()
    assert "0.90-1.00" in system  # the CONTRACTS.md sec 2.1 band table, verbatim
    assert "below 0.25" in system.lower()


def test_empty_context_is_rejected():
    agent, _ = _agent([])
    with pytest.raises(ValueError, match="context must be non-empty"):
        agent.investigate("What broke?", context="   ")


def test_empty_query_is_rejected():
    agent, _ = _agent([])
    with pytest.raises(ValueError, match="query must be non-empty"):
        agent.investigate("", context=_CONTEXT)


def test_multiple_text_blocks_are_concatenated():
    resp = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            TextBlock(type="text", text="part one. "),
            TextBlock(type="text", text="part two."),
        ],
        model="claude-opus-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    agent, _ = _agent([resp])
    result = agent.investigate("q", context=_CONTEXT)
    assert result.text == "part one. part two."


# --------------------------------------------------------- structured output (Day 4, diagnose)


def _tool_use_message(
    payload: dict[str, Any],
    *,
    tool_name: str = EMIT_TOOL_NAME,
    stop_reason: str = "tool_use",
) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-opus-5",
        content=[ToolUseBlock(type="tool_use", id="toolu_1", name=tool_name, input=payload)],
        usage=SimpleNamespace(input_tokens=120, output_tokens=240),
    )


# A valid analytic payload: a populated Assessment (severity), a null-value Assessment
# (root_cause) with its matching Gap, resolving evidence ids, and status 'partial' because a
# value is null. This is exactly what the model would put in the emit tool call.
_STRUCTURED_PAYLOAD: dict[str, Any] = {
    "status": "partial",
    "status_detail": None,
    "summary": "payments-api errors and latency rose sharply from 14:02 UTC; a memory leak "
    "is the leading hypothesis.",
    "findings": {
        "incident_window": {"start": "2026-07-26T14:02:00Z", "end": None},
        "affected_services": ["payments-api", "checkout-api"],
        "severity": {
            "value": "sev2",
            "confidence": 0.82,
            "evidence": ["ev_1"],
            "reasoning": "Customer-facing errors up ~70x with no total outage.",
            "detail": None,
        },
        "failure_mode": {
            "value": "resource_exhaustion",
            "confidence": 0.66,
            "evidence": ["ev_1", "ev_2"],
            "reasoning": "Resident memory climbed ~8x while the error ratio rose.",
            "detail": None,
        },
        "root_cause": {
            "value": None,
            "confidence": 0.2,
            "evidence": [],
            "reasoning": "The leak is visible but the offending allocation is not identified.",
            "detail": None,
        },
        "contributing_factors": [],
        "timeline": [
            {
                "id": "evt_1",
                "at": "2026-07-26T14:02:00Z",
                "service": "payments-api",
                "kind": "metric_threshold",
                "kind_detail": None,
                "description": "5xx ratio crossed 1%.",
                "severity": "sev2",
                "evidence_id": "ev_1",
            }
        ],
        "impact": {
            "error_rate_before": 0.001,
            "error_rate_after": 0.074,
            "p50_latency_ms_before": None,
            "p50_latency_ms_after": None,
            "p99_latency_ms_before": 120,
            "p99_latency_ms_after": 2100,
            "requests_affected": None,
            "duration_seconds": None,
        },
        "recommended_actions": [
            {
                "id": "act_1",
                "action": "Restart payments-api to reclaim leaked memory.",
                "rationale": "Immediate mitigation while the leak is root-caused.",
                "risk": "medium",
                "risk_detail": None,
                "reversible": True,
                "requires_approval": True,
                "target_service": "payments-api",
                "command": None,
            }
        ],
        "similar_incidents": [],
    },
    "evidence": [
        {
            "id": "ev_1",
            "source_type": "metric",
            "source_type_detail": None,
            "source_ref": 'http_requests_total{service="payments-api",status="500"}',
            "excerpt": "5xx ratio 0.1% at 14:00 -> 7.4% at 14:10.",
            "observed_at": "2026-07-26T14:10:00Z",
            "uri": None,
            "tool_call_id": None,
        },
        {
            "id": "ev_2",
            "source_type": "metric",
            "source_type_detail": None,
            "source_ref": 'process_resident_memory_bytes{service="payments-api"}',
            "excerpt": "180MB at 12:00 -> 1.4GB at 14:10.",
            "observed_at": "2026-07-26T14:10:00Z",
            "uri": None,
            "tool_call_id": None,
        },
    ],
    "gaps": [
        {
            "id": "gap_1",
            "description": "The leaking allocation cannot be identified from metrics alone.",
            "kind": "missing_data",
            "kind_detail": None,
            "blocks_field": "findings.root_cause.value",
            "suggested_agent": "github",
            "suggested_query": "Inspect recent payments-api changes for unbounded caches.",
            "resolvable": True,
        }
    ],
    "overall_confidence": 0.6,
}


def test_emit_schema_advertises_the_model_owned_fields_only():
    props = IncidentReport.model_json_schema()["properties"]
    # The model fills analytic fields...
    assert {"status", "summary", "findings", "evidence", "gaps", "overall_confidence"} <= set(props)
    # ...but never the envelope plumbing the caller/harness owns.
    for plumbing in (
        "schema_version",
        "agent",
        "request_id",
        "invocation_id",
        "generated_at",
        "tool_calls",
    ):
        assert plumbing not in props


def test_diagnose_returns_a_validated_incident_response():
    agent, _ = _agent([_tool_use_message(_STRUCTURED_PAYLOAD)])
    resp = agent.diagnose("Why is payments-api failing?", context=_CONTEXT)

    assert isinstance(resp, IncidentAgentResponse)
    assert resp.agent is AgentName.INCIDENT
    assert resp.status is ResponseStatus.PARTIAL
    assert resp.schema_version == "1.0.0"  # contract default, not model-supplied
    assert resp.findings.severity.value is Severity.SEV2
    assert resp.findings.failure_mode.value is FailureMode.RESOURCE_EXHAUSTION
    # The null analytic value survived round-trip and its Gap is present.
    assert resp.findings.root_cause.value is None
    assert any(g.blocks_field == "findings.root_cause.value" for g in resp.gaps)
    # No tools ran on Day 4, so the audit list is empty (Day 5 populates it).
    assert resp.tool_calls == []
    assert isinstance(resp.generated_at, datetime)


def test_diagnose_forces_the_emit_tool_with_the_structured_prompt():
    agent, messages = _agent([_tool_use_message(_STRUCTURED_PAYLOAD)])
    agent.diagnose("What broke?", context=_CONTEXT)
    call = messages.calls[0]

    assert call["system"] == INCIDENT_STRUCTURED_SYSTEM_PROMPT
    assert call["tool_choice"] == {"type": "tool", "name": EMIT_TOOL_NAME}
    assert [t["name"] for t in call["tools"]] == [EMIT_TOOL_NAME]
    # Context is still passed explicitly - the Day 3 invariant holds on the structured path.
    assert _CONTEXT in call["messages"][0]["content"]


def test_diagnose_generates_ids_when_none_are_supplied():
    agent, _ = _agent([_tool_use_message(_STRUCTURED_PAYLOAD)])
    resp = agent.diagnose("q", context=_CONTEXT)
    assert resp.request_id.startswith("req_")
    assert resp.invocation_id.startswith("inv_")


def test_diagnose_passes_supplied_ids_through():
    agent, _ = _agent([_tool_use_message(_STRUCTURED_PAYLOAD)])
    resp = agent.diagnose("q", context=_CONTEXT, request_id="req_abc", invocation_id="inv_xyz")
    assert (resp.request_id, resp.invocation_id) == ("req_abc", "inv_xyz")


def test_diagnose_raises_when_model_skips_the_tool():
    # end_turn with prose instead of the forced tool call.
    agent, _ = _agent([_message("I would investigate, but I did not call the tool.")])
    with pytest.raises(IncidentAgentError, match="did not call"):
        agent.diagnose("q", context=_CONTEXT)


def test_diagnose_rejects_a_payload_that_violates_the_contract():
    # A null root_cause value with its Gap removed - the envelope invariant must reject it,
    # proving the output is genuinely schema-validated (the Day 4 point).
    broken = {**_STRUCTURED_PAYLOAD, "gaps": []}
    agent, _ = _agent([_tool_use_message(broken)])
    with pytest.raises(ValidationError):
        agent.diagnose("q", context=_CONTEXT)


def test_diagnose_rejects_empty_context():
    agent, _ = _agent([])
    with pytest.raises(ValueError, match="context must be non-empty"):
        agent.diagnose("What broke?", context="   ")


def test_diagnose_names_truncation_instead_of_blaming_the_model():
    # A report cut off at the token ceiling still carries a tool_use block holding whatever
    # parsed, so validation would report a missing required field and send the reader hunting
    # for a prompt bug. Measured against the live API: Opus fills 4096 output tokens on this
    # schema without finishing, so this path is real, not hypothetical.
    truncated = {k: v for k, v in _STRUCTURED_PAYLOAD.items() if k != "overall_confidence"}
    agent, _ = _agent([_tool_use_message(truncated, stop_reason="max_tokens")])
    with pytest.raises(IncidentAgentError, match="truncated"):
        agent.diagnose("q", context=_CONTEXT)


# ------------------------------------------------------- schema guidance (annotation layer)


def test_emit_schema_states_the_other_detail_rule_on_every_detail_field():
    # The rule lives in the schema, not only the prompt: measured against the live API, both
    # Haiku 4.5 and Sonnet 5 filled every `*_detail` field regardless of its enum when the
    # schema was silent about the pairing. Each of these must say so on the field itself.
    defs = _EMIT_SCHEMA["$defs"]
    paired = [
        ("Gap", "kind_detail"),
        ("RecommendedAction", "risk_detail"),
        ("TimelineEvent", "kind_detail"),
        ("Evidence", "source_type_detail"),
        ("Assessment_Severity_", "detail"),
        ("Assessment_FailureMode_", "detail"),
    ]
    for def_name, field in paired:
        description = defs[def_name]["properties"][field].get("description", "")
        assert "other" in description, f"{def_name}.{field} does not state the `other` rule"
    # status_detail is on the root object rather than a $def.
    assert "other" in _EMIT_SCHEMA["properties"]["status_detail"]["description"]


def test_emit_schema_tells_the_model_not_to_nest_the_payload():
    # Sonnet wrapped the whole report in a `report` key when the top-level description was the
    # developer-facing docstring; the replacement says explicitly that these are the arguments.
    assert "do not nest" in _EMIT_SCHEMA["description"].lower()


def test_schema_guidance_fails_loudly_when_a_contract_field_is_renamed():
    # The guidance is keyed by field name, so a rename in aioc.contracts would silently drop the
    # rule a model depends on. It must break at import instead.
    with pytest.raises(RuntimeError, match="out of sync"):
        _apply_guidance({"properties": {}, "$defs": {}})
