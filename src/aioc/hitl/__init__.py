"""Human-in-the-loop approval (Day 16): the approval rule as code (`policy`) and the gate
that decides every recommended production write before anything could act on it (`gate`).
Day 17 added the audit log (`audit`): every decision is written before it is returned, to a
store that only appends."""

from .audit import (
    AuditLog,
    AuditLogError,
    MemoryAuditLog,
    PostgresAuditLog,
    default_audit_log,
)
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
    "AuditLog",
    "AuditLogError",
    "ConsoleApprover",
    "Decision",
    "DenyAll",
    "GateResult",
    "HitlGate",
    "MemoryAuditLog",
    "MutationKind",
    "PostgresAuditLog",
    "ScriptedApprover",
    "Verdict",
    "approval_reasons",
    "classify",
    "default_audit_log",
    "render_request",
    "requests_for",
    "requires_approval",
]
