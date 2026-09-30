"""Unit tests for the Deployment agent (Day 12).

Same pattern as the GitHub suite: the Anthropic client is a scripted fake injected through
`LLMClient`, and the MCP toolset is an in-process fake behind the `Toolset` seam whose
handlers return canned contract envelopes - deterministic, offline, key-free. Under test is
the agent's plumbing - explicit context passing, the two-phase tool loop, fact stamping
from the ledger, the grounding rejections, the not-looked-is-not-empty rule, the honest
`ToolCallRef`s - not the model's judgement (the Day 19 eval harness owns that).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock, ToolUseBlock

from aioc.agents import (
    DEPLOYMENT_EMIT_TOOL_NAME,
    DEPLOYMENT_SYSTEM_PROMPT,
    DeploymentAgent,
    DeploymentAgentError,
)
from aioc.agents.deployment import _EMIT_SCHEMA, DeploymentReport, _apply_guidance
from aioc.contracts import (
    AgentName,
    DeploymentAgentResponse,
    ErrorClass,
    ResponseStatus,
    RolloutStatus,
)
from aioc.llm import LLMClient, LLMSettings, ToolResult, ToolSpec, Usage, system_text
from tests.wire import check_conversation

# --------------------------------------------------------------------------- fakes


class _FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        check_conversation(kwargs["messages"])
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake client ran out of scripted responses")
        return self._responses.pop(0)


class _FakeAnthropic:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _FakeMessages(responses)


V1, V2 = "v1.4.2", "v1.4.3"
_COMMIT_MESSAGE = "Raise DB_POOL_MAX and add a pool timeout (#12)"


def _diff_envelope() -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "service": "checkout-api",
            "from_version": V1,
            "to_version": V2,
            "config_keys_added": ["DB_POOL_TIMEOUT_MS"],
            "config_keys_removed": [],
            "config_keys_changed": ["DB_POOL_MAX"],
            "image_changes": [
                {
                    "container": "checkout-api",
                    "from_image": "aioc-demo-service:day3",
                    "to_image": "aioc-demo-service:day4",
                    "from_digest": None,
                    "to_digest": "sha256:" + "1" * 64,
                }
            ],
            "commits": [
                {
                    "sha": "b" * 40,
                    "message": _COMMIT_MESSAGE,
                    "authored_at": "2026-09-08T10:00:00Z",
                }
            ],
            "manifest_changes": ["services.checkout-api.deploy.replicas"],
        },
        "meta": {
            "truncated": False,
            "total_available": 3,
            "returned": 5,
            "token_estimate": 140,
            "query_ms": 410,
            "source": "github",
            "as_of": "2026-09-09T12:00:00Z",
        },
    }


def _health_envelope(**overrides: Any) -> dict[str, Any]:
    data = {
        "service": "checkout-api",
        "environment": "development",
        "version": V2,
        "status": "degraded",
        "replicas": {"desired": 1, "ready": 1, "updated": 1, "unavailable": 0},
        "signals": {
            "error_rate": 0.31,
            "p50_latency_ms": 310.0,
            "p99_latency_ms": 2100.0,
            "restart_count": 0,
            "probe_failures": 0,
        },
        "compared_to_baseline": {
            "baseline_version": V1,
            "error_rate_delta": 0.306,
            "p99_delta_ms": 1920.0,
        },
    }
    data.update(overrides)
    return {
        "ok": True,
        "data": data,
        "meta": {
            "truncated": False,
            "total_available": None,
            "returned": 1,
            "token_estimate": 84,
            "query_ms": 120,
            "source": "prometheus",
            "as_of": "2026-09-09T12:00:00Z",
        },
    }


def _error_envelope() -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "class": "transient",
            "code": "PROMETHEUS_TIMEOUT",
            "message": "Prometheus did not answer within 5s.",
            "retryable": True,
            "retry_after_ms": 2000,
            "details": {"prometheus_url": "http://localhost:9090"},
            "remediation": "Retry after 2s.",
        },
    }


class _FakeToolset:
    """An open toolset: the two deployment tools, each answering from a canned envelope."""

    server_name = "aioc-deployment"

    def __init__(self, envelopes: dict[str, dict[str, Any]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._envelopes = envelopes

    @property
    def tools(self) -> list[ToolSpec]:
        def handler(name: str) -> Any:
            def run(args: dict[str, Any]) -> ToolResult:
                self.calls.append((name, args))
                envelope = self._envelopes[name]
                return ToolResult(content=json.dumps(envelope), is_error=not envelope["ok"])

            return run

        return [
            ToolSpec(
                name=n, description=f"{n} desc", input_schema={"type": "object"}, handler=handler(n)
            )
            for n in ("diff_release", "check_rollout_health")
        ]


def _message(
    content: list[Any], *, stop_reason: str, in_tokens: int = 500, out_tokens: int = 100
) -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=content,
        model="claude-sonnet-5",
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
    )


def _tool_call_message(name: str, args: dict[str, Any], call_id: str = "toolu_1") -> Any:
    return _message(
        [ToolUseBlock(type="tool_use", id=call_id, name=name, input=args)], stop_reason="tool_use"
    )


def _done_message() -> Any:
    return _message([TextBlock(type="text", text="I have what I need.")], stop_reason="end_turn")


def _emit_message(payload: dict[str, Any], *, stop_reason: str = "tool_use") -> Any:
    return _message(
        [
            ToolUseBlock(
                type="tool_use", id="toolu_emit", name=DEPLOYMENT_EMIT_TOOL_NAME, input=payload
            )
        ],
        stop_reason=stop_reason,
    )


def _assessment(value: Any, confidence: float = 0.8, evidence: list[str] | None = None) -> dict:
    return {
        "value": value,
        "confidence": confidence,
        "evidence": evidence if evidence is not None else ["ev_1"],
        "reasoning": "From the tool replies.",
        "detail": None,
    }


def _report(**overrides: Any) -> dict[str, Any]:
    """A contract-valid DeploymentReport grounded in the two fake envelopes above."""
    payload: dict[str, Any] = {
        "status": "complete",
        "status_detail": None,
        "summary": "v1.4.3 raised DB_POOL_MAX and the error rate is 31%; roll back.",
        "findings": {
            "service": "checkout-api",
            "environment": "development",
            "environment_detail": None,
            "releases_compared": {"from_version": V1, "to_version": V2},
            "rollout_status": _assessment("degraded"),
            "regression_suspected": _assessment(True, 0.75, ["ev_1", "ev_2"]),
            "rollback_recommendation": _assessment("rollback_now", 0.7, ["ev_1", "ev_2"]),
            "approval": {
                "requires_approval": True,
                "risk": "medium",
                "risk_detail": None,
                "blast_radius": "checkout-api only",
            },
        },
        "evidence": [
            {
                "id": "ev_1",
                "source_type": "metric",
                "source_type_detail": None,
                "source_ref": "error_rate",
                "excerpt": '"error_rate": 0.31',
                "observed_at": None,
                "uri": None,
                "tool_call_id": None,
            },
            {
                "id": "ev_2",
                "source_type": "commit",
                "source_type_detail": None,
                "source_ref": "b" * 40,
                "excerpt": _COMMIT_MESSAGE,
                "observed_at": None,
                "uri": None,
                "tool_call_id": None,
            },
        ],
        "gaps": [],
        "overall_confidence": 0.75,
    }
    payload.update(overrides)
    return payload


def _agent(
    responses: list[Any], envelopes: dict[str, dict[str, Any]] | None = None
) -> tuple[DeploymentAgent, _FakeMessages, _FakeToolset]:
    fake = _FakeAnthropic(responses)
    settings = LLMSettings(model="claude-sonnet-5", max_tokens=4096)
    client = LLMClient(settings, client=fake)  # type: ignore[arg-type]
    toolset = _FakeToolset(
        envelopes
        if envelopes is not None
        else {"diff_release": _diff_envelope(), "check_rollout_health": _health_envelope()}
    )

    @contextmanager
    def factory() -> Any:
        yield toolset

    agent = DeploymentAgent(client, toolset=factory, max_validation_retries=0)
    return agent, fake.messages, toolset


_CONTEXT = (
    "Deployment check for checkout-api in development. The previous release was v1.4.2 and "
    "v1.4.3 went out at 13:52Z; a 5xx spike on checkout-api started at 13:58Z."
)
_QUERY = "Did the v1.4.3 release of checkout-api cause the 5xx spike, and should we roll back?"

_DIFF_ARGS = {"service": "checkout-api", "from_version": V1, "to_version": V2}
_HEALTH_ARGS = {
    "service": "checkout-api",
    "environment": "development",
    "version": V2,
    "lookback_minutes": 15,
}
_SCRIPT = [
    _tool_call_message("diff_release", _DIFF_ARGS),
    _tool_call_message("check_rollout_health", _HEALTH_ARGS, call_id="toolu_2"),
    _done_message(),
]


# ------------------------------------------------------------------------ happy path


def test_assess_returns_a_validated_response_with_facts_stamped_from_the_tools():
    agent, messages, toolset = _agent([*_SCRIPT, _emit_message(_report())])
    usage = Usage()

    resp = agent.assess(
        _QUERY, context=_CONTEXT, request_id="req_1", invocation_id="inv_1", usage=usage
    )

    assert isinstance(resp, DeploymentAgentResponse)
    assert resp.agent is AgentName.DEPLOYMENT
    assert resp.request_id == "req_1" and resp.invocation_id == "inv_1"
    assert toolset.calls == [("diff_release", _DIFF_ARGS), ("check_rollout_health", _HEALTH_ARGS)]

    f = resp.findings
    # Facts came from the envelopes, not the model (which sent service + releases + judgements).
    assert f.changed_config_keys == ["DB_POOL_MAX", "DB_POOL_TIMEOUT_MS"]
    assert [i.to_image for i in f.image_changes] == ["aioc-demo-service:day4"]
    assert f.image_changes[0].to_digest == "sha256:" + "1" * 64
    assert f.health_signals.error_rate == 0.31 and f.health_signals.p99_latency_ms == 2100.0
    assert f.health_signals.replicas_desired == 1 and f.health_signals.replicas_ready == 1
    assert f.health_signals.restart_count == 0 and f.health_signals.probe_failures == 0
    assert f.health_signals.observed_over_seconds == 15 * 60  # from the call's arguments
    assert f.releases_compared.from_version == V1 and f.releases_compared.to_version == V2
    assert f.approval.requires_approval is True and f.approval.risk.value == "medium"
    assert f.rollout_status.value == "degraded" and f.regression_suspected.value is True
    assert f.rollback_recommendation.value == "rollback_now"

    # One honest ToolCallRef per wire call; evidence points at the call that returned it.
    assert [tc.tool_name for tc in resp.tool_calls] == ["diff_release", "check_rollout_health"]
    assert all(tc.server == "aioc-deployment" and tc.ok for tc in resp.tool_calls)
    assert resp.tool_calls[0].tokens_returned == 140 and resp.tool_calls[1].tokens_returned == 84
    by_id = {e.id: e for e in resp.evidence}
    assert by_id["ev_1"].tool_call_id == resp.tool_calls[1].id  # the metric, from health
    assert by_id["ev_2"].tool_call_id == resp.tool_calls[0].id  # the commit, from the diff

    # Cost: three loop calls plus the forced emit call, all accumulated.
    assert len(messages.calls) == 4
    assert usage.input_tokens == 2000 and usage.output_tokens == 400


def test_context_is_passed_explicitly_and_the_emit_call_is_forced():
    agent, messages, _ = _agent([*_SCRIPT, _emit_message(_report())])
    agent.assess(_QUERY, context=_CONTEXT)

    first = messages.calls[0]
    assert system_text(first["system"]) == DEPLOYMENT_SYSTEM_PROMPT
    user_text = first["messages"][0]["content"]
    assert _CONTEXT in user_text and _QUERY in user_text
    assert {t["name"] for t in first["tools"]} == {
        "diff_release",
        "check_rollout_health",
        DEPLOYMENT_EMIT_TOOL_NAME,  # offered, never forced, during the investigation
    }
    assert "tool_choice" not in first or first["tool_choice"] is None

    emit = messages.calls[-1]
    assert emit["tool_choice"] == {"type": "tool", "name": DEPLOYMENT_EMIT_TOOL_NAME}
    roles = [m["role"] for m in emit["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]


def test_a_model_that_emits_inside_the_loop_skips_the_forced_call():
    script = [
        *_SCRIPT[:2],
        _message(
            [
                ToolUseBlock(
                    type="tool_use",
                    id="toolu_3",
                    name=DEPLOYMENT_EMIT_TOOL_NAME,
                    input=_report(),
                )
            ],
            stop_reason="tool_use",
        ),
        _done_message(),
    ]
    agent, messages, _ = _agent(script)
    resp = agent.assess(_QUERY, context=_CONTEXT)
    assert len(messages.calls) == 4 and "tool_choice" not in messages.calls[-1]
    # The emit capture is not a wire call.
    assert [tc.tool_name for tc in resp.tool_calls] == ["diff_release", "check_rollout_health"]


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_context_is_refused_before_any_call(bad: str):
    agent, messages, toolset = _agent([])
    with pytest.raises(ValueError, match="context must be non-empty"):
        agent.assess(_QUERY, context=bad)
    assert messages.calls == [] and toolset.calls == []


# ------------------------------------------------------------------ grounding rejections


def test_a_service_the_tools_were_never_asked_about_is_rejected():
    report = _report()
    report["findings"]["service"] = "payments-api"
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    with pytest.raises(DeploymentAgentError, match="'payments-api' was never asked"):
        agent.assess(_QUERY, context=_CONTEXT)


def test_releases_no_tool_returned_are_rejected():
    report = _report()
    report["findings"]["releases_compared"] = {"from_version": V1, "to_version": "v9.9.9"}
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    with pytest.raises(DeploymentAgentError, match="match no diff_release reply"):
        agent.assess(_QUERY, context=_CONTEXT)


def test_a_paraphrased_excerpt_is_rejected():
    report = _report()
    report["evidence"][1]["excerpt"] = "The commit raised the pool max."
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    with pytest.raises(DeploymentAgentError, match="does not appear verbatim"):
        agent.assess(_QUERY, context=_CONTEXT)


def test_a_number_quoted_from_a_reply_is_grounded_with_or_without_json_quotes():
    """`"error_rate": 0.31` and `error_rate: 0.31` are the same fact; both are the tool's."""
    for excerpt in ('"error_rate": 0.31', "error_rate: 0.31", '"p99_latency_ms": 2100.0'):
        report = _report()
        report["evidence"][0]["excerpt"] = excerpt
        agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
        resp = agent.assess(_QUERY, context=_CONTEXT)
        assert resp.evidence[0].tool_call_id == resp.tool_calls[1].id


