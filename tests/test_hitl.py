"""Day 16 - the human-in-the-loop approval gate and the approval rule as code.

The policy half: `classify` recognises each kind of production write the contract names
(sec 4.1: rollback, restart, scale, merge, config write, traffic shift, plus deploy and
destructive data operations) and leaves read-only lines alone; `approval_reasons` ORs the
agent's flag, the risk rule, and the classifier.

The gate half: every recommendation in each agent's latest report becomes a request and
gets a decision record; nothing that needs a human is released without one; the default,
a broken approver, an anonymous approval, and an action missing from a script all deny.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from aioc.contracts import (
    CoordinatorResponse,
    DeploymentAgentResponse,
    RecommendedAction,
)
from aioc.hitl import (
    ActionSource,
    ApprovalDecision,
    ApprovalRequest,
    ConsoleApprover,
    Decision,
    HitlGate,
    MutationKind,
    ScriptedApprover,
    Verdict,
    approval_reasons,
    classify,
    render_request,
    requests_for,
    requires_approval,
)
from tests.test_contract import _worked_example
from tests.test_handoff import _deployment_response

# ------------------------------------------------------------------ the classifier

WRITES: list[tuple[str, str | None, MutationKind]] = [
    ("Roll back checkout-api to v1.4.2.", None, MutationKind.ROLLBACK),
    ("Revert commit 9f3c2a1", None, MutationKind.ROLLBACK),
    (
        "Undo the last rollout",
        "kubectl rollout undo deployment/checkout-api",
        MutationKind.ROLLBACK,
    ),
    ("Restart payments-api pods", None, MutationKind.RESTART),
    (
        "Recover the service",
        "kubectl rollout restart deployment/payments-api",
        MutationKind.RESTART,
    ),
    ("Scale checkout-api to 6 replicas", None, MutationKind.SCALE),
    ("Merge PR #12", None, MutationKind.MERGE),
    ("Set DB_POOL_SIZE back to 20", None, MutationKind.CONFIG_WRITE),
    ("Increase the payments-api client timeout to 3s", None, MutationKind.CONFIG_WRITE),
    ("Disable the new-checkout feature flag", None, MutationKind.CONFIG_WRITE),
    (
        "Apply the fixed manifest",
        "kubectl apply -f deploy/checkout.yaml",
        MutationKind.CONFIG_WRITE,
    ),
    ("Shift traffic to us-east", None, MutationKind.TRAFFIC_SHIFT),
    ("Fail over the primary database", None, MutationKind.TRAFFIC_SHIFT),
    ("Drain the node", None, MutationKind.TRAFFIC_SHIFT),
    ("Deploy the hotfix", None, MutationKind.DEPLOY),
    ("Redeploy inventory-api", None, MutationKind.DEPLOY),
    ("Purge the stale cache entries", None, MutationKind.DESTRUCTIVE),
]

READ_ONLY = [
    "Check payments-api p99 latency in Grafana",
    "Review the deploy history for checkout-api",
    "Monitor error rate for 15 minutes",
    "Compare the release diff between v1 and v2",
    "Investigate connection pool exhaustion",
    "Verify the DB_POOL_SIZE key changed in the release",
    "Confirm which commit introduced the regression",
    "Read the checkout-api runbook",
]


@pytest.mark.parametrize(("action", "command", "kind"), WRITES)
def test_each_named_production_write_is_recognised(
    action: str, command: str | None, kind: MutationKind
):
    assert kind in classify(action, command)


@pytest.mark.parametrize("action", READ_ONLY)
def test_read_only_actions_are_left_alone(action: str):
    assert classify(action) == []


# Verbatim from recorded live runs (test-results, Days 10 and 15), replayed through the
# gate on Day 16. Each was misread by the first version of the classifier.
LIVE_READ_ONLY = [
    # "deploy" the noun, a past event - not a request to deploy.
    "Monitor payments-api and checkout-api p99 latency over the next hour to confirm whether "
    "the spike is transient (e.g. cold cache/connection warm-up after deploy) or sustained",
    # An instruction not to roll back.
    "Hold off on rolling back the release that shipped PR #11; continue monitoring "
    "checkout-api and payments-api.",
]


@pytest.mark.parametrize("action", LIVE_READ_ONLY)
def test_recorded_live_non_writes_are_left_alone(action: str):
    assert classify(action) == []


def test_a_recorded_live_circuit_breaker_change_is_a_config_write():
    action = "Add or tighten a timeout/circuit breaker on checkout-api's call to payments-api"
    assert classify(action) == [MutationKind.CONFIG_WRITE]


def test_only_the_negated_match_is_dropped():
    assert classify("Do not restart payments-api; roll back instead") == [MutationKind.ROLLBACK]
    assert classify("If the diff confirms PR #11, roll back checkout-api") == [
        MutationKind.ROLLBACK
    ]


def test_the_command_counts_as_much_as_the_prose():
    # The line reads harmless; the command is the write.
    assert classify("Recover checkout-api", "kubectl rollout undo deployment/checkout-api") == [
        MutationKind.ROLLBACK
    ]


def _action(**over: Any) -> RecommendedAction:
    base: dict[str, Any] = {
        "id": "act_9",
        "action": "Check payments-api p99 latency in Grafana",
        "rationale": "confirms the downstream hypothesis",
        "risk": "low",
        "risk_detail": None,
        "reversible": True,
        "requires_approval": False,
        "target_service": "payments-api",
        "command": None,
    }
    base.update(over)
    return RecommendedAction.model_validate(base)


def test_the_three_signals_are_independent_and_any_one_gates():
    assert approval_reasons(_action()) == []
    assert not requires_approval(_action())
    # The agent flagged it, though nothing else would have.
    assert approval_reasons(_action(requires_approval=True)) == [
        "flagged requires_approval by the agent"
    ]
    # Risky, so the contract already forces the flag - both reasons are recorded.
    assert "risk is high" in approval_reasons(_action(risk="high", requires_approval=True))
    # The case the contract validator cannot see: a low-risk write the agent did not flag.
    unflagged_write = _action(action="Restart payments-api pods")
    assert approval_reasons(unflagged_write) == ["mutates production state: restart"]
    assert requires_approval(unflagged_write)


# --------------------------------------------------------------------- the gate


def _response(
    actions: list[dict[str, Any]] | None = None,
    *,
    deployment: DeploymentAgentResponse | None = None,
) -> CoordinatorResponse:
    example = _worked_example()
    incident = next(r for r in example["agent_responses"] if r["agent"] == "incident")
    if actions is not None:
        incident["findings"]["recommended_actions"] = actions
    if deployment is not None:
        example["agent_responses"].append(deployment.model_dump(mode="json"))
    return CoordinatorResponse.model_validate(example)


def _payload(action: RecommendedAction) -> dict[str, Any]:
    return action.model_dump(mode="json")


def _rollback_now(**approval: Any) -> DeploymentAgentResponse:
    payload = _deployment_response().model_dump(mode="json")
    payload["findings"]["rollback_recommendation"] = {
        "value": "rollback_now",
        "confidence": 0.8,
        "evidence": ["ev_health"],
        "reasoning": "error rate doubled after the release",
        "detail": None,
    }
    payload["findings"]["approval"].update(approval)
    return DeploymentAgentResponse.model_validate(payload)


_FIXED = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def test_the_worked_example_rollback_is_withheld_by_default():
    """The default approver denies: with no human wired in, nothing is released."""
    result = HitlGate(clock=lambda: _FIXED).review(_response())

    (decision,) = result.decisions
    assert decision.request.action_id == "act_1"
    assert decision.request.source is ActionSource.RECOMMENDED_ACTION
    assert decision.request.mutations == [MutationKind.ROLLBACK]
    assert decision.decision is Decision.DENIED and not decision.released
    assert decision.decided_by == "deny_all"
    assert decision.decided_at == _FIXED
    assert result.released == [] and result.withheld == [decision]


def test_a_read_only_action_is_released_without_asking_and_still_recorded():
    approver = ScriptedApprover({})
    read_only = _payload(_action())
    result = HitlGate(approver).review(_response([read_only]))

    (decision,) = result.decisions
    assert approver.asked == []  # nobody was asked...
    assert decision.decision is Decision.NOT_REQUIRED  # ...and the record says why
    assert decision.decided_by == "policy" and decision.released


def test_an_unflagged_low_risk_write_is_still_sent_to_a_human():
    # The flag says false; the gate re-derives the requirement and does not trust it.
    approver = ScriptedApprover({"act_9": True}, identity="oncall@example")
    write = _payload(_action(action="Restart payments-api pods"))
    result = HitlGate(approver).review(_response([write]))

    (decision,) = result.decisions
    assert [r.action_id for r in approver.asked] == ["act_9"]
    assert decision.decision is Decision.APPROVED and decision.decided_by == "oncall@example"
    assert decision.request.reasons == ["mutates production state: restart"]


def test_a_scripted_denial_and_an_action_missing_from_the_script_both_deny():
    actions = [
        _payload(_action(id="act_a", action="Restart payments-api pods")),
        _payload(_action(id="act_b", action="Scale checkout-api to 6 replicas")),
    ]
    result = HitlGate(ScriptedApprover({"act_a": False})).review(_response(actions))
    assert [(d.request.action_id, d.decision) for d in result.decisions] == [
        ("act_a", Decision.DENIED),
        ("act_b", Decision.DENIED),
    ]
    assert result.decisions[1].note == "not in the approval script"


class _Broken:
    def decide(self, request: ApprovalRequest) -> Verdict:
        raise TimeoutError("pager did not answer")


class _Anonymous:
    def decide(self, request: ApprovalRequest) -> Verdict:
        return Verdict(approved=True, decided_by="  ")


def test_a_broken_approver_denies_instead_of_crashing():
    (decision,) = HitlGate(_Broken()).review(_response()).decisions
    assert decision.decision is Decision.DENIED
    assert "fails closed" in decision.note and "pager did not answer" in decision.note


def test_an_anonymous_approval_is_refused():
    (decision,) = HitlGate(_Anonymous()).review(_response()).decisions
    assert decision.decision is Decision.DENIED
    assert "anonymous" in decision.note


def test_a_deployment_rollback_is_gated_unconditionally():
    # Low risk and the only signal is the contract's const approval - still gated.
    approver = ScriptedApprover({"act_dep_rollback": True}, identity="release-manager")
    result = HitlGate(approver).review(_response([], deployment=_rollback_now()))

    (decision,) = result.decisions
    request = decision.request
    assert request.source is ActionSource.ROLLBACK_RECOMMENDATION
    assert request.action_id == "act_dep_rollback"
    assert request.action == "Roll back checkout-api in development from 1522000 to 7e5c94f"
    assert request.reversible is None  # the report does not say, so neither does the gate
    assert request.evidence == ["ev_health"]
    assert request.reasons[0].startswith("deployment approval is const")
    assert decision.decision is Decision.APPROVED


def test_hold_and_monitor_is_not_an_action():
    # The fixture's recommendation is hold_and_monitor: nothing to gate.
    assert requests_for(_response([], deployment=_deployment_response())) == []


def test_an_other_recommendation_is_gated_too():
    payload = _rollback_now().model_dump(mode="json")
    payload["findings"]["rollback_recommendation"].update(
        value="other", detail="pause the canary at 10%"
    )
    deployment = DeploymentAgentResponse.model_validate(payload)
    (request,) = requests_for(_response([], deployment=deployment))
    assert "pause the canary at 10%" in request.action
    assert request.needs_human


def test_only_each_agents_latest_report_is_gated():
    """A refinement round's report supersedes the earlier one from the same agent."""
    example = _worked_example()
    first = next(r for r in example["agent_responses"] if r["agent"] == "incident")
    revised = {**first, "invocation_id": "inv_a1_r1"}
    revised["findings"] = {
        **first["findings"],
        "recommended_actions": [_payload(_action(id="act_revised"))],
    }
    example["agent_responses"].append(revised)
    response = CoordinatorResponse.model_validate(example)
    assert [r.action_id for r in requests_for(response)] == ["act_revised"]


