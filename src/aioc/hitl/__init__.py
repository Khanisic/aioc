"""Human-in-the-loop approval (Day 16): the approval rule as code (`policy`) and the gate
that decides every recommended production write before anything could act on it (`gate`)."""

from .gate import (
    ActionSource,
    ApprovalDecision,
    ApprovalRequest,
    Approver,
    ConsoleApprover,
    Decision,
    DenyAll,
    GateResult,
    HitlGate,
    ScriptedApprover,
    Verdict,
    render_request,
    requests_for,
)
from .policy import MutationKind, approval_reasons, classify, requires_approval

__all__ = [
    "ActionSource",
    "ApprovalDecision",
    "ApprovalRequest",
    "Approver",
    "ConsoleApprover",
    "Decision",
    "DenyAll",
    "GateResult",
    "HitlGate",
    "MutationKind",
    "ScriptedApprover",
    "Verdict",
    "approval_reasons",
    "classify",
    "render_request",
    "requests_for",
    "requires_approval",
]