def test_an_excerpt_quoted_from_the_context_is_grounded_without_a_tool_call_id():
    report = _report()
    report["evidence"].append(
        {
            "id": "ev_ctx",
            "source_type": "deployment",
            "source_type_detail": None,
            "source_ref": "checkout-api@v1.4.3",
            "excerpt": "v1.4.3 went out at 13:52Z",
            "observed_at": None,
            "uri": None,
            "tool_call_id": None,
        }
    )
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    resp = agent.assess(_QUERY, context=_CONTEXT)
    assert resp.evidence[-1].tool_call_id is None


def test_a_rejected_report_rides_on_the_error_for_the_retry_loop():
    report = _report()
    report["findings"]["service"] = "ghost-api"
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    with pytest.raises(DeploymentAgentError) as info:
        agent.assess(_QUERY, context=_CONTEXT)
    assert isinstance(info.value.report, DeploymentReport)
    assert info.value.report.findings.service == "ghost-api"


# ------------------------------------------------ not looked is a gap, never an empty list


def _health_gap() -> dict[str, Any]:
    return {
        "id": "gap_h",
        "description": "check_rollout_health was not run.",
        "kind": "missing_data",
        "kind_detail": None,
        "blocks_field": "findings.health_signals",
        "suggested_agent": None,
        "suggested_query": None,
        "resolvable": True,
    }


