"""Coordinator: plan execution and response assembly (Day 7).

`Coordinator.plan` decides; this module acts. `Executor.execute` consumes a validated
`SelectionPlan`, runs every invocation it can, and assembles the contract's
`CoordinatorResponse` - which makes this file the place where the project's single
most-tested orchestration fact becomes executable: **each subagent receives exactly
`AgentInvocation.context_passed` as its context, and nothing else.** The executor holds the
query and the plan; it forwards the context block verbatim and forwards nothing the
coordinator knew but did not write into the plan. `tests/test_executor.py` asserts that
literally, argument by argument.

Three deliberate decisions, written down because the handoff asked for them:

**An invocation the executor cannot run produces a `Gap`, never a fabricated response.**
Incident (Day 4) and Docs (Day 8) are live; GitHub and Deployment land on Days 11 and 12,
and a plan can legitimately select any of the four. The contract-honest answer for an agent
that does not exist is a `Gap` with ``resolvable: false`` plus a `status` of ``partial`` or
weaker - a plausible placeholder `AgentResponse` is precisely the failure mode the
null-vs-`[]` rule exists to prevent. ``resolvable: false`` is load-bearing: the Day 14
refinement loop must not spend rounds re-delegating to an agent that is not there.

**Synthesis is deterministic on Day 7, not model-written.** The `answer` is adopted from the
highest-confidence agent report and cites that report's own evidence ids, so the
`CoordinatorResponse` invariant - the coordinator cites its subagents and never mints
evidence ids - holds by construction rather than by model compliance. A model-written
synthesis belongs with the refinement loop (Day 14), which is the first consumer that needs
one; buying it early would add a failure mode and a token cost to every request for prose
nobody reads yet.

**Execution is a plain loop, on purpose.** ``mode`` and ``depends_on`` are recorded in the
plan, so Day 9 changes the execution strategy (parallel Task calls) without touching either
the plan or this module's accounting. Building concurrency today would be building Day 9
early. The loop runs the parallel group first, then the sequential chain in dependency
order; a dependency that produced no response fails its dependents honestly rather than
running them against an input that never arrived.

Cost is measured, never estimated: one `Usage` accumulator threads through the planning
call and every agent call, and `CoordinatorResponse.cost` is read off it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import uuid4

from aioc.agents import DocsAgent, IncidentAgent
from aioc.contracts import (
    AgentInvocation,
    AgentName,
    AgentResponse,
    Assessment,
    CoordinatorResponse,
    Cost,
    Gap,
    GapKind,
    ResponseStatus,
    walk_assessments,
)
from aioc.llm import Usage

from .planner import Coordinator, SelectionPlan, utcnow


class AgentRunner(Protocol):
    """One runnable subagent, as the executor sees it.

    The protocol is the executor's seam: tests inject recording fakes, Day 8/11/12 register
    real agents, and the executor stays agnostic about what is behind it. ``context`` is the
    plan's ``context_passed`` verbatim - a runner must not be handed anything else.
    """

    def run(
        self,
        query: str,
        *,
        context: str,
        request_id: str,
        invocation_id: str,
        usage: Usage,
    ) -> AgentResponse: ...


class IncidentRunner:
    """Adapts `IncidentAgent.diagnose` to the `AgentRunner` protocol.

    The seam was already built on Day 4: ``diagnose`` takes exactly what the coordinator
    has, so the adapter is a straight pass-through with no reshaping - which is the point.
    Any glue that "enriched" the context here would be inheritance sneaking back in.
    """

    def __init__(self, agent: IncidentAgent | None = None) -> None:
        self._agent = agent or IncidentAgent()

    def run(
        self,
        query: str,
        *,
        context: str,
        request_id: str,
        invocation_id: str,
        usage: Usage,
    ) -> AgentResponse:
        return self._agent.diagnose(
            query,
            context=context,
            request_id=request_id,
            invocation_id=invocation_id,
            usage=usage,
        )


class DocsRunner:
    """Adapts `DocsAgent.answer` to the `AgentRunner` protocol - the same straight
    pass-through as `IncidentRunner`, for the same reason: any glue that "enriched" the
    context here would be inheritance sneaking back in. Retrieval is the agent's own tool
    call, not context - it happens inside the agent, after the handoff."""

    def __init__(self, agent: DocsAgent | None = None) -> None:
        self._agent = agent or DocsAgent()

    def run(
        self,
        query: str,
        *,
        context: str,
        request_id: str,
        invocation_id: str,
        usage: Usage,
    ) -> AgentResponse:
        return self._agent.answer(
            query,
            context=context,
            request_id=request_id,
            invocation_id=invocation_id,
            usage=usage,
        )


def default_runners() -> dict[AgentName, AgentRunner]:
    """Every agent that exists today. Days 11/12 add github and deployment."""
    return {AgentName.INCIDENT: IncidentRunner(), AgentName.DOCS: DocsRunner()}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


class Executor:
    """Runs a `SelectionPlan` and assembles the `CoordinatorResponse`."""

    def __init__(self, runners: dict[AgentName, AgentRunner] | None = None) -> None:
        self._runners = dict(runners) if runners is not None else dict(default_runners())

    def execute(
        self,
        plan: SelectionPlan,
        query: str,
        *,
        request_id: str | None = None,
        received_at: datetime | None = None,
        usage: Usage | None = None,
    ) -> CoordinatorResponse:
        """Run every invocation the plan selected, in dependency order.

        ``usage`` may arrive pre-seeded with the planning call's tokens (see `respond`);
        the agent calls add theirs, and ``cost`` is the total. ``received_at`` is the
        moment the request arrived, which is before planning ran - the caller who planned
        supplies it, a standalone call defaults to now.
        """
        usage = usage if usage is not None else Usage()
        request_id = request_id or Coordinator.new_request_id()
        received = received_at if received_at is not None else utcnow()

        responses: list[AgentResponse] = []
        execution_gaps: list[Gap] = []
        succeeded: set[str] = set()
        any_failure = False

        # Parallel group first, then the sequential chain in dependency order. The plan
        # validator guarantees depends_on resolves in-plan and is acyclic, so ordering the
        # chain by "all dependencies already attempted" always terminates.
        for inv in _execution_order(plan):
            runner = self._runners.get(inv.agent)
            if runner is None:
                execution_gaps.append(_agent_missing_gap(inv))
                continue
            unmet = [dep for dep in inv.depends_on if dep not in succeeded]
            if unmet:
                execution_gaps.append(_dependency_unmet_gap(inv, unmet))
                continue
            try:
                response = runner.run(
                    query,
                    context=inv.context_passed,
                    request_id=request_id,
                    invocation_id=inv.invocation_id,
                    usage=usage,
                )
            except Exception as exc:  # noqa: BLE001 - one agent failing must not kill the rest
                any_failure = True
                execution_gaps.append(_invocation_failed_gap(inv, query, exc))
                continue
            responses.append(response)
            succeeded.add(inv.invocation_id)

        answer, synthesis = _synthesise(plan, responses, execution_gaps)
        unresolved = [*plan.gaps, *execution_gaps, *(g for r in responses for g in r.gaps)]

        return CoordinatorResponse(
            request_id=request_id,
            query=query,
            received_at=received,
            intent=plan.intent,
            selected_agents=plan.selected_agents,
            skipped_agents=plan.skipped_agents,
            agent_responses=responses,  # type: ignore[arg-type]
            synthesis=synthesis,
            answer=answer,
            refinement_rounds=0,  # the refinement loop is Day 14
            unresolved_gaps=unresolved,
            status=_status(plan, responses, unresolved, any_failure),
            cost=Cost(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens),
            trace_id=None,  # Langfuse tracing is Day 9
            completed_at=utcnow(),
        )


def respond(
    query: str,
    *,
    situation: str | None = None,
    coordinator: Coordinator | None = None,
    executor: Executor | None = None,
) -> CoordinatorResponse:
    """Plan and execute one request end to end - the Day 10 demo entry point.

    One `Usage` accumulator covers the planning call and every agent call, so
    ``cost`` on the response is the whole request's real token spend.
    """
    coordinator = coordinator or Coordinator()
    executor = executor or Executor()
    usage = Usage()
    request_id = Coordinator.new_request_id()
    received_at = utcnow()
    plan = coordinator.plan(query, situation=situation, usage=usage)
    return executor.execute(
        plan, query, request_id=request_id, received_at=received_at, usage=usage
    )


# ------------------------------------------------------------------------ execution order


def _execution_order(plan: SelectionPlan) -> list[AgentInvocation]:
    """Parallel group, then sequential invocations in dependency order.

    Kahn's algorithm over the chain; the plan validator already rejected cycles and
    dangling ids, so this always drains. Ties keep plan order for determinism.
    """
    ordered = list(plan.parallel_group)
    attempted = {inv.invocation_id for inv in ordered}
    pending = list(plan.sequential_chain)
    while pending:
        ready = [inv for inv in pending if set(inv.depends_on) <= attempted]
        if not ready:  # pragma: no cover - the SelectionPlan validator makes this unreachable
            raise RuntimeError(
                "plan has an unsatisfiable depends_on despite validation; "
                f"stuck: {sorted(i.invocation_id for i in pending)}"
            )
        ordered.extend(ready)
        attempted.update(inv.invocation_id for inv in ready)
        pending = [inv for inv in pending if inv.invocation_id not in attempted]
    return ordered


# ---------------------------------------------------------------------------- honest gaps


def _agent_missing_gap(inv: AgentInvocation) -> Gap:
    # The interesting decision of the day, made explicit: no fabricated AgentResponse for
    # an agent that does not exist. resolvable=False stops the Day 14 loop from retrying
    # an absence that another round cannot fix.
    return Gap(
        id=_new_id("gap"),
        description=(
            f"The plan selected the {inv.agent.value} agent "
            f"(invocation {inv.invocation_id}), but that agent is not implemented yet - "
            "github and deployment land on Days 11 and 12. The invocation was "
            "not executed and no response was fabricated for it."
        ),
        kind=GapKind.OTHER,
        kind_detail="agent_not_implemented",
        blocks_field=None,
        suggested_agent=None,
        suggested_query=None,
        resolvable=False,
    )


def _dependency_unmet_gap(inv: AgentInvocation, unmet: list[str]) -> Gap:
    return Gap(
        id=_new_id("gap"),
        description=(
            f"Invocation {inv.invocation_id} ({inv.agent.value}) depends on "
            f"{', '.join(unmet)}, which produced no response; it was not executed rather "
            "than run against input that never arrived."
        ),
        kind=GapKind.MISSING_DATA,
        kind_detail=None,
        blocks_field=None,
        suggested_agent=None,
        suggested_query=None,
        resolvable=False,
    )


def _invocation_failed_gap(inv: AgentInvocation, query: str, exc: Exception) -> Gap:
    # A failed invocation is genuinely worth one more attempt - model nondeterminism and
    # transient upstreams both clear on retry - so this gap is machine-consumable by the
    # Day 14 loop: resolvable, with the agent and query to re-delegate spelled out.
    return Gap(
        id=_new_id("gap"),
        description=(
            f"Invocation {inv.invocation_id} ({inv.agent.value}) failed with "
            f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
        ),
        kind=GapKind.OTHER,
        kind_detail="agent_invocation_failed",
        blocks_field=None,
        suggested_agent=inv.agent,
        suggested_query=query,
        resolvable=True,
    )


# ------------------------------------------------------------------ synthesis and status


def _synthesise(
    plan: SelectionPlan,
    responses: list[AgentResponse],
    execution_gaps: list[Gap],
) -> tuple[Assessment[str], str]:
    """Deterministic synthesis - see the module docstring for why it is not model-written.

    The answer is adopted from the highest-confidence agent report and cites the evidence
    ids that report's own assessments cite, so every id already resolves inside
    ``agent_responses[].evidence`` and the coordinator never mints one.
    """
    lines: list[str] = []
    for r in responses:
        lines.append(
            f"- {r.agent.value} ({r.status.value}, confidence {r.overall_confidence:.2f}): "
            f"{r.summary}"
        )
    for gap in execution_gaps:
        lines.append(f"- not executed: {gap.description}")

    if not responses:
        synthesis = (
            "No selected agent could be executed for this query.\n" + "\n".join(lines)
            if lines
            else "No selected agent could be executed for this query."
        )
        answer = Assessment[str](
            value=None,
            confidence=0.0,
            evidence=[],
            reasoning="No selected agent produced a response; see unresolved_gaps.",
            detail=None,
        )
        return answer, synthesis

    primary = max(responses, key=lambda r: r.overall_confidence)
    cited = _cited_evidence(primary)
    confidence = primary.overall_confidence
    reasoning = (
        f"Adopted from the {primary.agent.value} agent's report, the highest-confidence "
        f"response of {len(responses)}."
    )
    if not cited:
        # A report can carry confidence while citing nothing only when its own status is
        # insufficient_evidence or error; an uncited answer must not claim the >= 0.5 band
        # the contract reserves for evidenced conclusions.
        confidence = min(confidence, 0.49)
        reasoning += " No evidence was cited by that report, so confidence is capped below 0.5."

    synthesis = (
        f"Synthesis of {len(responses)} agent response(s) "
        f"(intent: {plan.intent.value.value if plan.intent.value else 'unclassified'}):\n"
        + "\n".join(lines)
    )
    answer = Assessment[str](
        value=primary.summary,
        confidence=confidence,
        evidence=cited,
        reasoning=reasoning,
        detail=None,
    )
    return answer, synthesis


def _cited_evidence(response: AgentResponse) -> list[str]:
    """The evidence ids a report's own assessments cite, deduplicated, in citation order.

    Falls back to everything the report recorded: evidence an agent gathered but attached
    no conclusion to is still the basis of its summary.
    """
    seen: dict[str, None] = {}
    for _, assessment in walk_assessments(response.findings, "findings"):
        for ev in assessment.evidence:
            seen.setdefault(ev, None)
    if not seen:
        for ev_entry in response.evidence:
            seen.setdefault(ev_entry.id, None)
    return list(seen)


def _status(
    plan: SelectionPlan,
    responses: list[AgentResponse],
    unresolved: list[Gap],
    any_failure: bool,
) -> ResponseStatus:
    """Honest roll-up: `complete` only when everything ran, completely, with nothing open."""
    all_ran = len(responses) == len(plan.selected_agents)
    if responses:
        if (
            all_ran
            and not unresolved
            and all(r.status is ResponseStatus.COMPLETE for r in responses)
        ):
            return ResponseStatus.COMPLETE
        return ResponseStatus.PARTIAL
    return ResponseStatus.ERROR if any_failure else ResponseStatus.INSUFFICIENT_EVIDENCE
