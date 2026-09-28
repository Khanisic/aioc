"""The coordinator: intent classification, dynamic agent selection, and (later) the loop.

Day 6 shipped the planning half - `Coordinator.plan` returns a validated `SelectionPlan`
naming which agents a query needs, what each is told, and which are deliberately skipped.
Day 7 shipped the execution half - `Executor.execute` consumes a plan into a contract
`CoordinatorResponse` with explicit context passing and honest gaps for what could not run,
and `respond` glues the two together for one-call use. Day 13 made the sequential chain a
real handoff - `handoff.digest` is what a dependent is told about its dependency, appended
to the planner's block and recorded in `context_passed`. Day 14 added the refinement loop
(the executor re-delegates open gaps off `suggested_agent` + `suggested_query`, round by
round, up to a cap) and the synthesis seam (`synthesis.deterministic` by default,
`ModelSynthesiser` opt-in at the entry point, grounded in code with a deterministic
fallback).
"""

from .confidence import (
    BANDS,
    Band,
    FieldConfidence,
    Flag,
    RequestProfile,
    ResponseProfile,
    band,
    profile,
    request_profile,
)
from .executor import (
    DEFAULT_MAX_REFINEMENT_ROUNDS,
    AgentRunner,
    DocsRunner,
    Executor,
    IncidentRunner,
    default_runners,
    respond,
)
from .handoff import compose_dependent_context, digest, refinement_block, refinement_query
from .planner import (
    ALL_AGENTS,
    SELECT_TOOL_NAME,
    SELECTION_SYSTEM_PROMPT,
    Coordinator,
    CoordinatorError,
    ModelSelectionPlan,
    PlannedInvocation,
    SelectionPlan,
    utcnow,
)
from .provenance import (
    ClaimProvenance,
    ClaimSource,
    CoverageGap,
    CoverageReport,
    DocsProvenance,
    coverage_gaps,
    provenance,
)
from .synthesis import (
    ModelSynthesiser,
    Synthesis,
    Synthesiser,
    SynthesisError,
    SynthesisRequest,
    deterministic,
)

__all__ = [
    "ALL_AGENTS",
    "BANDS",
    "Band",
    "ClaimProvenance",
    "ClaimSource",
    "CoverageGap",
    "CoverageReport",
    "DocsProvenance",
    "FieldConfidence",
    "Flag",
    "RequestProfile",
    "ResponseProfile",
    "band",
    "coverage_gaps",
    "profile",
    "provenance",
    "request_profile",
    "DEFAULT_MAX_REFINEMENT_ROUNDS",
    "SELECTION_SYSTEM_PROMPT",
    "SELECT_TOOL_NAME",
    "AgentRunner",
    "Coordinator",
    "CoordinatorError",
    "DocsRunner",
    "Executor",
    "IncidentRunner",
    "ModelSelectionPlan",
    "ModelSynthesiser",
    "PlannedInvocation",
    "SelectionPlan",
    "Synthesis",
    "SynthesisError",
    "SynthesisRequest",
    "Synthesiser",
    "compose_dependent_context",
    "default_runners",
    "deterministic",
    "digest",
    "refinement_block",
    "refinement_query",
    "respond",
    "utcnow",
]