def _null_gap(field: str) -> dict[str, Any]:
    """The contract's twin rule: a null Assessment needs a Gap naming it (sec 2.1)."""
    return {
        "id": f"gap_{field.rsplit('.', 1)[-1]}",
        "description": f"{field} could not be assessed from what was read.",
        "kind": "missing_data",
        "kind_detail": None,
        "blocks_field": f"findings.{field}.value",
        "suggested_agent": None,
        "suggested_query": None,
        "resolvable": True,
    }


def _diff_gap() -> dict[str, Any]:
    return {
        "id": "gap_d",
        "description": "diff_release was not run; the question was about health only.",
        "kind": "missing_data",
        "kind_detail": None,
        "blocks_field": "findings.changed_config_keys",
        "suggested_agent": None,
        "suggested_query": None,
        "resolvable": True,
    }


def test_health_only_stamps_releases_from_the_health_reply_and_needs_a_diff_gap():
    script = [_tool_call_message("check_rollout_health", _HEALTH_ARGS), _done_message()]
    report = _report(status="partial", gaps=[_diff_gap(), _null_gap("regression_suspected")])
    report["evidence"] = [report["evidence"][0]]  # the commit was never fetched
    report["findings"]["regression_suspected"] = _assessment(None, 0.2, [])
    report["findings"]["regression_suspected"]["reasoning"] = "No diff was read."
    report["findings"]["rollback_recommendation"] = _assessment("hold_and_monitor", 0.6)
    agent, _, _ = _agent([*script, _emit_message(report)])
    resp = agent.assess(_QUERY, context=_CONTEXT)
    assert resp.findings.releases_compared.from_version == V1  # the health reply's baseline
    assert resp.findings.changed_config_keys == [] and resp.findings.image_changes == []
    assert resp.findings.health_signals.error_rate == 0.31


