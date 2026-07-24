"""`GitHubFindings` and its sub-types (CONTRACTS.md sec 4.3).

Factual fields stay plain scalars; judgements are wrapped in `Assessment`.
"""

from datetime import datetime

from pydantic import Field, model_validator

from ._common import StrictModel, check_other_detail
from .enums import ChangeType, PullRequestState, RiskLevel
from .primitives import Assessment


class PullRequestAnalysis(StrictModel):
    number: int
    title: str
    state: PullRequestState
    state_detail: str | None = None
    merged_at: datetime | None = None
    head_sha: str
    files_changed: int
    additions: int
    deletions: int
    touched_paths: list[str] = Field(default_factory=list)
    risk: Assessment[RiskLevel]
    summary: Assessment[str]

    @model_validator(mode="after")
    def _check(self) -> "PullRequestAnalysis":
        check_other_detail(
            self.state, self.state_detail, field="state", detail_field="state_detail"
        )
        return self


class CommitRef(StrictModel):
    """All plain - these are facts."""

    sha: str
    short_sha: str
    message: str
    authored_at: datetime
    touched_paths: list[str] = Field(default_factory=list)
    pull_request_number: int | None = None


class SuspectChange(StrictModel):
    """Links a code change to an observed symptom - the join point with `IncidentFindings`."""

    change_ref: str  # SHA or #PR
    change_type: ChangeType
    change_type_detail: str | None = None
    symptom_link: Assessment[str]  # how this change explains the symptom

    @model_validator(mode="after")
    def _check(self) -> "SuspectChange":
        check_other_detail(
            self.change_type,
            self.change_type_detail,
            field="change_type",
            detail_field="change_type_detail",
        )
        return self


class GitHubFindings(StrictModel):
    repository: str  # owner/name
    ref: str | None = None
    pull_requests: list[PullRequestAnalysis] = Field(default_factory=list)
    commits: list[CommitRef] = Field(default_factory=list)
    suspect_changes: list[SuspectChange] = Field(default_factory=list)
    diff_summary: Assessment[str]
