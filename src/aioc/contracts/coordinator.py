"""`CoordinatorResponse` and its sub-types (CONTRACTS.md sec 5)."""

from datetime import datetime

from pydantic import Field, model_validator

from .. import SCHEMA_VERSION
from ._common import StrictModel
from .enums import AgentName, Intent, InvocationMode, ResponseStatus
from .envelope import AnyAgentResponse
from .primitives import Assessment, Gap


class AgentInvocation(StrictModel):
    """Schema-level proof of the project's single most-tested orchestration fact: each
    subagent receives its context explicitly in its prompt, with no automatic inheritance.
    """

    invocation_id: str
    agent: AgentName
    reason: str
    mode: InvocationMode
    depends_on: list[str] = Field(default_factory=list)
    context_passed: str
    round: int

    @model_validator(mode="after")
    def _check(self) -> "AgentInvocation":
        if not self.context_passed.strip():
            raise ValueError(
                "context_passed must be non-empty - an empty value means context was assumed "
                "inherited, the exact failure this project demonstrates the absence of "
                "(CONTRACTS.md sec 5)"
            )
        if self.mode is InvocationMode.PARALLEL and self.depends_on:
            raise ValueError("depends_on must be empty when mode == parallel (CONTRACTS.md sec 5)")
        if self.mode is InvocationMode.SEQUENTIAL and not self.depends_on:
            raise ValueError(
                "depends_on must be non-empty when mode == sequential (CONTRACTS.md sec 5)"
            )
        return self


class SkippedAgent(StrictModel):
    """Dynamic-selection evidence. An empty ``skipped_agents`` list on a typical query
    indicates dynamic selection is not working."""

    agent: AgentName
    reason: str


class Cost(StrictModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    usd: float | None = None


class CoordinatorResponse(StrictModel):
    schema_version: str = SCHEMA_VERSION
    request_id: str
    query: str
    received_at: datetime
    intent: Assessment[Intent]
    selected_agents: list[AgentInvocation] = Field(default_factory=list)
    skipped_agents: list[SkippedAgent] = Field(default_factory=list)
    agent_responses: list[AnyAgentResponse] = Field(default_factory=list)
    synthesis: str
    answer: Assessment[str]
    refinement_rounds: int
    unresolved_gaps: list[Gap] = Field(default_factory=list)
    status: ResponseStatus
    cost: Cost
    trace_id: str | None = None
    completed_at: datetime

    @model_validator(mode="after")
    def _check(self) -> "CoordinatorResponse":
        # At coordinator level, evidence ids resolve against the union of the subagents'
        # evidence - the coordinator cites its subagents rather than duplicating.
        union = {e.id for r in self.agent_responses for e in r.evidence}
        for ev in self.answer.evidence:
            if ev not in union:
                raise ValueError(
                    f"answer references evidence id '{ev}' not present in any agent response "
                    "(CONTRACTS.md sec 2.1)"
                )
        if (
            self.answer.value is not None
            and self.answer.confidence >= 0.5
            and not self.answer.evidence
        ):
            raise ValueError(
                "answer: evidence is required when value is set and confidence >= 0.5 "
                "(CONTRACTS.md sec 2.1)"
            )
        # `intent` is the one exemption: it is derived from the query text alone and is
        # permitted evidence: [] at any confidence, so it is not checked here.
        return self