def test_health_only_without_a_diff_gap_is_rejected():
    script = [_tool_call_message("check_rollout_health", _HEALTH_ARGS), _done_message()]
    report = _report(gaps=[_null_gap("regression_suspected")])
    report["evidence"] = [report["evidence"][0]]
    report["findings"]["regression_suspected"] = _assessment(None, 0.2, [])
    agent, _, _ = _agent([*script, _emit_message(report)])
    with pytest.raises(DeploymentAgentError, match="changed_config_keys.*never an empty list"):
        agent.assess(_QUERY, context=_CONTEXT)


def test_health_only_from_version_must_be_the_reported_baseline():
    script = [_tool_call_message("check_rollout_health", _HEALTH_ARGS), _done_message()]
    report = _report(status="partial", gaps=[_diff_gap()])
    report["evidence"] = [report["evidence"][0]]
    report["findings"]["releases_compared"] = {"from_version": "v1.0.0", "to_version": V2}
    agent, _, _ = _agent([*script, _emit_message(report)])
    with pytest.raises(DeploymentAgentError, match="baseline was 'v1.4.2'"):
        agent.assess(_QUERY, context=_CONTEXT)


def test_diff_only_leaves_health_null_and_needs_a_health_gap():
    script = [_tool_call_message("diff_release", _DIFF_ARGS), _done_message()]
    judgements = ("rollout_status", "regression_suspected", "rollback_recommendation")
    report = _report(status="partial", gaps=[_health_gap(), *map(_null_gap, judgements)])
    report["evidence"] = [report["evidence"][1]]
    for field in judgements:
        report["findings"][field] = _assessment(None, 0.1, [])
    agent, _, _ = _agent([*script, _emit_message(report)])
    resp = agent.assess(_QUERY, context=_CONTEXT)
    signals = resp.findings.health_signals
    assert all(
        getattr(signals, name) is None
        for name in (
            "replicas_desired",
            "replicas_ready",
            "restart_count",
            "probe_failures",
            "error_rate",
            "p99_latency_ms",
            "observed_over_seconds",
        )
    )
    assert resp.findings.changed_config_keys == ["DB_POOL_MAX", "DB_POOL_TIMEOUT_MS"]


