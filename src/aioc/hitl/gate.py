"""The human-in-the-loop approval gate (Day 16).

AIOC recommends; it never acts. What leaves a request is a `CoordinatorResponse` whose
agents may recommend production writes - the Incident agent's ``recommended_actions``
and the Deployment agent's ``rollback_recommendation``. The gate stands between that
response and anything that would carry a recommendation out: it turns each recommendation
into an `ApprovalRequest`, decides whether a human must answer it, asks the `Approver` when
one must, and returns an `ApprovalDecision` for **every** recommendation - including the
ones it released without asking, so the record says why nobody was asked.

The decisions worth writing down:

**The gate re-derives the requirement; it does not trust the flag.** An agent's
``requires_approval`` is one of three signals (`aioc.hitl.policy.approval_reasons`); the
risk level and the mutation classifier are the other two, and any one gates the action.
A Deployment rollback is gated unconditionally - the contract makes its approval const
``true``, and the gate honours the contract rather than re-arguing it.

**It fails closed.** The default approver is `DenyAll`: with no human wired in, nothing
that needs one is released. An approver that raises, or answers without saying who
decided, produces a ``denied`` decision carrying the reason - never an approval by
default and never a crash that loses the other decisions.

**Every decision is a record, not a return code.** `ApprovalDecision` carries the request
it answers, who or what decided, when, and why - the shape the Day 17 audit log persists.
It is deliberately not part of the frozen contract: the contract describes what agents
report, and a human's answer is not an agent report. `CoordinatorResponse` stays
untouched; the gate reads it and hands its decisions alongside.

**Only each agent's latest report is gated.** A refinement round can supersede an earlier
report from the same agent (the executor judges status the same way); gating the earlier
one too would ask a human twice about a recommendation the agent has since revised.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from aioc.contracts import (
    AgentName,
    AgentResponse,
    CoordinatorResponse,
    DeploymentAgentResponse,
    IncidentAgentResponse,
    RecommendedAction,
    RiskLevel,
    RollbackRecommendation,
)

from .policy import MutationKind, approval_reasons, classify


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ActionSource(StrEnum):
    """Where in an agent's findings the recommendation came from."""

    RECOMMENDED_ACTION = "recommended_action"  # IncidentFindings.recommended_actions[i]
    ROLLBACK_RECOMMENDATION = "rollback_recommendation"  # DeploymentFindings


