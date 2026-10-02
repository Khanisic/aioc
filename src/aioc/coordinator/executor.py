"""Coordinator: plan execution and response assembly (Day 7; parallel + traced on Day 9;
handoff on Day 13; the refinement loop and model-written synthesis on Day 14).

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
All four agents are registered as of Day 12 (Incident Day 4, Docs Day 8, GitHub Day 11,
Deployment Day 12), so with the default runners this path no longer fires - but an
executor built with a partial runner set (tests, a deployment that ships fewer agents) can
still be handed a plan that selects an agent it lacks. The contract-honest answer for an
agent that is not there is a `Gap` with ``resolvable: false`` plus a `status` of
``partial`` or weaker - a plausible placeholder `AgentResponse` is precisely the failure
mode the null-vs-`[]` rule exists to prevent. ``resolvable: false`` is load-bearing: the
refinement loop must not spend rounds re-delegating to an agent that is not there.

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

Cost is measured, never estimated: the planning call, every agent call, and the synthesis
call land in one total, and `CoordinatorResponse.cost` is read off it.

**Tracing is opt-in at the entry point (Day 9).** The default is `NullTracer`, and the
live entry points pass `default_tracer()` explicitly - the offline suite must stay
network-free even on a machine whose `.env` carries real Langfuse keys. One trace per
request; one span per agent invocation, opened and closed in the worker thread that runs
it, so span timing is the real wall clock and a Langfuse trace of a parallel plan visibly
overlaps - which is the Day 9 checkpoint artifact.

**The sequential chain is a handoff, not just an ordering (Day 13).** A dependent's
context is composed here, at the moment its dependencies have returned: the planner's
block for that invocation, then a structured digest of each dependency's response
(`aioc.coordinator.handoff`). The composed block is what the runner receives and what the
response records in that invocation's ``context_passed``, so the explicit-passing test
still holds argument for argument - the block is longer than the planner wrote, and every
extra character is visible in the response. Nothing is inherited: GitHub's report reaches
Deployment only because the executor wrote it into Deployment's context, and the response
shows exactly what was written.

**The refinement loop consumes gaps as data, not prose (Day 14).** After the plan has run,
every open `Gap` that is ``resolvable`` and names a ``suggested_agent`` (which the contract
guarantees carries a ``suggested_query``) is a candidate for re-delegation. The loop groups
them by agent, and for each agent builds a new `AgentInvocation` with ``round: 1+``: the
query is the gaps' ``suggested_query`` verbatim, and the context is the planner's block for
that agent (when it was in the plan), then a refinement block naming the gaps, then the
digest of every response that raised one - the Day 13 handoff composition again, so a
re-delegated agent sees what the earlier round found and what it could not establish, and
inherits nothing. Refinement invocations of different agents run in parallel; the loop
stops when no candidate is left, when the round cap is reached, or when a candidate has
already been asked (the same agent with the same query is not asked twice - a gap that
comes back identical is not progress). A gap whose re-delegation produced a response is
closed; the new response's own gaps take its place. ``resolvable: false`` is honoured
without exception, and an agent that is not registered is never retried. Every round is
another full agent run, so the cap defaults low and the executor never guesses at cost.

**Every planned agent is told who else is on the request (Day 16).** Each agent receives
the whole user query, so on a multi-part question each one saw parts that were another
agent's and reported them as resolvable gaps pointing at that sibling - and the loop, doing
exactly what it is for, spent a full round of four agents re-asking for work the plan had
already done (the Day 15 live run: about half the request's cost, and a correct answer
ending `partial`). Which agents are on the request, and why each was selected, is plumbing
the coordinator already holds, so the executor states it: `handoff.roster_block` follows
the planner's block in every context composed for a planned agent, and is recorded
verbatim with it. The alternative - the loop dropping a gap whose suggested agent already
answered - would make the loop judge a gap's worth, which it was deliberately built not to
do; the fix belongs where the gap is raised.

**Synthesis is a seam with a deterministic fallback (Day 14).** `aioc.coordinator.synthesis`
holds both forms. The executor defaults to the deterministic one (adopt the
highest-confidence report and cite its own evidence ids, so the coordinator never mints an
id), and a live entry point may pass `ModelSynthesiser()` to get a model-written answer
over the agents' digests - grounded in code: an evidence id no agent carries rejects the
synthesis, and the executor falls back to the deterministic form and says so in
``answer.reasoning`` rather than throw away a request every agent has already been paid
for. Opt-in at the entry point for the same reason tracing is: the offline suite must not
make a network call because a key happens to be in `.env`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import uuid4

from aioc.agents import DeploymentAgent, DocsAgent, GitHubAgent, IncidentAgent
from aioc.contracts import (
    AgentInvocation,
    AgentName,
    AgentResponse,
    Assessment,
    CoordinatorResponse,
    Cost,
    Gap,
    GapKind,
    InvocationMode,
    ResponseStatus,
)
from aioc.hitl import HitlGate
from aioc.llm import Usage
from aioc.observability.tracing import NullTracer, RequestTrace, Tracer

from .handoff import (
    compose_dependent_context,
    refinement_block,
    refinement_query,
    roster_block,
    with_roster,
)
from .planner import Coordinator, SelectionPlan, utcnow
from .synthesis import (
    Synthesis,
    Synthesiser,
    SynthesisRequest,
    deterministic,
    render_prompt,
)

DEFAULT_MAX_REFINEMENT_ROUNDS = 2


class AgentRunner(Protocol):
    """One runnable subagent, as the executor sees it.

    The protocol is the executor's seam: tests inject recording fakes, the agent days register
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