def test_the_console_approver_needs_an_explicit_yes():
    shown: list[str] = []
    answers = iter(["y", "", "nope"])
    approver = ConsoleApprover("misbah@console", ask=lambda _: next(answers), show=shown.append)
    gate = HitlGate(approver)
    decisions = [gate.review(_response()).decisions[0] for _ in range(3)]
    assert [d.decision for d in decisions] == [Decision.APPROVED, Decision.DENIED, Decision.DENIED]
    assert all(d.decided_by == "misbah@console" for d in decisions)
    assert "kubectl rollout undo deployment/checkout-api" in shown[0]


def test_end_of_input_at_the_console_denies():
    def eof(_: str) -> str:
        raise EOFError

    (decision,) = (
        HitlGate(ConsoleApprover("ops", ask=eof, show=lambda _: None)).review(_response()).decisions
    )
    assert decision.decision is Decision.DENIED


def test_a_console_approver_must_say_who_is_approving():
    with pytest.raises(ValueError):
        ConsoleApprover(" ")


def test_the_rendered_request_names_what_a_human_needs_to_decide():
    (request,) = requests_for(_response())
    text = render_request(request)
    for needle in (
        "act_1",
        "Roll back checkout-api to v1.4.2.",
        "kubectl rollout undo deployment/checkout-api",
        "risk:      medium; reversible: yes",
        "mutates production state: rollback",
    ):
        assert needle in text


