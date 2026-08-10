"""Day 6: coordinator intent classification and dynamic agent selection.

Driven by a scripted fake client - no API key, no network, no cost. The live counterpart is
`scripts/check_agent_selection.py`.

Most of these are negative tests, and that is the point. Dynamic selection and explicit context
passing are the project's most-tested orchestration facts (CCA-F Domain 1), and a plan that
"usually" gets them right is not evidence. Each rule below has a test proving the violation is
rejected rather than merely discouraged by the prompt.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock, ToolUseBlock
from pydantic import ValidationError

from aioc.contracts import AgentName, Intent, InvocationMode
from aioc.coordinator import (
    SELECT_TOOL_NAME,
    SELECTION_SYSTEM_PROMPT,
    Coordinator,
    CoordinatorError,
    SelectionPlan,
)
from aioc.llm import LLMClient, LLMSettings

# --------------------------------------------------------------------------------- fakes


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


def _tool_use(payload: dict[str, Any], *, stop_reason: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        model="claude-sonnet-5",
        content=[ToolUseBlock(type="tool_use", id="toolu_1", name=SELECT_TOOL_NAME, input=payload)],
        usage=SimpleNamespace(input_tokens=300, output_tokens=180),
    )


def _coordinator(responses: list[Any]) -> tuple[Coordinator, _FakeMessages]:
    fake = _FakeAnthropic(responses)
    client = LLMClient(LLMSettings(model="claude-sonnet-5", max_tokens=2048), client=fake)  # type: ignore[arg-type]
    return Coordinator(client), fake.messages


def _assessment(value: str | None, confidence: float) -> dict[str, Any]:
    return {
        "value": value,
        "confidence": confidence,
        "evidence": [],
        "reasoning": "derived from the query wording",
        "detail": None,
    }


def _invocation(agent: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "invocation_id": f"inv_{agent}",
        "agent": agent,
        "reason": f"{agent} will establish the operational facts behind the symptom",
        "mode": "parallel",
        "depends_on": [],
        "context_passed": (
            "checkout-api 5xx rose from 0.1% to 2.1% between 14:00 and 14:15 UTC while "
            "payments-api p99 went 120ms -> 2100ms. inventory-api is nominal. No deploys in "
            "24h. Determine whether payments-api is the origin."
        ),
        "round": 0,
    }
    base.update(over)
    return base


def _skip(agent: str) -> dict[str, Any]:
    return {
        "agent": agent,
        "reason": f"the query names no {agent} artefact and needs no {agent} lookup to answer",
    }


# One selected agent, three skipped: the shape a narrow query should produce.
_NARROW_PLAN: dict[str, Any] = {
    "intent": _assessment("incident_diagnosis", 0.92),
    "selected_agents": [_invocation("incident")],
    "skipped_agents": [_skip("docs"), _skip("github"), _skip("deployment")],
    "gaps": [],
}


# ------------------------------------------------------------------------------ happy path


def test_plan_returns_a_validated_selection_plan():
    coordinator, _ = _coordinator([_tool_use(_NARROW_PLAN)])
    plan = coordinator.plan("Checkout success rate is dropping. What is going on?")

    assert isinstance(plan, SelectionPlan)
    assert plan.intent.value is Intent.INCIDENT_DIAGNOSIS
    assert [i.agent for i in plan.selected_agents] == [AgentName.INCIDENT]
    assert {s.agent for s in plan.skipped_agents} == {
        AgentName.DOCS,
        AgentName.GITHUB,
        AgentName.DEPLOYMENT,
    }


def test_plan_forces_the_select_tool_with_the_selection_prompt():
    coordinator, messages = _coordinator([_tool_use(_NARROW_PLAN)])
    coordinator.plan("Why is checkout slow?")
    call = messages.calls[0]

    assert call["system"] == SELECTION_SYSTEM_PROMPT
    assert call["tool_choice"] == {"type": "tool", "name": SELECT_TOOL_NAME}
    assert [t["name"] for t in call["tools"]] == [SELECT_TOOL_NAME]


def test_situation_is_passed_to_the_model_but_not_auto_forwarded_to_agents():
    # The asymmetry is the whole point of explicit context passing: the coordinator sees the
    # situation, and must decide for itself what each agent is told about it.
    coordinator, messages = _coordinator([_tool_use(_NARROW_PLAN)])
    plan = coordinator.plan("Why is checkout slow?", situation="payments-api p99 is 2100ms")

    assert "payments-api p99 is 2100ms" in messages.calls[0]["messages"][0]["content"]
    assert all("2100ms" in i.context_passed for i in plan.selected_agents) is True


def test_parallel_and_sequential_groups_are_separable():
    plan = SelectionPlan.model_validate(
        {
            "intent": _assessment("mixed", 0.7),
            "selected_agents": [
                _invocation("github", invocation_id="inv_gh"),
                _invocation(
                    "deployment",
                    invocation_id="inv_dep",
                    mode="sequential",
                    depends_on=["inv_gh"],
                    context_passed=(
                        "Diff the release containing the PR github reports, once it has."
                    ),
                ),
            ],
            "skipped_agents": [_skip("incident"), _skip("docs")],
            "gaps": [],
        }
    )
    assert [i.agent for i in plan.parallel_group] == [AgentName.GITHUB]
    assert [i.agent for i in plan.sequential_chain] == [AgentName.DEPLOYMENT]
    assert plan.sequential_chain[0].mode is InvocationMode.SEQUENTIAL


# ----------------------------------------------------------- dynamic selection, enforced


def test_plan_rejects_an_agent_that_is_neither_selected_nor_skipped():
    # The failure this catches is not "selected too many" but "forgot to account for one",
    # which is what makes the skip list auditable rather than decorative.
    broken = {**_NARROW_PLAN, "skipped_agents": [_skip("docs"), _skip("github")]}
    with pytest.raises(ValidationError, match="either selected or skipped"):
        SelectionPlan.model_validate(broken)


def test_plan_rejects_an_agent_that_is_both_selected_and_skipped():
    broken = {
        **_NARROW_PLAN,
        "skipped_agents": [_skip("incident"), _skip("docs"), _skip("github"), _skip("deployment")],
    }
    with pytest.raises(ValidationError, match="both selected and skipped"):
        SelectionPlan.model_validate(broken)


def test_plan_rejects_the_same_agent_invoked_twice():
    broken = {
        **_NARROW_PLAN,
        "selected_agents": [
            _invocation("incident"),
            _invocation("incident", invocation_id="inv_2"),
        ],
    }
    with pytest.raises(ValidationError, match="more than once"):
        SelectionPlan.model_validate(broken)


def test_plan_rejects_invoking_nothing():
    # `intent` is deliberately a real member rather than `other` here: `other` without a
    # detail string trips the contract's pairing rule first and masks the rule under test.
    broken = {
        "intent": _assessment("incident_diagnosis", 0.3),
        "selected_agents": [],
        "skipped_agents": [_skip(a) for a in ("incident", "docs", "github", "deployment")],
        "gaps": [],
    }
    with pytest.raises(ValidationError, match="at least one agent"):
        SelectionPlan.model_validate(broken)


def test_other_intent_requires_a_detail_string():
    # The `other`-plus-detail pattern, on the coordinator's own enum this time.
    broken = {**_NARROW_PLAN, "intent": _assessment("other", 0.4)}
    with pytest.raises(ValidationError, match="detail must be non-null"):
        SelectionPlan.model_validate(broken)


def test_selecting_all_four_is_structurally_legal_but_leaves_skipped_empty():
    # Not an error - some queries genuinely need everything. It is the signal to watch: an
    # empty skipped_agents on a *typical* query means selection is not discriminating.
    plan = SelectionPlan.model_validate(
        {
            "intent": _assessment("mixed", 0.6),
            "selected_agents": [
                _invocation(a) for a in ("incident", "docs", "github", "deployment")
            ],
            "skipped_agents": [],
            "gaps": [],
        }
    )
    assert plan.skipped_agents == []
    assert len(plan.selected_agents) == 4


# ------------------------------------------------------- explicit context passing, enforced


def test_plan_rejects_empty_context_passed():
    # Enforced by the contract model itself: an empty value means context was assumed
    # inherited, which is the exact failure this project demonstrates the absence of.
    broken = {**_NARROW_PLAN, "selected_agents": [_invocation("incident", context_passed="   ")]}
    with pytest.raises(ValidationError, match="context_passed must be non-empty"):
        SelectionPlan.model_validate(broken)


def test_plan_rejects_context_that_only_restates_the_query():
    # The subtler failure the contract cannot catch: non-empty, but telling the subagent
    # nothing it would not have had anyway. Inheritance with extra steps.
    query = "Why is checkout-api returning 502s?"
    echoed = {**_NARROW_PLAN, "selected_agents": [_invocation("incident", context_passed=query)]}
    coordinator, _ = _coordinator([_tool_use(echoed)])
    with pytest.raises(ValueError, match="only restates the query"):
        coordinator.plan(query)


def test_context_that_adds_facts_is_accepted_even_if_it_quotes_the_query():
    query = "Why is checkout-api returning 502s?"
    enriched = {
        **_NARROW_PLAN,
        "selected_agents": [
            _invocation(
                "incident",
                context_passed=(
                    f"{query} Relevant facts: payments-api p99 rose 120ms -> 2100ms in the same "
                    "window, inventory-api is nominal, and no deploys landed in 24h. checkout-api "
                    "returns 502 when either downstream fails, so establish which one."
                ),
            )
        ],
    }
    coordinator, _ = _coordinator([_tool_use(enriched)])
    plan = coordinator.plan(query)
    assert "2100ms" in plan.selected_agents[0].context_passed


# ------------------------------------------------------ parallel/sequential, enforced


def test_plan_rejects_parallel_with_dependencies():
    broken = {
        **_NARROW_PLAN,
        "selected_agents": [_invocation("incident", mode="parallel", depends_on=["inv_x"])],
    }
    with pytest.raises(ValidationError, match="depends_on must be empty"):
        SelectionPlan.model_validate(broken)


def test_plan_rejects_sequential_without_dependencies():
    broken = {
        **_NARROW_PLAN,
        "selected_agents": [_invocation("incident", mode="sequential", depends_on=[])],
    }
    with pytest.raises(ValidationError, match="depends_on must be non-empty"):
        SelectionPlan.model_validate(broken)


def test_plan_rejects_a_dependency_on_an_invocation_outside_the_plan():
    # Without this the Day 7 executor deadlocks waiting for an invocation that never runs.
    broken = {
        **_NARROW_PLAN,
        "selected_agents": [
            _invocation("deployment", mode="sequential", depends_on=["inv_never_scheduled"])
        ],
        "skipped_agents": [_skip("incident"), _skip("docs"), _skip("github")],
    }
    with pytest.raises(ValidationError, match="unknown invocation_id"):
        SelectionPlan.model_validate(broken)


def test_plan_rejects_a_self_dependency():
    broken = {
        **_NARROW_PLAN,
        "selected_agents": [
            _invocation("incident", invocation_id="inv_a", mode="sequential", depends_on=["inv_a"])
        ],
    }
    with pytest.raises(ValidationError, match="depends on itself"):
        SelectionPlan.model_validate(broken)


# ------------------------------------------------------------------- honest classification


def test_plan_rejects_a_null_intent_with_no_gap_explaining_it():
    broken = {**_NARROW_PLAN, "intent": _assessment(None, 0.1), "gaps": []}
    with pytest.raises(ValidationError, match="no gap explains it"):
        SelectionPlan.model_validate(broken)


def test_null_intent_with_a_gap_is_accepted():
    plan = SelectionPlan.model_validate(
        {
            **_NARROW_PLAN,
            "intent": _assessment(None, 0.15),
            "gaps": [
                {
                    "id": "gap_1",
                    "description": "The query could be a docs lookup or an incident report.",
                    "kind": "ambiguous_query",
                    "kind_detail": None,
                    "blocks_field": "intent.value",
                    "suggested_agent": None,
                    "suggested_query": "Is this about a live problem or a documented procedure?",
                    "resolvable": True,
                }
            ],
        }
    )
    assert plan.intent.value is None


def test_intent_may_carry_no_evidence_at_high_confidence():
    # The one Assessment exempt from the evidence rule: intent is derived from the query text
    # alone, so requiring evidence would force the coordinator to invent some.
    plan = SelectionPlan.model_validate(
        {**_NARROW_PLAN, "intent": _assessment("incident_diagnosis", 0.95)}
    )
    assert plan.intent.evidence == []


# ------------------------------------------------------------------------- failure modes


def test_plan_raises_when_the_model_skips_the_tool():
    no_tool = SimpleNamespace(
        stop_reason="end_turn",
        model="claude-sonnet-5",
        content=[TextBlock(type="text", text="I would route this to the incident agent.")],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    coordinator, _ = _coordinator([no_tool])
    with pytest.raises(CoordinatorError, match="did not call"):
        coordinator.plan("Why is checkout slow?")


def test_plan_names_truncation_instead_of_blaming_the_model():
    truncated = {k: v for k, v in _NARROW_PLAN.items() if k != "intent"}
    coordinator, _ = _coordinator([_tool_use(truncated, stop_reason="max_tokens")])
    with pytest.raises(CoordinatorError, match="truncated"):
        coordinator.plan("Why is checkout slow?")


def test_plan_rejects_an_empty_query():
    coordinator, _ = _coordinator([])
    with pytest.raises(ValueError, match="query must be non-empty"):
        coordinator.plan("   ")


# ---------------------------------------------------------------------- schema guidance


def test_select_schema_states_the_accounting_rule():
    from aioc.coordinator.planner import _SELECT_SCHEMA

    described = _SELECT_SCHEMA["properties"]["skipped_agents"]["description"]
    assert "All four" in described
    assert "do not nest" in _SELECT_SCHEMA["description"].lower()


# ------------------------------------------------------- round is plumbing, not a decision


def test_the_model_is_never_asked_for_round():
    # Measured on the first live delegation run: with `round` in the model-facing schema,
    # Sonnet omitted it and the whole plan failed validation. The coordinator knows the round
    # number, so asking for it is a failure mode with no upside.
    from aioc.coordinator.planner import _SELECT_SCHEMA

    invocation_schema = _SELECT_SCHEMA["$defs"]["PlannedInvocation"]
    assert "round" not in invocation_schema["properties"]
    assert "round" not in invocation_schema.get("required", [])
    # Everything the model *does* own is still asked for.
    assert {"invocation_id", "agent", "reason", "mode", "depends_on", "context_passed"} <= set(
        invocation_schema["properties"]
    )


def test_plan_stamps_round_zero_on_a_payload_that_omits_it():
    # The exact live failure, as a regression test: a payload with no `round` anywhere.
    payload = {
        **_NARROW_PLAN,
        "selected_agents": [
            {k: v for k, v in _invocation("incident").items() if k != "round"},
        ],
    }
    coordinator, _ = _coordinator([_tool_use(payload)])
    plan = coordinator.plan("Why is checkout returning 502s?")
    assert [i.round for i in plan.selected_agents] == [0]


def test_the_refinement_round_number_is_stamped_over_anything_the_model_says():
    # Day 14 passes round_number=1+. `round` is a fact about this delegation round, so a model
    # that volunteers one does not get to be authoritative about it.
    payload = {**_NARROW_PLAN, "selected_agents": [_invocation("incident", round=7)]}
    coordinator, _ = _coordinator([_tool_use(payload)])
    plan = coordinator.plan("Why is checkout returning 502s?", round_number=2)
    assert [i.round for i in plan.selected_agents] == [2]


def test_the_contract_still_requires_round():
    # The model-facing twin is a narrower ask, not a relaxation of the frozen contract.
    from aioc.contracts import AgentInvocation

    with pytest.raises(ValidationError, match="round"):
        AgentInvocation.model_validate(
            {k: v for k, v in _invocation("incident").items() if k != "round"}
        )


def test_select_schema_guidance_fails_loudly_on_a_renamed_contract_field():
    from aioc.coordinator.planner import _apply_guidance

    with pytest.raises(RuntimeError, match="out of sync"):
        _apply_guidance({"properties": {}, "$defs": {}})
