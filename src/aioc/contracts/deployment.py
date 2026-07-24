"""`DeploymentFindings` and its sub-types (CONTRACTS.md sec 4.4).

Config values are never returned by any tool or agent, at any layer - keys only. Keys
leak nothing; values leak connection strings and API keys.
"""

from typing import Literal

from pydantic import Field, model_validator

from ._common import StrictModel, check_other_detail
from .enums import Environment, RiskLevel, RollbackRecommendation, RolloutStatus
from .primitives import Assessment


class ReleasesCompared(StrictModel):
    from_version: str | None = None  # null = first release
    to_version: str


class ImageChange(StrictModel):
    container: str
    from_image: str | None = None
    to_image: str
    from_digest: str | None = None
    to_digest: str | None = None


class HealthSignals(StrictModel):
    """All fields nullable. A signal that could not be measured is ``null``, never ``0``."""

    replicas_desired: int | None = None
    replicas_ready: int | None = None
    restart_count: int | None = None
    probe_failures: int | None = None
    error_rate: float | None = None
    p99_latency_ms: float | None = None
    observed_over_seconds: int | None = None


class ApprovalRequirement(StrictModel):
    """``requires_approval`` is const ``true`` here - every deployment action is human-gated."""

    requires_approval: Literal[True] = True
    risk: RiskLevel
    risk_detail: str | None = None
    blast_radius: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "ApprovalRequirement":
        check_other_detail(self.risk, self.risk_detail, field="risk", detail_field="risk_detail")
        return self


class DeploymentFindings(StrictModel):
    service: str
    environment: Environment
    environment_detail: str | None = None
    releases_compared: ReleasesCompared
    rollout_status: Assessment[RolloutStatus]
    changed_config_keys: list[str] = Field(default_factory=list)  # keys only, never values
    image_changes: list[ImageChange] = Field(default_factory=list)
    health_signals: HealthSignals
    regression_suspected: Assessment[bool]
    rollback_recommendation: Assessment[RollbackRecommendation]
    approval: ApprovalRequirement

    @model_validator(mode="after")
    def _check(self) -> "DeploymentFindings":
        check_other_detail(
            self.environment,
            self.environment_detail,
            field="environment",
            detail_field="environment_detail",
        )
        return self
