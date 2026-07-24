"""`IncidentFindings` and its sub-types (CONTRACTS.md sec 4.1)."""

from datetime import datetime

from pydantic import Field, model_validator

from ._common import StrictModel, check_other_detail
from .enums import FailureMode, RiskLevel, Severity, TimelineEventKind
from .primitives import Assessment


class IncidentWindow(StrictModel):
    start: datetime
    end: datetime | None = None  # null = ongoing


class TimelineEvent(StrictModel):
    id: str
    at: datetime
    service: str
    kind: TimelineEventKind
    kind_detail: str | None = None
    description: str
    severity: Severity | None = None
    evidence_id: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "TimelineEvent":
        check_other_detail(self.kind, self.kind_detail, field="kind", detail_field="kind_detail")
        return self


class Impact(StrictModel):
    """Every field is nullable. An unmeasured metric is ``null``, never ``0``."""

    error_rate_before: float | None = None
    error_rate_after: float | None = None
    p50_latency_ms_before: int | None = None
    p50_latency_ms_after: int | None = None
    p99_latency_ms_before: int | None = None
    p99_latency_ms_after: int | None = None
    requests_affected: int | None = None
    duration_seconds: int | None = None


class RecommendedAction(StrictModel):
    """The input to the HITL approval gate."""

    id: str
    action: str
    rationale: str
    risk: RiskLevel
    risk_detail: str | None = None
    reversible: bool
    requires_approval: bool
    target_service: str | None = None
    command: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "RecommendedAction":
        check_other_detail(self.risk, self.risk_detail, field="risk", detail_field="risk_detail")
        # Approval rule (CONTRACTS.md sec 4.1). The "mutates production state" half of the
        # rule is semantic and enforced by the agent prompt / HITL gate; the risk-based
        # half is structural and enforced here.
        needs_approval = self.risk in (RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.OTHER)
        if needs_approval and not self.requires_approval:
            raise ValueError(
                "requires_approval must be true when risk is medium, high, or other "
                "(CONTRACTS.md sec 4.1)"
            )
        return self


class IncidentFindings(StrictModel):
    incident_window: IncidentWindow
    affected_services: list[str] = Field(default_factory=list)
    severity: Assessment[Severity]
    failure_mode: Assessment[FailureMode]
    root_cause: Assessment[str]
    contributing_factors: list[Assessment[str]] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    impact: Impact
    recommended_actions: list[RecommendedAction] = Field(default_factory=list)
    similar_incidents: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "IncidentFindings":
        ats = [e.at for e in self.timeline]
        if ats != sorted(ats):
            raise ValueError("timeline must be ascending by `at` (CONTRACTS.md sec 4.1)")
        return self
