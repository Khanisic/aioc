"""Coordinator: plan execution and response assembly (Day 7; parallel + traced on Day 9).

`Coordinator.plan` decides; this module acts. `Executor.execute` consumes a validated
`SelectionPlan`, runs every invocation it can, and assembles the contract's
`CoordinatorResponse` - which makes this file the place where the project's single
most-tested orchestration fact becomes executable: **each subagent receives exactly
`AgentInvocation.context_passed` as its context, and nothing else.** The executor holds the
query and the plan; it forwards the context block verbatim and forwards nothing the
coordinator knew but did not write into the plan. `tests/test_executor.py` asserts that
literally, argument by argument.

The deliberate decisions, written down because the handoff asked for them:

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

**The parallel group actually runs in parallel (Day 9).** Independent invocations
(``mode: parallel``, empty ``depends_on``) run concurrently on a thread pool - the agents
block on the Anthropic SDK, so threads buy real wall-clock overlap without rewriting them
as async. The sequential chain then runs in dependency order, one at a time; a dependency
that produced no response fails its dependents honestly rather than running them against
an input that never arrived. Results are merged in *plan* order, not completion order, so
the response is deterministic either way.

**Every runner gets its own `Usage` accumulator, merged after the join.** The accumulator
is a plain mutable object, and ``+=`` on a shared one from two threads loses counts
silently - which would corrupt ``cost`` in a way nothing downstream can detect. Rather
than hide a lock inside `Usage` (making every single-threaded consumer pay for this one
call site), the executor hands each runner a fresh accumulator and folds them into the
request total once the runner returns. The merge is single-threaded by construction.

Cost is measured, never estimated: the planning call and every agent call land in one
total, and `CoordinatorResponse.cost` is read off it.

**Tracing is opt-in at the entry point (Day 9).** The default is `NullTracer`, and the
live entry points pass `default_tracer()` explicitly - the offline suite must stay
network-free even on a machine whose `.env` carries real Langfuse keys. One trace per
request; one span per agent invocation, opened and closed in the worker thread that runs
it, so span timing is the real wall clock and a Langfuse trace of a parallel plan visibly
overlaps - which is the Day 9 checkpoint artifact.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from aioc.agents import DocsAgent, GitHubAgent, IncidentAgent
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
from aioc.observability.tracing import NullTracer, RequestTrace, Tracer

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


class GitHubRunner:
    """Adapts `GitHubAgent.analyze` to the `AgentRunner` protocol - the same straight
    pass-through as the other two, for the same reason. The agent's tool calls over the
    MCP wire happen inside the agent, after the handoff; the runner passes exactly
    ``context_passed`` and nothing else."""

    def __init__(self, agent: GitHubAgent | None = None) -> None:
        self._agent = agent or GitHubAgent()

    def run(
        self,
        query: str,
        *,
        context: str,
        request_id: str,
        invocation_id: str,
        usage: Usage,
    ) -> AgentResponse:
        return self._agent.analyze(
            query,
            context=context,
            request_id=request_id,
            invocation_id=invocation_id,
            usage=usage,
        )


def default_runners() -> dict[AgentName, AgentRunner]:
    """Every agent that exists today. Day 12 adds deployment."""
    return {
        AgentName.INCIDENT: IncidentRunner(),
        AgentName.DOCS: DocsRunner(),
        AgentName.GITHUB: GitHubRunner(),
    }


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


@dataclass(slots=True)
class _Outcome:
    """What one attempted invocation came back with - exactly one of response/error is
    set, and ``usage`` counts the tokens it spent either way (a failed call still cost)."""

    invocation: AgentInvocation
    response: AgentResponse | None
    error: Exception | None
    usage: Usage


class Executor:
    """Runs a `SelectionPlan` and assembles the `CoordinatorResponse`."""

    def __init__(
        self,
        runners: dict[AgentName, AgentRunner] | None = None,
        *,
        tracer: Tracer | None = None,
    ) -> None:
        self._runners = dict(runners) if runners is not None else dict(default_runners())
        # NullTracer by default, never default_tracer(): tracing activates only when an
        # entry point passes a tracer, so the offline suite cannot emit spans by accident.
        self._tracer: Tracer = tracer if tracer is not None else NullTracer()

    def execute(
        self,
        plan: SelectionPlan,
        query: str,
        *,
        request_id: str | None = None,
        received_at: datetime | None = None,
        usage: Usage | None = None,
        trace: RequestTrace | None = None,
    ) -> CoordinatorResponse:
        """Run every invocation the plan selected: the parallel group concurrently, then
        the sequential chain in dependency order.

        ``usage`` may arrive pre-seeded with the planning call's tokens (see `respond`);
        the agent calls add theirs, and ``cost`` is the total. ``received_at`` is the
        moment the request arrived, which is before planning ran - the caller who planned
        supplies it, a standalone call defaults to now. ``trace`` is an already-open
        request trace when the caller owns the whole request (again `respond`, which also
        traced the planning call); a standalone call opens and closes its own.
        """
        usage = usage if usage is not None else Usage()
        request_id = request_id or Coordinator.new_request_id()
        received = received_at if received_at is not None else utcnow()

        owns_trace = trace is None
        if trace is None:
            trace = self._tracer.start_request(
                "coordinator_request", request_id=request_id, query=query
            )

        responses: list[AgentResponse] = []
        execution_gaps: list[Gap] = []
        succeeded: set[str] = set()
        any_failure = False

        def settle(outcome: _Outcome) -> None:
            # Single-threaded merge point: called only after the worker has returned, in
            # plan order, so the shared accumulator and the result lists never race.
            nonlocal any_failure
            usage.add(outcome.usage)
            if outcome.response is not None:
                responses.append(outcome.response)
                succeeded.add(outcome.invocation.invocation_id)
            elif outcome.error is not None:
                any_failure = True
                execution_gaps.append(
                    _invocation_failed_gap(outcome.invocation, query, outcome.error)
                )

        # -- the parallel group, concurrently. The plan validator guarantees every member
        # has depends_on == [], so there is nothing to wait on and no unmet-dependency case.
        group: list[tuple[AgentInvocation, AgentRunner]] = []
        for inv in plan.parallel_group:
            runner = self._runners.get(inv.agent)
            if runner is None:
                execution_gaps.append(_agent_missing_gap(inv))
            else:
                group.append((inv, runner))
        if len(group) == 1:
            inv, runner = group[0]
            settle(self._run_invocation(inv, runner, query, request_id, trace))
        elif group:
            with ThreadPoolExecutor(
                max_workers=len(group), thread_name_prefix="aioc-agent"
            ) as pool:
                futures = [
                    pool.submit(self._run_invocation, inv, runner, query, request_id, trace)
                    for inv, runner in group
                ]
                # future.result() never raises here: _run_invocation catches the runner's
                # exception and returns it as data, so one failure cannot hide the others.
                for future in futures:
                    settle(future.result())

        # -- the sequential chain, one at a time in dependency order.
        for inv in _sequential_order(plan):
            runner = self._runners.get(inv.agent)
            if runner is None:
                execution_gaps.append(_agent_missing_gap(inv))
                continue
            unmet = [dep for dep in inv.depends_on if dep not in succeeded]
            if unmet:
                execution_gaps.append(_dependency_unmet_gap(inv, unmet))
                continue
            settle(self._run_invocation(inv, runner, query, request_id, trace))

        answer, synthesis = _synthesise(plan, responses, execution_gaps)
        unresolved = [*plan.gaps, *execution_gaps, *(g for r in responses for g in r.gaps)]
        status = _status(plan, responses, unresolved, any_failure)

        response = CoordinatorResponse(
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
            status=status,
            cost=Cost(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens),
            trace_id=trace.trace_id,
            completed_at=utcnow(),
        )
        if owns_trace:
            trace.end(
                output=answer.value if answer.value is not None else synthesis,
                status=status.value,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
        return response

    def _run_invocation(
        self,
        inv: AgentInvocation,
        runner: AgentRunner,
        query: str,
        request_id: str,
        trace: RequestTrace,
    ) -> _Outcome:
        """Run one invocation to an `_Outcome` - possibly on a worker thread, so it never
        raises and never touches shared state; `settle` folds the result in afterwards.

        The span opens and closes here, in the thread doing the work, so its timing is the
        agent's real wall clock - concurrent agents show as overlapping spans.
        """
        local = Usage()
        span = trace.start_span(
            f"agent:{inv.agent.value}",
            input_text=inv.context_passed,
            metadata={
                "invocation_id": inv.invocation_id,
                "mode": inv.mode.value,
                "round": inv.round,
            },
        )
        try:
            response = runner.run(
                query,
                context=inv.context_passed,
                request_id=request_id,
                invocation_id=inv.invocation_id,
                usage=local,
            )
        except Exception as exc:  # noqa: BLE001 - one agent failing must not kill the rest
            span.end(
                output=None,
                status="error",
                input_tokens=local.input_tokens,
                output_tokens=local.output_tokens,
                error=f"{type(exc).__name__}: {exc}",
            )
            return _Outcome(invocation=inv, response=None, error=exc, usage=local)
        for ref in response.tool_calls:
            span.record_tool_call(ref)
        span.end(
            output=response.summary,
            status=response.status.value,
            input_tokens=local.input_tokens,
            output_tokens=local.output_tokens,
        )
        return _Outcome(invocation=inv, response=response, error=None, usage=local)


def respond(
    query: str,
    *,
    situation: str | None = None,
    coordinator: Coordinator | None = None,
    executor: Executor | None = None,
    tracer: Tracer | None = None,
) -> CoordinatorResponse:
    """Plan and execute one request end to end - the Day 10 demo entry point.

    One `Usage` accumulator covers the planning call and every agent call, so ``cost`` on
    the response is the whole request's real token spend. One trace covers them too:
    `respond` owns the request trace, wraps the planning call in its own span, and hands
    the open trace to the executor for the agent spans. ``tracer`` defaults to
    `NullTracer`; live entry points pass `default_tracer()`.
    """
    coordinator = coordinator or Coordinator()
    executor = executor or Executor()
    tracer = tracer if tracer is not None else NullTracer()
    usage = Usage()
    request_id = Coordinator.new_request_id()
    received_at = utcnow()

    trace = tracer.start_request("coordinator_request", request_id=request_id, query=query)
    plan_span = trace.start_span(
        "plan",
        input_text=query,
        metadata={"has_situation": bool(situation and situation.strip())},
    )
    plan_usage = Usage()
    try:
        plan = coordinator.plan(query, situation=situation, usage=plan_usage)
    except Exception as exc:
        # A rejected plan still cost real tokens - the coordinator added them to
        # plan_usage before raising, so the error span carries the true spend.
        error = f"{type(exc).__name__}: {exc}"
        plan_span.end(
            output=None,
            status="error",
            input_tokens=plan_usage.input_tokens,
            output_tokens=plan_usage.output_tokens,
            error=error,
        )
        trace.end(
            output=None,
            status="error",
            input_tokens=plan_usage.input_tokens,
            output_tokens=plan_usage.output_tokens,
            error=error,
        )
        raise
    plan_span.end(
        output=_describe_plan(plan),
        status="ok",
        input_tokens=plan_usage.input_tokens,
        output_tokens=plan_usage.output_tokens,
    )
    usage.add(plan_usage)

    response = executor.execute(
        plan, query, request_id=request_id, received_at=received_at, usage=usage, trace=trace
    )
    trace.end(
        output=response.answer.value if response.answer.value is not None else response.synthesis,
        status=response.status.value,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
    return response


def _describe_plan(plan: SelectionPlan) -> str:
    selected = ", ".join(inv.agent.value for inv in plan.selected_agents) or "none"
    skipped = ", ".join(s.agent.value for s in plan.skipped_agents) or "none"
    return f"selected: {selected}; skipped: {skipped}"


# ------------------------------------------------------------------------ execution order


def _sequential_order(plan: SelectionPlan) -> list[AgentInvocation]:
    """The sequential chain in dependency order (the parallel group has already run).

    Kahn's algorithm; the plan validator already rejected cycles and dangling ids, so
    this always drains. Ties keep plan order for determinism.
    """
    ordered: list[AgentInvocation] = []
    attempted = {inv.invocation_id for inv in plan.parallel_group}
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
