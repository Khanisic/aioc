"""The coordinator: intent classification, dynamic agent selection, and (later) the loop.

Day 6 ships the planning half - `Coordinator.plan` returns a validated `SelectionPlan` naming
which agents a query needs, what each is told, and which are deliberately skipped. Task
delegation lands on Day 7 and the refinement loop on Day 14.
"""

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
    "Coordinator",
    "CoordinatorError",
    "SelectionPlan",
    "utcnow",
]