class DeploymentRunner:
    """Adapts `DeploymentAgent.assess` to the `AgentRunner` protocol - the same straight
    pass-through as the other three. The Day 13 sequential path (GitHub reads the PR, then
    Deployment diffs the release) arrives here as ``context_passed`` composed by the
    executor from the planner's block and GitHub's digest, not as anything this runner
    adds."""

    def __init__(self, agent: DeploymentAgent | None = None) -> None:
        self._agent = agent or DeploymentAgent()

    def run(
        self,
        query: str,
        *,
        context: str,
        request_id: str,
        invocation_id: str,
        usage: Usage,
    ) -> AgentResponse:
        return self._agent.assess(
            query,
            context=context,
            request_id=request_id,
            invocation_id=invocation_id,
            usage=usage,
        )


def default_runners() -> dict[AgentName, AgentRunner]:
    """Every agent: all four exist as of Day 12."""
    return {
        AgentName.INCIDENT: IncidentRunner(),
        AgentName.DOCS: DocsRunner(),
        AgentName.GITHUB: GitHubRunner(),
        AgentName.DEPLOYMENT: DeploymentRunner(),
    }


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


@dataclass(slots=True)
class _Outcome:
    """What one attempted invocation came back with - exactly one of response/error is
    set, and ``usage`` counts the tokens it spent either way (a failed call still cost)."""

    invocation: AgentInvocation
    query: str
    response: AgentResponse | None
    error: Exception | None
    usage: Usage


@dataclass(slots=True)
class _OpenGap:
    """A gap the run has seen, with where it came from and whether a refinement round has
    consumed it. ``source`` is the invocation that raised it (an agent's response gap, or
    the executor's own gap about that invocation); the planner's gaps have none."""

    gap: Gap
    source: str | None
    consumed: bool = False


