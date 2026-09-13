"""Settling a report's `status` against what its findings actually contain (Day 13).

CONTRACTS.md sec 3: ``status`` must be ``partial`` or weaker whenever any analytic
``Assessment.value`` in the findings is null. The rule is mechanical - it follows from the
findings, and the runtime can see the findings - so it is enforced here rather than left to
the model's bookkeeping. The first live sequential run failed on exactly this: the
Deployment agent left one judgement null with an honest gap against it, then wrote
``status: complete``, and the whole response was refused for a field the runtime could
have settled itself. Same principle as the planner stamping ``round`` and the GitHub agent
stamping SHAs: a value the code already knows is a value the model can only get wrong.

Only the one direction is taken. ``complete`` with a null becomes ``partial``. A model that
says ``partial``, ``insufficient_evidence``, or ``error`` is believed - those are judgements
about what was answered, and weakening is never overridden upward.
"""

from __future__ import annotations

from pydantic import BaseModel

from aioc.contracts import ResponseStatus, walk_assessments


def settle_status(status: ResponseStatus, findings: BaseModel) -> ResponseStatus:
    """``partial`` instead of ``complete`` when a findings assessment is null; otherwise the
    status as reported."""
    if status is ResponseStatus.COMPLETE and any(
        a.value is None for _, a in walk_assessments(findings, "findings")
    ):
        return ResponseStatus.PARTIAL
    return status
