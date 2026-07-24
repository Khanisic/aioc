"""The `AgentResponse` envelope shared by all four agents (CONTRACTS.md sec 3).

The response-scoped invariants live here, because this is the first place the evidence
list and the gaps are visible together with the findings:

  - every `Assessment.evidence` id resolves against this response's ``evidence[]``;
  - ``status`` is ``partial`` or weaker whenever any analytic ``Assessment.value`` is null;
  - a null ``Assessment.value`` requires a `Gap` whose ``blocks_field`` references it;
  - ``evidence`` may be empty only when ``status`` is ``insufficient_evidence`` or ``error``.
"""

from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from .. import SCHEMA_VERSION
from ._common import StrictModel, check_other_detail
from .deployment import DeploymentFindings
from .docs import DocsFindings
from .enums import AgentName, ResponseStatus
from .github import GitHubFindings
from .incident import IncidentFindings
from .primitives import Assessment, Evidence, Gap, ToolCallRef

_EVIDENCE_OPTIONAL_STATUSES = (ResponseStatus.INSUFFICIENT_EVIDENCE, ResponseStatus.ERROR)


def walk_assessments(obj: object, path: str) -> Iterator[tuple[str, Assessment[Any]]]:
    """Yield ``(dotted_path, Assessment)`` for every `Assessment` nested inside ``obj``.

    Stops descending at an `Assessment` (its ``value`` is a scalar/enum, not a model).
    """
    if isinstance(obj, Assessment):
        yield path, obj
        return
    if isinstance(obj, BaseModel):
        for name in type(obj).model_fields:
            yield from walk_assessments(getattr(obj, name), f"{path}.{name}")
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            yield from walk_assessments(item, f"{path}[{i}]")


class AgentResponse(StrictModel):
    schema_version: str = SCHEMA_VERSION
    agent: AgentName
    request_id: str
    invocation_id: str
    status: ResponseStatus
    status_detail: str | None = None
    summary: str
    findings: BaseModel
    evidence: list[Evidence] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)
    tool_calls: list[ToolCallRef] = Field(default_factory=list)
    generated_at: datetime

    @model_validator(mode="after")
    def _check_envelope(self) -> "AgentResponse":
        check_other_detail(
            self.status, self.status_detail, field="status", detail_field="status_detail"
        )
        if not self.evidence and self.status not in _EVIDENCE_OPTIONAL_STATUSES:
            raise ValueError(
                "evidence may be empty only when status is insufficient_evidence or error "
                "(CONTRACTS.md sec 3)"
            )

        evidence_ids = {e.id for e in self.evidence}
        assessments = list(walk_assessments(self.findings, "findings"))

        if any(a.value is None for _, a in assessments) and self.status is ResponseStatus.COMPLETE:
            raise ValueError(
                "status 'complete' is invalid when a findings Assessment.value is null "
                "(CONTRACTS.md sec 3)"
            )

        for path, a in assessments:
            for ev in a.evidence:
                if ev not in evidence_ids:
                    raise ValueError(
                        f"Assessment at {path} references evidence id '{ev}' not present "
                        "in evidence[]"
                    )
            if a.value is not None and a.confidence >= 0.5 and not a.evidence:
                raise ValueError(
                    f"Assessment at {path}: evidence is required when value is set and "
                    "confidence >= 0.5 (CONTRACTS.md sec 2.1)"
                )
            if a.value is None and not any(
                g.blocks_field and g.blocks_field.startswith(path) for g in self.gaps
            ):
                raise ValueError(
                    f"null Assessment at {path} requires a Gap whose blocks_field references it "
                    "(CONTRACTS.md sec 2.1)"
                )
        return self


class IncidentAgentResponse(AgentResponse):
    agent: Literal[AgentName.INCIDENT] = AgentName.INCIDENT
    findings: IncidentFindings


class DocsAgentResponse(AgentResponse):
    agent: Literal[AgentName.DOCS] = AgentName.DOCS
    findings: DocsFindings


class GitHubAgentResponse(AgentResponse):
    agent: Literal[AgentName.GITHUB] = AgentName.GITHUB
    findings: GitHubFindings


class DeploymentAgentResponse(AgentResponse):
    agent: Literal[AgentName.DEPLOYMENT] = AgentName.DEPLOYMENT
    findings: DeploymentFindings


# Discriminated on `agent` so a raw payload parses into the correct concrete response.
AnyAgentResponse = Annotated[
    IncidentAgentResponse | DocsAgentResponse | GitHubAgentResponse | DeploymentAgentResponse,
    Field(discriminator="agent"),
]