def test_diff_only_without_a_health_gap_is_rejected():
    script = [_tool_call_message("diff_release", _DIFF_ARGS), _done_message()]
    judgements = ("rollout_status", "regression_suspected", "rollback_recommendation")
    report = _report(status="partial", gaps=list(map(_null_gap, judgements)))
    report["evidence"] = [report["evidence"][1]]
    for field in judgements:
        report["findings"][field] = _assessment(None, 0.1, [])
    agent, _, _ = _agent([*script, _emit_message(report)])
    with pytest.raises(DeploymentAgentError, match="health_signals"):
        agent.assess(_QUERY, context=_CONTEXT)


def test_a_health_reply_for_another_version_does_not_stamp_this_release():
    """Signals measured on v1.4.4 are not v1.4.3's health; they must not be presented as it."""
    envelopes = {
        "diff_release": _diff_envelope(),
        "check_rollout_health": _health_envelope(version="v1.4.4"),
    }
    report = _report()  # no health gap, and it claims v1.4.3
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)], envelopes)
    with pytest.raises(DeploymentAgentError, match="health_signals"):
        agent.assess(_QUERY, context=_CONTEXT)


# ----------------------------------------------------------------- failure honesty


def test_a_failed_tool_call_is_recorded_with_its_error_class():
    envelopes = {"diff_release": _diff_envelope(), "check_rollout_health": _error_envelope()}
    nulls = ("rollout_status", "rollback_recommendation")
    report = _report(status="partial", gaps=[_health_gap(), *map(_null_gap, nulls)])
    report["evidence"] = [report["evidence"][1]]
    for field in nulls:
        report["findings"][field] = _assessment(None, 0.1, [])
    report["findings"]["regression_suspected"] = _assessment(True, 0.4, ["ev_2"])
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)], envelopes)
    resp = agent.assess(_QUERY, context=_CONTEXT)
    assert resp.status.value == "partial"
    health = resp.tool_calls[1]
    assert health.ok is False and health.error_class is ErrorClass.TRANSIENT
    assert health.tokens_returned is None and health.truncated is False
    assert resp.findings.health_signals.error_rate is None