@dataclass(slots=True)
class _Run:
    """The mutable state of one `execute` call, threaded through the rounds. Every merge
    into it happens on the calling thread after a worker has returned."""

    query: str
    request_id: str
    trace: RequestTrace
    usage: Usage
    # Invocations as actually executed, in execution order: the plan's, then each
    # refinement round's. A sequential or refinement one carries its composed context.
    executed: dict[str, AgentInvocation] = field(default_factory=dict)
    by_invocation: dict[str, AgentResponse] = field(default_factory=dict)
    gaps: list[_OpenGap] = field(default_factory=list)
    execution_gaps: list[Gap] = field(default_factory=list)
    any_failure: bool = False
    # (agent, query) pairs already delegated by the refinement loop - asked once only.
    delegated: set[tuple[AgentName, str]] = field(default_factory=set)

    @property
    def reports(self) -> list[tuple[AgentInvocation, AgentResponse]]:
        return [
            (inv, self.by_invocation[inv_id])
            for inv_id, inv in self.executed.items()
            if inv_id in self.by_invocation
        ]

    @property
    def responses(self) -> list[AgentResponse]:
        return [response for _, response in self.reports]

    @property
    def unresolved(self) -> list[Gap]:
        return [g.gap for g in self.gaps if not g.consumed]

    def note_execution_gap(self, gap: Gap, source: str | None) -> None:
        self.execution_gaps.append(gap)
        self.gaps.append(_OpenGap(gap, source))

    def settle(self, outcome: _Outcome) -> None:
        """Fold one finished invocation in. Single-threaded by construction: called only
        after the worker has returned, in plan order."""
        self.usage.add(outcome.usage)
        inv = outcome.invocation
        self.executed[inv.invocation_id] = inv
        if outcome.response is not None:
            self.by_invocation[inv.invocation_id] = outcome.response
            self.gaps.extend(_OpenGap(g, inv.invocation_id) for g in outcome.response.gaps)
        elif outcome.error is not None:
            self.any_failure = True
            self.note_execution_gap(
                _invocation_failed_gap(inv, outcome.query, outcome.error), inv.invocation_id
            )