class Decision(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    NOT_REQUIRED = "not_required"  # released without asking: no signal said a human must


class ApprovalRequest(_Record):
    """One recommendation, as the human sees it. Everything here comes from the agent's
    report; ``reasons`` is the gate's own account of why it is asking."""

    request_id: str
    invocation_id: str
    agent: AgentName
    source: ActionSource
    action_id: str
    action: str
    rationale: str
    risk: RiskLevel
    risk_detail: str | None
    reversible: bool | None  # null when the report does not say (a rollback recommendation)
    target_service: str | None
    command: str | None
    mutations: list[MutationKind]
    reasons: list[str]
    evidence: list[str] = Field(default_factory=list)

    @property
    def needs_human(self) -> bool:
        return bool(self.reasons)


class ApprovalDecision(_Record):
    """What was decided about one request - the unit the Day 17 audit log persists."""

    decision_id: str
    request: ApprovalRequest
    decision: Decision
    decided_by: str  # the approver's identity, or "policy" for NOT_REQUIRED
    decided_at: datetime
    note: str

    @property
    def released(self) -> bool:
        return self.decision in (Decision.APPROVED, Decision.NOT_REQUIRED)


class Verdict(_Record):
    """An approver's answer. ``decided_by`` must name who answered - an anonymous approval
    is not an approval."""

    approved: bool
    decided_by: str
    note: str = ""


class Approver(Protocol):
    def decide(self, request: ApprovalRequest) -> Verdict: ...


class DenyAll:
    """The default: nothing that needs a human is released when no human is wired in."""

    def decide(self, request: ApprovalRequest) -> Verdict:
        return Verdict(
            approved=False,
            decided_by="deny_all",
            note="no approver is configured; the gate fails closed",
        )


class ScriptedApprover:
    """Answers from a table keyed by ``action_id`` - for tests and scripted demos. An action
    the table does not name is denied, so a script cannot approve something by omission."""

    def __init__(self, verdicts: Mapping[str, bool], *, identity: str = "scripted") -> None:
        self._verdicts = dict(verdicts)
        self._identity = identity
        self.asked: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> Verdict:
        self.asked.append(request)
        if request.action_id not in self._verdicts:
            return Verdict(
                approved=False, decided_by=self._identity, note="not in the approval script"
            )
        approved = self._verdicts[request.action_id]
        return Verdict(
            approved=approved,
            decided_by=self._identity,
            note="approved by script" if approved else "denied by script",
        )


class ConsoleApprover:
    """Asks a person at the terminal. Only an explicit ``y``/``yes`` approves; anything
    else, including end of input, denies."""

    def __init__(
        self,
        identity: str,
        *,
        ask: Callable[[str], str] = input,
        show: Callable[[str], None] = print,
    ) -> None:
        if not identity.strip():
            raise ValueError("a console approver must say who is approving")
        self._identity = identity
        self._ask = ask
        self._show = show

    def decide(self, request: ApprovalRequest) -> Verdict:
        self._show(render_request(request))
        try:
            answer = self._ask("Approve? [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        approved = answer in ("y", "yes")
        return Verdict(
            approved=approved,
            decided_by=self._identity,
            note="approved at the console" if approved else f"denied at the console ({answer!r})",
        )


class GateResult(_Record):
    decisions: list[ApprovalDecision]

    @property
    def released(self) -> list[ApprovalDecision]:
        return [d for d in self.decisions if d.released]

    @property
    def withheld(self) -> list[ApprovalDecision]:
        return [d for d in self.decisions if not d.released]


class HitlGate:
    """Reviews a `CoordinatorResponse`'s recommendations and decides each one."""

    def __init__(
        self,
        approver: Approver | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._approver: Approver = approver if approver is not None else DenyAll()
        self._clock = clock

    def review(self, response: CoordinatorResponse) -> GateResult:
        return GateResult(decisions=[self.decide(r) for r in requests_for(response)])

    def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        if not request.needs_human:
            return self._record(
                request,
                Decision.NOT_REQUIRED,
                "policy",
                "read-only by every signal: not flagged, low risk, no production write recognised",
            )
        try:
            verdict = self._approver.decide(request)
        except Exception as exc:  # noqa: BLE001 - a broken approver must deny, not crash
            return self._record(
                request,
                Decision.DENIED,
                type(self._approver).__name__,
                f"approver failed, so the gate fails closed: {type(exc).__name__}: {exc}",
            )
        if not verdict.decided_by.strip():
            return self._record(
                request,
                Decision.DENIED,
                type(self._approver).__name__,
                "the approver did not say who decided; an anonymous approval is refused",
            )
        return self._record(
            request,
            Decision.APPROVED if verdict.approved else Decision.DENIED,
            verdict.decided_by,
            verdict.note,
        )

    def _record(
        self, request: ApprovalRequest, decision: Decision, by: str, note: str
    ) -> ApprovalDecision:
        return ApprovalDecision(
            decision_id=f"apr_{uuid4().hex[:8]}",
            request=request,
            decision=decision,
            decided_by=by,
            decided_at=self._clock(),
            note=note,
        )


# ------------------------------------------------------------------ building requests


def requests_for(response: CoordinatorResponse) -> list[ApprovalRequest]:
    """One request per recommendation in each agent's latest report, in report order."""
    latest: dict[AgentName, AgentResponse] = {}
    for report in response.agent_responses:
        latest[report.agent] = report
    requests: list[ApprovalRequest] = []
    for latest_report in latest.values():
        if isinstance(latest_report, IncidentAgentResponse):
            requests.extend(
                _from_action(response.request_id, latest_report, action)
                for action in latest_report.findings.recommended_actions
            )
        elif isinstance(latest_report, DeploymentAgentResponse):
            rollback = _from_rollback(response.request_id, latest_report)
            if rollback is not None:
                requests.append(rollback)
    return requests


def _from_action(
    request_id: str, report: IncidentAgentResponse, action: RecommendedAction
) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=request_id,
        invocation_id=report.invocation_id,
        agent=report.agent,
        source=ActionSource.RECOMMENDED_ACTION,
        action_id=action.id,
        action=action.action,
        rationale=action.rationale,
        risk=action.risk,
        risk_detail=action.risk_detail,
        reversible=action.reversible,
        target_service=action.target_service,
        command=action.command,
        mutations=classify(action.action, action.command),
        reasons=approval_reasons(action),
    )


def _from_rollback(request_id: str, report: DeploymentAgentResponse) -> ApprovalRequest | None:
    """A rollback recommendation is an action; hold, no action, and insufficient data are
    not. ``other`` is gated too - a recommendation the enum could not name is not one the
    gate can prove harmless."""
    f = report.findings
    rec = f.rollback_recommendation
    if rec.value not in (RollbackRecommendation.ROLLBACK_NOW, RollbackRecommendation.OTHER):
        return None
    releases = f.releases_compared
    if rec.value is RollbackRecommendation.ROLLBACK_NOW:
        target = releases.from_version or "the previous release"
        action = (
            f"Roll back {f.service} in {f.environment.value} from {releases.to_version} to {target}"
        )
        mutations = [MutationKind.ROLLBACK]
    else:
        action = f"Act on the deployment recommendation for {f.service}: {rec.detail}"
        mutations = classify(action)
    reasons = ["deployment approval is const requires_approval: true (CONTRACTS.md sec 4.4)"]
    if f.approval.risk in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.OTHER):
        reasons.append(f"risk is {f.approval.risk.value}")
    if mutations:
        reasons.append("mutates production state: " + ", ".join(m.value for m in mutations))
    return ApprovalRequest(
        request_id=request_id,
        invocation_id=report.invocation_id,
        agent=report.agent,
        source=ActionSource.ROLLBACK_RECOMMENDATION,
        action_id=f"act_{report.invocation_id.removeprefix('inv_')}_rollback",
        action=action,
        rationale=rec.reasoning or "",
        risk=f.approval.risk,
        risk_detail=f.approval.risk_detail,
        reversible=None,
        target_service=f.service,
        command=None,
        mutations=mutations,
        reasons=reasons,
        evidence=list(rec.evidence),
    )


def render_request(request: ApprovalRequest) -> str:
    """The request as a person reads it before answering."""
    lines = [
        f"Approval requested - {request.action_id} ({request.agent.value}, "
        f"invocation {request.invocation_id})",
        f"  action:    {request.action}",
    ]
    if request.command:
        lines.append(f"  command:   {request.command}")
    if request.target_service:
        lines.append(f"  service:   {request.target_service}")
    risk = request.risk.value + (f" ({request.risk_detail})" if request.risk_detail else "")
    reversible = {True: "yes", False: "NO", None: "not stated"}[request.reversible]
    lines += [
        f"  risk:      {risk}; reversible: {reversible}",
        f"  rationale: {request.rationale}",
        "  gated because:",
        *(f"    - {r}" for r in request.reasons),
    ]
    if request.evidence:
        lines.append(f"  evidence:  {', '.join(request.evidence)}")
    return "\n".join(lines)