def test_complete_over_a_null_judgement_settles_to_partial_not_a_refusal():
    """The first live sequential run: one judgement honestly null with its gap, and
    `status: complete` on the envelope - refused by the contract for a field the runtime
    could have settled. Now it is settled: the report is accepted as `partial`. The other
    direction is never taken - a reported `partial` stays `partial` (Day 13)."""
    report = _report(gaps=[_null_gap("regression_suspected")])
    report["findings"]["regression_suspected"] = _assessment(None, 0.2, [])
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    resp = agent.assess(_QUERY, context=_CONTEXT)
    assert resp.status is ResponseStatus.PARTIAL
    assert resp.findings.regression_suspected.value is None
    assert resp.findings.rollout_status.value is RolloutStatus.DEGRADED  # the rest intact

    stays = _report(status="partial")
    agent, _, _ = _agent([*_SCRIPT, _emit_message(stays)])
    assert agent.assess(_QUERY, context=_CONTEXT).status is ResponseStatus.PARTIAL


def test_complete_status_with_no_successful_tool_call_is_rejected():
    envelopes = {"diff_release": _error_envelope(), "check_rollout_health": _error_envelope()}
    judgements = ("rollout_status", "regression_suspected", "rollback_recommendation")
    report = _report(gaps=[_health_gap(), _diff_gap(), *map(_null_gap, judgements)])
    report["evidence"] = []
    for field in judgements:
        report["findings"][field] = _assessment(None, 0.1, [])
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)], envelopes)
    with pytest.raises(DeploymentAgentError, match="no tool call succeeded"):
        agent.assess(_QUERY, context=_CONTEXT)


