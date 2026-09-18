"""Day 14: the synthesis module on its own - the deterministic form, the prompt the model
form reads, and the model form against a scripted fake client (no network, no key).

The executor tests prove the seam; these prove each side of it. The grounding rule is the
one worth the most tests: a model-written synthesis may cite only evidence ids the agents
actually recorded, and every way of breaking that is a `SynthesisError` rather than a
response the coordinator would have to explain later.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock, ToolUseBlock

from aioc.contracts import AgentInvocation, Assessment, Gap, Intent
from aioc.coordinator.synthesis import (
    _EMIT_SCHEMA,
    EMIT_TOOL_NAME,
    SYNTHESIS_SYSTEM_PROMPT,
    ModelSynthesiser,
    SynthesisError,
    SynthesisRequest,
    check_grounding,
    deterministic,
    render_prompt,
)
from aioc.llm import LLMClient, LLMSettings, Usage
from tests.test_executor import _CONTEXT, _github_response, _incident_response

# ------------------------------------------------------------------------------ fixtures


def _inv(agent: str, invocation_id: str, round_number: int = 0) -> AgentInvocation:
    return AgentInvocation(
        invocation_id=invocation_id,
        agent=agent,  # type: ignore[arg-type]
        reason=f"{agent} contributes its findings",
        mode="parallel",  # type: ignore[arg-type]
        depends_on=[],
        context_passed=_CONTEXT,
        round=round_number,
    )


def _intent(value: str | None = "incident_diagnosis") -> Assessment[Intent]:
    return Assessment[Intent](
        value=value,  # type: ignore[arg-type]
        confidence=0.9 if value else 0.1,
        evidence=[],
        reasoning="from the wording",
        detail=None,
    )


def _open_gap() -> Gap:
    return Gap(
        id="gap_open",
        description="No deploy record exists for the window.",
        kind="missing_data",  # type: ignore[arg-type]
        kind_detail=None,
        blocks_field=None,
        suggested_agent=None,
        suggested_query=None,
        resolvable=False,
    )


def _request(
    *,
    rounds: int = 0,
    with_second_round: bool = False,
    unresolved: list[Gap] | None = None,
) -> SynthesisRequest:
    reports = [
        (_inv("incident", "inv_incident"), _incident_response("req_1", "inv_incident")),
        (
            _inv("github", "inv_gh"),
            _github_response("req_1", "inv_gh").model_copy(update={"overall_confidence": 0.9}),
        ),
    ]
    if with_second_round:
        reports.append((_inv("incident", "inv_r1", 1), _incident_response("req_1", "inv_r1")))
    return SynthesisRequest(
        query="Why is checkout failing, and did anything ship?",
        intent=_intent(),
        reports=reports,
        execution_gaps=[],
        unresolved_gaps=unresolved if unresolved is not None else [],
        refinement_rounds=rounds,
    )


# ------------------------------------------------------------------------- deterministic


def test_deterministic_adopts_the_highest_confidence_report_and_its_own_evidence():
    result = deterministic(_request())
    github = _github_response("req_1", "inv_gh")
    assert result.answer.value == github.summary
    assert result.answer.confidence == 0.9
    assert set(result.answer.evidence) <= {e.id for e in github.evidence}
    assert result.answer.evidence  # a confident answer cites
    assert "- incident (complete, confidence 0.72): " in result.synthesis
    assert "- github (complete, confidence 0.90): " in result.synthesis
    assert "round" not in result.synthesis  # no refinement ran, so no round labels


def test_deterministic_labels_refinement_rounds_from_the_invocation_not_by_guessing():
    result = deterministic(_request(rounds=1, with_second_round=True))
    lines = result.synthesis.splitlines()
    assert lines[0].endswith("after 1 refinement round(s)):")
    assert sum(", round 1): " in line for line in lines) == 1
    assert "- incident (complete, confidence 0.72): " in result.synthesis  # round 0 unlabelled


def test_deterministic_caps_an_uncited_answer_below_the_evidenced_band():
    thin = _incident_response(
        "req_1", "inv_incident", status="insufficient_evidence", with_evidence=False
    ).model_copy(update={"overall_confidence": 0.8})
    request = SynthesisRequest(
        query="q?",
        intent=_intent(),
        reports=[(_inv("incident", "inv_incident"), thin)],
        execution_gaps=[],
        unresolved_gaps=list(thin.gaps),
        refinement_rounds=0,
    )
    result = deterministic(request)
    assert result.answer.evidence == []
    assert result.answer.confidence == 0.49
    assert "capped below 0.5" in (result.answer.reasoning or "")


def test_deterministic_with_no_reports_is_a_null_answer_not_a_guess():
    request = SynthesisRequest(
        query="q?",
        intent=_intent(None),
        reports=[],
        execution_gaps=[_open_gap()],
        unresolved_gaps=[_open_gap()],
        refinement_rounds=0,
    )
    result = deterministic(request)
    assert result.answer.value is None and result.answer.confidence == 0.0
    assert result.synthesis.startswith("No selected agent could be executed")
    assert "- not executed: No deploy record" in result.synthesis


# ------------------------------------------------------------------------- the prompt


def test_the_prompt_carries_the_query_each_digest_with_its_round_and_the_open_gaps():
    prompt = render_prompt(_request(rounds=1, with_second_round=True, unresolved=[_open_gap()]))
    assert "<query>\nWhy is checkout failing, and did anything ship?\n</query>" in prompt
    assert "<intent value='incident_diagnosis' confidence=0.90 />" in prompt
    assert "<refinement_rounds>1</refinement_rounds>" in prompt
    assert prompt.count("<handoff from=") == 3
    assert "(refinement round 1, invocation inv_r1)" in prompt
    assert '<handoff from="github" invocation_id="inv_gh"' in prompt
    assert "- gap_open [unresolvable]: No deploy record exists for the window." in prompt
    # The evidence ids the model may cite are visible in the digests, nothing else is.
    assert "ev_1" in prompt


def test_the_prompt_says_when_nothing_is_open():
    assert "No gaps remain open." in render_prompt(_request())


# ------------------------------------------------------------------------- grounding


def test_grounding_rejects_an_evidence_id_no_agent_carries():
    answer = Assessment[str](value="x", confidence=0.6, evidence=["ev_ghost"], reasoning="r")
    with pytest.raises(SynthesisError, match="ev_ghost"):
        check_grounding(answer, _request().responses)


def test_grounding_rejects_a_confident_answer_that_cites_nothing():
    answer = Assessment[str](value="x", confidence=0.5, evidence=[], reasoning="r")
    with pytest.raises(SynthesisError, match="cites no evidence"):
        check_grounding(answer, _request().responses)


def test_grounding_accepts_a_hedged_uncited_answer_and_a_cited_confident_one():
    check_grounding(
        Assessment[str](value="maybe", confidence=0.4, evidence=[], reasoning="r"),
        _request().responses,
    )
    check_grounding(
        Assessment[str](value="yes", confidence=0.8, evidence=["ev_1"], reasoning="r"),
        _request().responses,
    )


# ------------------------------------------------------------------- the model form


class _FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake client ran out of scripted responses")
        return self._responses.pop(0)


def _synthesiser(responses: list[Any]) -> tuple[ModelSynthesiser, _FakeMessages]:
    fake = SimpleNamespace(messages=_FakeMessages(responses))
    client = LLMClient(LLMSettings(model="claude-sonnet-5", max_tokens=2048), client=fake)  # type: ignore[arg-type]
    return ModelSynthesiser(client), fake.messages


def _tool_use(payload: dict[str, Any], *, stop_reason: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-sonnet-5",
        content=[ToolUseBlock(type="tool_use", id="toolu_1", name=EMIT_TOOL_NAME, input=payload)],
        usage=SimpleNamespace(input_tokens=640, output_tokens=120),
    )


def _payload(evidence: list[str], confidence: float = 0.7) -> dict[str, Any]:
    # Flat, as the model is asked for it (see ModelSynthesis for the live measurement).
    return {
        "synthesis": "payments-api latency is degrading checkout-api; PR #412 shipped a retry.",
        "answer": "payments-api latency is the origin and PR #412 is the suspect change.",
        "confidence": confidence,
        "evidence": evidence,
        "reasoning": "The incident report and the GitHub report agree on the window.",
    }


def test_the_model_form_is_one_forced_call_over_the_digests_and_counts_its_tokens():
    synthesiser, messages = _synthesiser([_tool_use(_payload(["ev_1"]))])
    usage = Usage()
    result = synthesiser.synthesise(_request(), usage=usage)

    assert result.answer.value is not None and result.answer.evidence == ["ev_1"]
    assert result.synthesis.startswith("payments-api latency")
    assert (usage.input_tokens, usage.output_tokens) == (640, 120)
    (call,) = messages.calls
    assert call["system"] == SYNTHESIS_SYSTEM_PROMPT
    assert call["tool_choice"] == {"type": "tool", "name": EMIT_TOOL_NAME}
    assert [t["name"] for t in call["tools"]] == [EMIT_TOOL_NAME]
    assert call["messages"][0]["content"] == render_prompt(_request())


def test_an_ungrounded_model_synthesis_is_rejected_after_its_tokens_are_counted():
    synthesiser, _ = _synthesiser([_tool_use(_payload(["ev_1", "ev_invented"]))])
    usage = Usage()
    with pytest.raises(SynthesisError, match="ev_invented"):
        synthesiser.synthesise(_request(), usage=usage)
    assert usage.input_tokens == 640  # a rejected call still cost


def test_a_truncated_synthesis_reports_itself_before_validation():
    synthesiser, _ = _synthesiser([_tool_use(_payload(["ev_1"]), stop_reason="max_tokens")])
    with pytest.raises(SynthesisError, match="max_tokens"):
        synthesiser.synthesise(_request(), usage=Usage())


def test_a_reply_without_the_tool_call_is_rejected():
    prose = SimpleNamespace(
        stop_reason="end_turn",
        model="claude-sonnet-5",
        content=[TextBlock(type="text", text="Here is my synthesis...")],
        usage=SimpleNamespace(input_tokens=600, output_tokens=40),
    )
    synthesiser, _ = _synthesiser([prose])
    with pytest.raises(SynthesisError, match="did not call"):
        synthesiser.synthesise(_request(), usage=Usage())


def test_a_payload_that_breaks_the_assessment_invariants_is_rejected():
    # confidence < 0.25 with a value set: the contract's own Assessment rule.
    synthesiser, _ = _synthesiser([_tool_use(_payload(["ev_1"], confidence=0.1))])
    with pytest.raises(SynthesisError, match="failed validation"):
        synthesiser.synthesise(_request(), usage=Usage())


def test_no_reports_means_no_call():
    synthesiser, messages = _synthesiser([])  # a call would run the fake out of steps
    request = SynthesisRequest(
        query="q?",
        intent=_intent(None),
        reports=[],
        execution_gaps=[_open_gap()],
        unresolved_gaps=[_open_gap()],
        refinement_rounds=0,
    )
    result = synthesiser.synthesise(request, usage=Usage())
    assert result.answer.value is None and messages.calls == []


def test_the_emit_schema_is_flat_and_carries_the_grounding_guidance_on_the_fields():
    props = _EMIT_SCHEMA["properties"]
    assert set(props) == {"synthesis", "answer", "confidence", "evidence", "reasoning"}
    assert "$defs" not in _EMIT_SCHEMA  # no nested object for the model to mis-serialise
    assert "add no facts" in props["synthesis"]["description"]
    assert "Never invent an id" in props["evidence"]["description"]
    assert "do not nest" in _EMIT_SCHEMA["description"]
    assert _EMIT_SCHEMA["additionalProperties"] is False


def test_a_nested_answer_object_is_rejected_not_silently_accepted():
    # The shape the first live run came back with, in spirit: `answer` not a plain string.
    payload = _payload(["ev_1"])
    payload["answer"] = {"value": payload["answer"], "confidence": 0.7}
    synthesiser, _ = _synthesiser([_tool_use(payload)])
    with pytest.raises(SynthesisError, match="failed validation"):
        synthesiser.synthesise(_request(), usage=Usage())
