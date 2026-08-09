"""The coordinator: intent classification, dynamic agent selection, and (later) the loop.

Day 6 shipped the planning half - `Coordinator.plan` returns a validated `SelectionPlan`
naming which agents a query needs, what each is told, and which are deliberately skipped.
Day 7 shipped the execution half - `Executor.execute` consumes a plan into a contract
`CoordinatorResponse` with explicit context passing and honest gaps for what could not run,
and `respond` glues the two together for one-call use. The refinement loop is Day 14.
"""

from .executor import AgentRunner, Executor, IncidentRunner, default_runners, respond
from .planner import (
    ALL_AGENTS,
    SELECT_TOOL_NAME,
    SELECTION_SYSTEM_PROMPT,
    Coordinator,
    CoordinatorError,
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
    "Executor",
    "IncidentRunner",
    "SelectionPlan",
    "default_runners",
    "respond",
    "utcnow",
]
