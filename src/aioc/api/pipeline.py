"""What one request runs: `respond()` with the deployed service's tracer and gate (Day 24).

Two modes, one path. Both go through `respond()` with the process's tracer and a HITL
gate writing to the audit log, so the trace, the gate, and the audit rows a smoke request
leaves are the ones a live request leaves:

- ``smoke`` plans without a model and runs two scripted agents (`aioc.api.smoke`). Zero
  Claude calls; it proves the deployment is wired - Postgres, the audit log, Langfuse,
  real executor concurrency - for free.
- ``live`` is the real coordinator and the four agents: the HANDOFF sec 1 exception,
  about $0.55 a request with no refinement and the deterministic synthesis, and only when
  `AIOC_API_LIVE` is on.

The gate's approver is `DenyAll`: a deployed service has nobody at a console, so every
production write an agent recommends is withheld and recorded as withheld. Approving one
is a human act this service does not offer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aioc.contracts import CoordinatorResponse
from aioc.coordinator import Coordinator, Executor
from aioc.coordinator.executor import respond
from aioc.coordinator.synthesis import ModelSynthesiser
from aioc.hitl import ApprovalDecision, AuditLog, HitlGate
from aioc.observability.tracing import LangfuseTracer, Tracer

from .smoke import SMOKE_SLEEP_SECONDS, SmokeCoordinator, smoke_runners

Mode = Literal["smoke", "live"]


class RequestBody(BaseModel):
    """What a client sends. Unknown fields are refused, like every contract model."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=4_000)
    situation: str | None = Field(
        default=None,
        max_length=20_000,
        description="What is known about the incident right now (the planner's situation "
        "block); without it the agents have only the query.",
    )
    mode: Mode = "smoke"
    synthesis: Literal["deterministic", "model"] = "deterministic"
    max_refinement_rounds: int = Field(default=0, ge=0, le=2)


@dataclass(frozen=True, slots=True)
class Outcome:
    response: CoordinatorResponse
    approvals: list[ApprovalDecision] = field(default_factory=list)
    trace_url: str | None = None


def build_pipeline(
    tracer: Tracer, audit: AuditLog, *, smoke_sleep_seconds: float = SMOKE_SLEEP_SECONDS
) -> Callable[[RequestBody], Outcome]:
    """One request -> its response, its gate decisions, and a link to its trace."""

    def run(request: RequestBody) -> Outcome:
        gate = HitlGate(audit=audit)
        coordinator: Coordinator
        if request.mode == "smoke":
            coordinator = SmokeCoordinator()
            executor = Executor(
                smoke_runners(sleep_seconds=smoke_sleep_seconds),
                tracer=tracer,
                max_refinement_rounds=0,
            )
        else:
            coordinator = Coordinator()
            executor = Executor(
                tracer=tracer,
                synthesiser=ModelSynthesiser() if request.synthesis == "model" else None,
                max_refinement_rounds=request.max_refinement_rounds,
            )
        try:
            response = respond(
                request.query,
                situation=request.situation,
                coordinator=coordinator,
                executor=executor,
                tracer=tracer,
                gate=gate,
            )
        finally:
            # A worker thread in a long-lived process: export now, so the trace is in
            # Langfuse by the time a client reads the job back.
            tracer.flush()
        approvals = audit.decisions(request_id=response.request_id)
        trace_url = None
        if isinstance(tracer, LangfuseTracer) and response.trace_id is not None:
            trace_url = tracer.trace_url(response.trace_id)
        return Outcome(response, approvals, trace_url)

    return run