def test_requires_approval_is_stamped_true_whatever_the_model_sent():
    report = _report()
    del report["findings"]["approval"]["requires_approval"]  # the model omitted it
    agent, _, _ = _agent([*_SCRIPT, _emit_message(report)])
    resp = agent.assess(_QUERY, context=_CONTEXT)
    assert resp.findings.approval.requires_approval is True


# ------------------------------------------------------------------- output plumbing


def test_truncated_emit_output_is_reported_as_truncation_not_as_a_schema_error():
    agent, _, _ = _agent(
        [*_SCRIPT, _emit_message({"status": "complete"}, stop_reason="max_tokens")]
    )
    with pytest.raises(DeploymentAgentError, match="max_tokens"):
        agent.assess(_QUERY, context=_CONTEXT)


def test_a_tool_loop_cut_off_mid_call_still_hands_on_a_conversation_the_api_accepts():
    # The 2026-09-30 live run: the loop stopped at max_tokens part-way through a tool call,
    # and the forced emit was refused with a 400 because that call had no tool_result.
    cut_off = _message(
        [
            TextBlock(type="text", text="Writing the report now."),
            ToolUseBlock(type="tool_use", id="toolu_cut", name=DEPLOYMENT_EMIT_TOOL_NAME, input={}),
        ],
        stop_reason="max_tokens",
    )
    agent, messages, toolset = _agent([*_SCRIPT[:2], cut_off, _emit_message(_report())])
    resp = agent.assess(_QUERY, context=_CONTEXT)
    assert resp.findings.service == "checkout-api"
    # The forced call answered the cut-off tool_use rather than leaving it dangling ...
    forced = messages.calls[-1]
    answer = forced["messages"][-2]["content"]
    assert answer[0]["tool_use_id"] == "toolu_cut" and answer[0]["is_error"] is True
    assert "max_tokens" in answer[0]["content"]
    # ... and never ran it: its input is whatever was written before the cut.
    assert [name for name, _ in toolset.calls] == ["diff_release", "check_rollout_health"]


def test_model_not_calling_the_emit_tool_fails_clearly():
    agent, _, _ = _agent([*_SCRIPT, _done_message()])
    with pytest.raises(DeploymentAgentError, match="did not call"):
        agent.assess(_QUERY, context=_CONTEXT)


def test_emit_schema_is_generated_from_the_frozen_models_and_annotated():
    props = _EMIT_SCHEMA["properties"]
    assert set(props) >= {"status", "summary", "findings", "evidence", "gaps", "overall_confidence"}
    assert "tool data" in props["status"]["description"]
    # Facts are not the model's to fill in: no keys, images, or signals in the reported shape.
    reported = set(_EMIT_SCHEMA["$defs"]["ReportedFindings"]["properties"])
    assert reported == {
        "service",
        "environment",
        "environment_detail",
        "releases_compared",
        "rollout_status",
        "regression_suspected",
        "rollback_recommendation",
        "approval",
    }
    assert (
        _EMIT_SCHEMA["$defs"]["ApprovalRequirement"]["properties"]["requires_approval"]["const"]
        is True
    )


def test_guidance_drift_fails_loudly():
    schema = DeploymentReport.model_json_schema()
    del schema["$defs"]["ReportedReleases"]["properties"]["to_version"]
    with pytest.raises(RuntimeError, match="ReportedReleases.to_version"):
        _apply_guidance(schema)