def test_a_decision_is_a_record_that_round_trips():
    """The shape the Day 17 audit log persists: JSON out, the same record back."""
    (decision,) = HitlGate(clock=lambda: _FIXED).review(_response()).decisions
    again = ApprovalDecision.model_validate_json(decision.model_dump_json())
    assert again == decision
    assert decision.decision_id.startswith("apr_")


# ------------------------------------------------ the recorded-run replay script (free)


def test_the_replay_script_gates_a_recorded_response(tmp_path, capsys):
    from gate_recorded_run import main

    run = tmp_path / "20260925T000000Z__llm__day16"
    run.mkdir()
    (run / "response.json").write_text(_response().model_dump_json(), encoding="utf-8")
    (tmp_path / "not_a_response.json").write_text("{}", encoding="utf-8")

    assert main(["--run", str(run), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    (summary,) = report["runs"]
    assert (summary["requests"], summary["needs_human"], summary["withheld"]) == (1, 1, 1)
    assert summary["decisions"][0]["decided_by"] == "deny_all"

    assert main(["--run", str(tmp_path / "not_a_response.json"), "--json"]) == 0
    assert "not a CoordinatorResponse" in json.loads(capsys.readouterr().out)["skipped"][0]


def test_the_replay_script_refuses_an_anonymous_console_approver():
    from gate_recorded_run import main

    with pytest.raises(SystemExit, match="--identity"):
        main(["--approver", "console"])
