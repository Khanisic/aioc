"""Shared primitives: `Assessment`, `Evidence`, `Gap`, `ToolCallRef` (CONTRACTS.md sec 2)."""

from datetime import datetime
from enum import Enum
from typing import TypeVar

from pydantic import Field, model_validator

from ._common import StrictModel, check_other_detail
from .enums import AgentName, ErrorClass, GapKind, SourceType

T = TypeVar("T")


class Assessment[T](StrictModel):
    """The confidence carrier for analytic fields (anything inferred, judged, concluded).

    Factual scalars stay plain - wrapping them adds tokens and implies a judgement that
    isn't there. Confidence bands are normative (CONTRACTS.md sec 2.1); the eval harness
    scores calibration against them.

    Locally validated invariants:
      - ``confidence`` in ``[0, 1]``.
      - ``confidence < 0.25`` implies ``value is None`` (speculation is a gap, not a guess).
      - ``detail`` is non-null exactly when ``value`` is the enum member ``other``.

    The evidence-resolution and "null value implies a matching Gap" invariants are
    response-scoped and are enforced by the `AgentResponse` / `CoordinatorResponse`
    envelope, which is where the evidence list and gaps are visible.
    """

    value: T | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    reasoning: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "Assessment[T]":
        if self.confidence < 0.25 and self.value is not None:
            raise ValueError(
                "confidence < 0.25 requires value to be null - set value to null and emit a "
                "Gap instead of recording speculation (CONTRACTS.md sec 2.1)"
            )
        if isinstance(self.value, Enum) and self.value.value == "other":
            if self.detail is None:
                raise ValueError("detail must be non-null when Assessment.value == 'other'")
        elif self.detail is not None:
            raise ValueError(
                "detail must be null unless Assessment.value is the enum member 'other'"
            )
        return self


class Evidence(StrictModel):
    """A verbatim slice of source data that an `Assessment` can cite by id."""

    id: str
    source_type: SourceType
    source_type_detail: str | None = None
    source_ref: str
    excerpt: str
    observed_at: datetime | None = None
    uri: str | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "Evidence":
        check_other_detail(
            self.source_type,
            self.source_type_detail,
            field="source_type",
            detail_field="source_type_detail",
        )
        return self


class Gap(StrictModel):
    """What an agent could not establish - the input the coordinator refinement loop consumes.

    ``suggested_agent`` + ``suggested_query`` exist for a machine: the loop re-delegates
    directly off them. ``resolvable: false`` is what stops the loop from spinning, so it
    must be set honestly.
    """

    id: str
    description: str
    kind: GapKind
    kind_detail: str | None = None
    blocks_field: str | None = None
    suggested_agent: AgentName | None = None
    suggested_query: str | None = None
    resolvable: bool

    @model_validator(mode="after")
    def _check(self) -> "Gap":
        check_other_detail(self.kind, self.kind_detail, field="kind", detail_field="kind_detail")
        if self.suggested_agent is not None and self.suggested_query is None:
            raise ValueError(
                "suggested_query must be non-null when suggested_agent is set "
                "(CONTRACTS.md sec 2.3)"
            )
        return self


class ToolCallRef(StrictModel):
    """A record of one tool call, emitted for tracing and the token-reduction measurement."""

    id: str
    tool_name: str
    server: str
    started_at: datetime
    duration_ms: int
    ok: bool
    error_class: ErrorClass | None = None
    tokens_returned: int | None = None
    truncated: bool

    @model_validator(mode="after")
    def _check(self) -> "ToolCallRef":
        if self.ok and self.error_class is not None:
            raise ValueError("error_class must be null when ok is true")
        if not self.ok and self.error_class is None:
            raise ValueError("error_class must be non-null when ok is false")
        return self
