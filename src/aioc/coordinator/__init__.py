"""The coordinator: intent classification, dynamic agent selection, and (later) the loop.

Day 6 shipped the planning half - `Coordinator.plan` returns a validated `SelectionPlan`
naming which agents a query needs, what each is told, and which are deliberately skipped.
Day 7 shipped the execution half - `Executor.execute` consumes a plan into a contract
`CoordinatorResponse` with explicit context passing and honest gaps for what could not run,
and `respond` glues the two together for one-call use. Day 13 made the sequential chain a
real handoff - `handoff.digest` is what a dependent is told about its dependency, appended
to the planner's block and recorded in `context_passed`. The refinement loop is Day 14.
"""

from .executor import (
    AgentRunner,
    DocsRunner,
    Executor,
    IncidentRunner,
    default_runners,
    respond,
)
from .handoff import compose_dependent_context, digest
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

__all__ = [
    "ALL_AGENTS",
    "SELECTION_SYSTEM_PROMPT",
    "SELECT_TOOL_NAME",
    "AgentRunner",
    "Coordinator",
    "CoordinatorError",
    "DocsRunner",
    "Executor",
    "IncidentRunner",
    "ModelSelectionPlan",
    "PlannedInvocation",
    "SelectionPlan",
    "compose_dependent_context",
    "default_runners",
    "digest",
    "respond",
    "utcnow",
]