class Executor:
    """Runs a `SelectionPlan` and assembles the `CoordinatorResponse`."""

    def __init__(
        self,
        runners: dict[AgentName, AgentRunner] | None = None,
        *,
        tracer: Tracer | None = None,
        synthesiser: Synthesiser | None = None,
        max_refinement_rounds: int = DEFAULT_MAX_REFINEMENT_ROUNDS,
    ) -> None:
        self._runners = dict(runners) if runners is not None else dict(default_runners())
        # NullTracer by default, never default_tracer(): tracing activates only when an
        # entry point passes a tracer, so the offline suite cannot emit spans by accident.
        self._tracer: Tracer = tracer if tracer is not None else NullTracer()
        # Deterministic synthesis by default, for the same reason: a model-written one is
        # a network call, and only an entry point may opt into it.
        self._synthesiser = synthesiser
        if max_refinement_rounds < 0:
            raise ValueError("max_refinement_rounds must be >= 0")
        self._max_rounds = max_refinement_rounds

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
        the sequential chain in dependency order; then re-delegate open gaps for up to
        ``max_refinement_rounds`` rounds; then synthesise.

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

        run = _Run(query=query, request_id=request_id, trace=trace, usage=usage)
        # The plan's invocations, in plan order, before anything runs. Each is replaced by
        # its composed form when it runs - the roster for every planned agent with siblings,
        # plus the handoff for a sequential one (CONTRACTS.md sec 5: context_passed is the
        # literal block embedded in the subagent's prompt - not the block as first planned).
        # One that never runs (no runner, an unmet dependency) keeps the planner's block.
        for inv in plan.selected_agents:
            run.executed[inv.invocation_id] = inv
        run.gaps.extend(_OpenGap(g, None) for g in plan.gaps)

        self._run_plan(plan, run)
        rounds = self._refine(plan, run)
        answer, synthesis = self._synthesise(plan, run, rounds)
        unresolved = run.unresolved
        status = _status(run, unresolved, answer)

        response = CoordinatorResponse(
            request_id=request_id,
            query=query,
            received_at=received,
            intent=plan.intent,
            # Execution order: the plan's invocations as executed, then each refinement
            # round's. A composed context is recorded verbatim.
            selected_agents=list(run.executed.values()),
            skipped_agents=plan.skipped_agents,
            agent_responses=run.responses,  # type: ignore[arg-type]
            synthesis=synthesis,
            answer=answer,
            refinement_rounds=rounds,
            unresolved_gaps=unresolved,
            status=status,
            cost=Cost(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                # Null until a response reports them: not measured is not zero.
                cache_read_tokens=usage.cache_read_tokens,
                cache_write_tokens=usage.cache_write_tokens,
            ),
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

    # ------------------------------------------------------------------- round zero

    def _run_plan(self, plan: SelectionPlan, run: _Run) -> None:
        # -- the parallel group, concurrently. The plan validator guarantees every member
        # has depends_on == [], so there is nothing to wait on and no unmet-dependency case.
        group: list[tuple[AgentInvocation, AgentRunner, str]] = []
        for inv in plan.parallel_group:
            runner = self._runners.get(inv.agent)
            if runner is None:
                run.note_execution_gap(_agent_missing_gap(inv), inv.invocation_id)
            else:
                composed = inv.model_copy(update={"context_passed": _planned_context(inv, plan)})
                group.append((composed, runner, run.query))
        for outcome in self._run_group(group, run):
            run.settle(outcome)

        # -- the sequential chain, one at a time in dependency order.
        #
        # Why this chain cannot be parallel: a dependent's context is composed FROM ITS
        # DEPENDENCY'S RESPONSE. Deployment learns which PR shipped, its head SHA, and the
        # paths it touched from GitHub's report, and diffs the release those facts
        # identify; until GitHub has returned, that context does not exist and there is
        # nothing to hand over. Starting Deployment early would mean starting it with the
        # planner's block alone and letting it guess or assume the rest - exactly the
        # inherited/assumed context this project exists to demonstrate the absence of.
        # It is a data dependency, not a scheduling preference: the parallel group above
        # runs together because nothing any member needs is produced by another member.
        for inv in _sequential_order(plan):
            runner = self._runners.get(inv.agent)
            if runner is None:
                run.note_execution_gap(_agent_missing_gap(inv), inv.invocation_id)
                continue
            unmet = [dep for dep in inv.depends_on if dep not in run.by_invocation]
            if unmet:
                run.note_execution_gap(_dependency_unmet_gap(inv, unmet), inv.invocation_id)
                continue
            handed_off = compose_dependent_context(
                _planned_context(inv, plan),
                [(run.executed[dep], run.by_invocation[dep]) for dep in inv.depends_on],
            )
            composed = inv.model_copy(update={"context_passed": handed_off})
            run.settle(self._run_invocation(composed, runner, run.query, run))

    # ------------------------------------------------------------- refinement rounds

    def _refine(self, plan: SelectionPlan, run: _Run) -> int:
        """Re-delegate open gaps, round by round, until nothing is left to ask or the cap
        is reached. Returns the number of rounds that actually ran."""
        rounds = 0
        for round_number in range(1, self._max_rounds + 1):
            targets = self._targets(run)
            if not targets:
                break
            rounds = round_number
            group: list[tuple[AgentInvocation, AgentRunner, str]] = []
            asked: dict[str, list[_OpenGap]] = {}
            for agent, open_gaps in targets.items():
                inv, query = self._refinement_invocation(agent, open_gaps, plan, run, round_number)
                run.executed[inv.invocation_id] = inv
                asked[inv.invocation_id] = open_gaps
                group.append((inv, self._runners[agent], query))
            for outcome in self._run_group(group, run):
                run.settle(outcome)
                if outcome.response is not None:
                    # The re-delegation answered: the gaps it was asked to close are
                    # consumed, and the new response's own gaps (already registered by
                    # settle) take their place. A failed re-delegation leaves them open,
                    # next to the failure gap.
                    for open_gap in asked[outcome.invocation.invocation_id]:
                        open_gap.consumed = True
        return rounds

    def _targets(self, run: _Run) -> dict[AgentName, list[_OpenGap]]:
        """The open gaps the loop may act on, grouped by the agent they name."""
        targets: dict[AgentName, list[_OpenGap]] = {}
        for open_gap in run.gaps:
            gap = open_gap.gap
            if open_gap.consumed or not gap.resolvable or gap.suggested_agent is None:
                continue
            if gap.suggested_agent not in self._runners:
                continue  # not registered: another round cannot fix an absence
            key = (gap.suggested_agent, gap.suggested_query or "")
            if key in run.delegated:
                continue  # asked already; an identical gap coming back is not progress
            targets.setdefault(gap.suggested_agent, []).append(open_gap)
        return targets

    def _refinement_invocation(
        self,
        agent: AgentName,
        open_gaps: list[_OpenGap],
        plan: SelectionPlan,
        run: _Run,
        round_number: int,
    ) -> tuple[AgentInvocation, str]:
        """A new invocation for ``agent`` that closes ``open_gaps``: the gaps' suggested
        queries verbatim as its query; the planner's block for the agent (if any), the
        refinement block, and the digest of each response that raised a gap as its
        context - composed here, recorded verbatim."""
        gaps = [og.gap for og in open_gaps]
        for gap in gaps:
            run.delegated.add((agent, gap.suggested_query or ""))
        # The responses that raised these gaps are what the re-delegation depends on.
        sources: list[str] = []
        for og in open_gaps:
            if og.source in run.by_invocation and og.source not in sources:
                sources.append(og.source)
        raised_by = {
            og.gap.id: f"{run.executed[og.source].agent.value}, invocation {og.source}"
            for og in open_gaps
            if og.source is not None and og.source in run.by_invocation
        }
        planned = next((inv for inv in plan.selected_agents if inv.agent is agent), None)
        planner_block = _planned_context(planned, plan) if planned is not None else None
        # The planner's block, the raising responses' digests, then the refinement block
        # (Day 22): the gaps this invocation must close are the most specific thing it is
        # told, so they sit last, next to the query that asks for them, rather than under
        # several thousand characters of digest.
        context = compose_dependent_context(
            planner_block or "", [(run.executed[src], run.by_invocation[src]) for src in sources]
        ).strip()
        tail = refinement_block(round_number, gaps, raised_by)
        context = f"{context}\n\n{tail}" if context else tail
        ids = ", ".join(g.id for g in gaps)
        invocation = AgentInvocation(
            invocation_id=_new_id("inv"),
            agent=agent,
            reason=(
                f"Refinement round {round_number}: re-delegated to close {len(gaps)} gap(s) "
                f"({ids}) that an earlier round left resolvable and pointed at this agent."
            ),
            mode=InvocationMode.SEQUENTIAL if sources else InvocationMode.PARALLEL,
            depends_on=sources,
            context_passed=context,
            round=round_number,
        )
        return invocation, refinement_query(gaps)

    # -------------------------------------------------------------------- synthesis

    def _synthesise(
        self, plan: SelectionPlan, run: _Run, rounds: int
    ) -> tuple[Assessment[str], str]:
        request = SynthesisRequest(
            query=run.query,
            intent=plan.intent,
            reports=run.reports,
            execution_gaps=list(run.execution_gaps),
            unresolved_gaps=run.unresolved,
            refinement_rounds=rounds,
        )
        if self._synthesiser is None:
            result = deterministic(request)
            return result.answer, result.synthesis

        local = Usage()
        span = run.trace.start_span(
            "synthesis",
            input_text=render_prompt(request),
            metadata={"responses": len(request.reports), "refinement_rounds": rounds},
        )
        try:
            result = self._synthesiser.synthesise(request, usage=local)
        except Exception as exc:  # noqa: BLE001 - the fallback is the point
            fallback = deterministic(request)
            note = (
                " Model-written synthesis was rejected and the deterministic form is used "
                f"instead: {type(exc).__name__}: {_error_summary(exc, 300)}"
            )
            result = Synthesis(
                synthesis=fallback.synthesis,
                answer=fallback.answer.model_copy(
                    update={"reasoning": (fallback.answer.reasoning or "").rstrip() + note}
                ),
            )
            span.end(
                output=None,
                status="error",
                input_tokens=local.input_tokens,
                output_tokens=local.output_tokens,
                error=f"{type(exc).__name__}: {exc}",
            )
        else:
            span.end(
                output=result.answer.value if result.answer.value is not None else result.synthesis,
                status="ok",
                input_tokens=local.input_tokens,
                output_tokens=local.output_tokens,
            )
        run.usage.add(local)
        return result.answer, result.synthesis

    # ------------------------------------------------------------------- running

    def _run_group(
        self,
        group: list[tuple[AgentInvocation, AgentRunner, str]],
        run: _Run,
    ) -> list[_Outcome]:
        """Run independent invocations concurrently; outcomes come back in group order."""
        if not group:
            return []
        if len(group) == 1:
            inv, runner, query = group[0]
            return [self._run_invocation(inv, runner, query, run)]
        with ThreadPoolExecutor(max_workers=len(group), thread_name_prefix="aioc-agent") as pool:
            futures = [
                pool.submit(self._run_invocation, inv, runner, query, run)
                for inv, runner, query in group
            ]
            # future.result() never raises here: _run_invocation catches the runner's
            # exception and returns it as data, so one failure cannot hide the others.
            return [future.result() for future in futures]

    def _run_invocation(
        self,
        inv: AgentInvocation,
        runner: AgentRunner,
        query: str,
        run: _Run,
    ) -> _Outcome:
        """Run one invocation to an `_Outcome` - possibly on a worker thread, so it never
        raises and never touches shared state; `settle` folds the result in afterwards.

        The span opens and closes here, in the thread doing the work, so its timing is the
        agent's real wall clock - concurrent agents show as overlapping spans. ``query`` is
        the user's query for a planned invocation and the gaps' suggested query for a
        refinement one.
        """
        local = Usage()
        span = run.trace.start_span(
            f"agent:{inv.agent.value}",
            input_text=inv.context_passed,
            metadata={
                "invocation_id": inv.invocation_id,
                "mode": inv.mode.value,
                "round": inv.round,
                "query": query,
            },
        )
        try:
            response = runner.run(
                query,
                context=inv.context_passed,
                request_id=run.request_id,
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
            return _Outcome(invocation=inv, query=query, response=None, error=exc, usage=local)
        for ref in response.tool_calls:
            span.record_tool_call(ref)
        span.end(
            output=response.summary,
            status=response.status.value,
            input_tokens=local.input_tokens,
            output_tokens=local.output_tokens,
        )
        return _Outcome(invocation=inv, query=query, response=response, error=None, usage=local)


def respond(
    query: str,
    *,
    situation: str | None = None,
    coordinator: Coordinator | None = None,
    executor: Executor | None = None,
    tracer: Tracer | None = None,
    gate: HitlGate | None = None,
) -> CoordinatorResponse:
    """Plan and execute one request end to end - the Day 10 demo entry point.

    One `Usage` accumulator covers the planning call and every agent call, so ``cost`` on
    the response is the whole request's real token spend. One trace covers them too:
    `respond` owns the request trace, wraps the planning call in its own span, and hands
    the open trace to the executor for the agent spans. ``tracer`` defaults to
    `NullTracer`; live entry points pass `default_tracer()`. Model-written synthesis and
    the refinement cap are the executor's: pass ``executor=Executor(synthesiser=...)``.

    ``gate`` (Day 17) is the HITL approval gate, opt-in at the entry point like tracing:
    when given, every recommendation in the finished response is decided and written to
    the gate's audit log before the response is returned, under a ``hitl_gate`` span. The
    decisions are read back from ``gate.audit`` by ``request_id``; the response itself is
    the frozen contract and carries none of them.
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
    if gate is not None:
        _review(gate, response, trace)
    trace.end(
        output=response.answer.value if response.answer.value is not None else response.synthesis,
        status=response.status.value,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
    )
    return response


def _review(gate: HitlGate, response: CoordinatorResponse, trace: RequestTrace) -> None:
    """Every recommendation through the gate, recorded on the trace. No tokens: the gate
    is code and a human, never a model call."""
    span = trace.start_span(
        "hitl_gate",
        input_text=response.synthesis,
        metadata={"request_id": response.request_id, "approver": type(gate.approver).__name__},
    )
    try:
        result = gate.review(response)
    except Exception as exc:
        span.end(
            output=None,
            status="error",
            input_tokens=0,
            output_tokens=0,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    span.end(
        output=(
            f"{len(result.decisions)} recommendation(s): {len(result.released)} released, "
            f"{len(result.withheld)} withheld"
        ),
        status="ok",
        input_tokens=0,
        output_tokens=0,
    )


def _planned_context(inv: AgentInvocation, plan: SelectionPlan) -> str:
    """The context every run of a planned agent starts from: the planner's block, then the
    roster of its siblings on this request (`handoff.roster_block`). Deterministic in the
    plan, so round 0 and every refinement round open with the same text."""
    return with_roster(inv.context_passed, roster_block(inv.agent, plan.selected_agents))


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
    # an agent that does not exist. resolvable=False stops the refinement loop from
    # retrying an absence that another round cannot fix.
    return Gap(
        id=_new_id("gap"),
        description=(
            f"The plan selected the {inv.agent.value} agent "
            f"(invocation {inv.invocation_id}), but no runner for that agent is registered "
            "with this executor. The invocation was not executed and no response was "
            "fabricated for it."
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


def _error_summary(exc: Exception, limit: int = 600) -> str:
    """The exception's text, whitespace-collapsed and capped. The whole message, not its
    first line: a pydantic `ValidationError` puts the one fact that matters (which field,
    which rule) on the second line, and the first live sequential run lost it to a
    first-line-only cut - the response said "1 validation error" and nothing else.

    Notes on the exception (PEP 678) follow the message and survive the cap: the Day 17
    validation-retry loop attaches one saying how many attempts it made and why it
    stopped, and that is the fact a reader of the gap needs next."""
    notes = " | ".join(" ".join(str(n).split()) for n in getattr(exc, "__notes__", []) or [])
    text = " ".join(str(exc).split())
    room = limit - (len(notes) + 3 if notes else 0)
    if len(text) > room:
        text = text[: max(room - 3, 0)].rstrip() + "..."
    return f"{text} | {notes}" if notes else text


def _invocation_failed_gap(inv: AgentInvocation, query: str, exc: Exception) -> Gap:
    # A failed invocation is genuinely worth one more attempt - model nondeterminism and
    # transient upstreams both clear on retry - so this gap is machine-consumable by the
    # refinement loop: resolvable, with the agent and query to re-delegate spelled out.
    return Gap(
        id=_new_id("gap"),
        description=(
            f"Invocation {inv.invocation_id} ({inv.agent.value}) failed with "
            f"{type(exc).__name__}: {_error_summary(exc)}"
        ),
        kind=GapKind.OTHER,
        kind_detail="agent_invocation_failed",
        blocks_field=None,
        suggested_agent=inv.agent,
        suggested_query=query,
        resolvable=True,
    )


# ------------------------------------------------------------------------------- status


def _status(run: _Run, unresolved: list[Gap], answer: Assessment[str]) -> ResponseStatus:
    """Honest roll-up: `complete` only when nothing is open and each agent's latest report
    is itself complete. An invocation that did not run is open through its execution gap
    (agent missing, dependency unmet, or failed), so "everything ran" is already part of
    "nothing is open" - unless a refinement round retried the failure and the retry
    answered, which consumes that gap. A refinement round can likewise close the gap that
    made an earlier report `partial`; the earlier report stays in the response, but the
    run is judged on where each agent ended up. And a run with no answer is not complete,
    however clean its agents were (a model synthesis may honestly return a null answer)."""
    responses = run.responses
    if not responses:
        return ResponseStatus.ERROR if run.any_failure else ResponseStatus.INSUFFICIENT_EVIDENCE
    latest: dict[AgentName, AgentResponse] = {}
    for response in responses:
        latest[response.agent] = response
    if (
        answer.value is not None
        and not unresolved
        and all(r.status is ResponseStatus.COMPLETE for r in latest.values())
    ):
        return ResponseStatus.COMPLETE
    return ResponseStatus.PARTIAL
