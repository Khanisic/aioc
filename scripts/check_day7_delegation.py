"""Day 7 done-when, live: does delegation actually pass context the agent never inherited?

    uv run python scripts/check_day7_delegation.py           # 2 live calls (plan + diagnose)
    uv run python scripts/check_day7_delegation.py --query "..."

Two Claude API calls: one for the coordinator's plan, one for the Incident agent's diagnosis.
The offline suite proves the executor's plumbing with fakes; this proves the same facts against
the real API, where the plan is model-written rather than a fixture.

**What this checks that the offline tests cannot.** `tests/test_executor.py` asserts the runner
receives exactly `context_passed`, but it hands the executor a plan a human wrote. Here the plan
comes from the model, so the context block is whatever the coordinator actually chose to write -
and the check is that *that* text, verbatim, is what reaches the agent's wire prompt. The
assertion is made at the HTTP boundary rather than at the runner, because the runner is our own
code and the prompt is what the model truly sees.

**The situation block is the trap being tested.** `respond(query, situation=...)` shows the
coordinator live operational facts. Those facts must not reach the agent automatically - only the
subset the coordinator deliberately writes into `context_passed`. A sentinel string is planted in
the situation and marked as coordinator-only; if it turns up in the agent's prompt without the
coordinator having put it in the plan, context was inherited and the check fails loudly.

The situation is a fixed fixture rather than a live Prometheus read, so this script needs no
Docker stack and measures one thing at a time. `check_day5_checkpoint.py` is the one that reads
live metrics.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.agents import IncidentAgent  # noqa: E402
from aioc.contracts import AgentName, AgentResponse, ResponseStatus  # noqa: E402
from aioc.coordinator import Coordinator, Executor, IncidentRunner  # noqa: E402
from aioc.coordinator.executor import respond  # noqa: E402
from aioc.llm import LLMClient, LLMSettings, Usage  # noqa: E402

DEFAULT_QUERY = (
    "Checkout is returning 502s to customers. What is actually broken, and how bad is it?"
)

# The sentinel is a fact the coordinator can see and the agent must not receive unless the
# coordinator deliberately passes it. It is deliberately irrelevant to the diagnosis, so a
# coordinator with any judgement leaves it out - which is what makes its appearance meaningful.
SENTINEL = "INTERNAL-ONLY-ROTA-TOKEN-7F3A"

SITUATION = (
    "checkout-api 5xx ratio 4.10%, p50 5ms, p99 975ms. payments-api 5xx ratio 0.00%, p50 4ms, "
    "p99 974ms, RSS 78MB. inventory-api nominal: 5xx 0.00%, p99 12ms. All three services are up. "
    "No deploys in the last 24 hours. checkout-api fans out to both downstreams and returns 502 "
    "if either fails, so a checkout-api 502 rate implicates a downstream rather than checkout "
    f"itself. On-call handover reference {SENTINEL} (coordinator bookkeeping only - not an "
    "operational signal and of no use to any subagent)."
)


class _PromptCapturingClient(LLMClient):
    """An `LLMClient` that records every outgoing request before sending it.

    Subclassed rather than mocked on purpose: the point of this script is that the real request
    goes to the real API. This only observes what `complete` was handed, which is the closest
    we can stand to the wire without intercepting HTTP.
    """

    def __init__(self, settings: LLMSettings | None = None) -> None:
        super().__init__(settings)
        self.prompts: list[str] = []

    def complete(self, **kwargs: Any) -> Any:
        messages = kwargs.get("messages") or []
        if messages:
            content = messages[0].get("content")
            if isinstance(content, str):
                self.prompts.append(content)
        return super().complete(**kwargs)


class _RecordingIncidentRunner:
    """Wraps the real `IncidentRunner`, recording exactly what the executor handed over."""

    def __init__(self) -> None:
        self.client = _PromptCapturingClient()
        self._inner = IncidentRunner(IncidentAgent(self.client))
        self.handed: dict[str, Any] | None = None

    def run(
        self,
        query: str,
        *,
        context: str,
        request_id: str,
        invocation_id: str,
        usage: Usage,
    ) -> AgentResponse:
        self.handed = {
            "query": query,
            "context": context,
            "request_id": request_id,
            "invocation_id": invocation_id,
        }
        return self._inner.run(
            query,
            context=context,
            request_id=request_id,
            invocation_id=invocation_id,
            usage=usage,
        )


def _evaluate(response: Any, runner: _RecordingIncidentRunner, query: str) -> list[str]:
    """Return the list of complaints. Empty means the done-when held live."""
    complaints: list[str] = []

    incident_invocations = [
        inv for inv in response.selected_agents if inv.agent is AgentName.INCIDENT
    ]
    if not incident_invocations:
        # Not a delegation bug, but this run measured nothing - say so rather than passing.
        complaints.append(
            "the coordinator did not select the incident agent, so delegation was never "
            "exercised; re-run with a query that clearly needs live diagnosis"
        )
        return complaints
    invocation = incident_invocations[0]

    if runner.handed is None:
        complaints.append("the incident runner was never invoked despite being selected")
        return complaints

    # 1. The executor handed over exactly the planned context - nothing added, nothing dropped.
    if runner.handed["context"] != invocation.context_passed:
        complaints.append(
            "the context handed to the agent is not identical to the plan's context_passed"
        )

    # 2. The agent's real wire prompt carries that block and the query, and nothing else.
    if not runner.client.prompts:
        complaints.append("no outgoing prompt was captured")
    else:
        prompt = runner.client.prompts[-1]
        expected = (
            f"<context>\n{invocation.context_passed.strip()}\n</context>\n\n"
            f"Operational query: {query.strip()}"
        )
        if prompt != expected:
            complaints.append("the agent's wire prompt is not exactly context + query")

        # 3. The sentinel test: coordinator-visible facts must not reach the agent by inheritance.
        if SENTINEL in prompt and SENTINEL not in invocation.context_passed:
            complaints.append(
                f"the sentinel {SENTINEL} reached the agent without being in context_passed - "
                "context was inherited, which is the exact failure this project denies"
            )

    # 4. Dynamic selection still discriminating.
    if not response.skipped_agents:
        complaints.append("skipped_agents is empty - dynamic selection is not discriminating")
    if len(response.selected_agents) + len(response.skipped_agents) != 4:
        complaints.append("not all four agents accounted for")

    # 5. Cost is measured across both calls, not estimated or left at zero.
    if response.cost.input_tokens <= 0 or response.cost.output_tokens <= 0:
        complaints.append("cost is zero - the Usage accumulator did not thread through")

    # 6. Nothing was fabricated for an agent that does not exist.
    responded = {r.agent for r in response.agent_responses}
    for inv in response.selected_agents:
        if inv.agent is AgentName.INCIDENT:
            continue
        if inv.agent in responded:
            complaints.append(f"a response exists for {inv.agent.value}, which is not implemented")
        elif not any(g.kind_detail == "agent_not_implemented" for g in response.unresolved_gaps):
            complaints.append(
                f"{inv.agent.value} was selected but produced neither response nor gap"
            )

    # 7. The answer cites only its subagents' evidence (validated on construction; restated here
    #    because a silently-empty citation list would still validate at low confidence).
    union = {e.id for r in response.agent_responses for e in r.evidence}
    if response.answer.evidence and not set(response.answer.evidence) <= union:
        complaints.append("the answer cites evidence no subagent reported")

    # 8. Honest status: complete is only legal when nothing is outstanding.
    if response.status is ResponseStatus.COMPLETE and response.unresolved_gaps:
        complaints.append("status is complete despite unresolved gaps")

    return complaints


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--query", default=DEFAULT_QUERY, help="the operational query to run")
    args = parser.parse_args(argv)

    if LLMSettings().anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2

    print("Day 7 delegation check - 2 live API calls (1 plan + 1 diagnose)\n")
    runner = _RecordingIncidentRunner()
    start = time.monotonic()

    with RunRecorder(
        kind="llm",
        name="day7-delegation",
        command="check_day7_delegation.py",
        metadata={"query": args.query, "sentinel": SENTINEL},
    ) as run:
        try:
            response = respond(
                args.query,
                situation=SITUATION,
                coordinator=Coordinator(),
                executor=Executor({AgentName.INCIDENT: runner}),
            )
        except Exception as exc:
            run.event(
                "delegation",
                outcome="failed",
                duration_ms=(time.monotonic() - start) * 1000,
                type="llm_call",
                message=f"{type(exc).__name__}: {exc}",
            )
            print(f"  FAIL  {type(exc).__name__}: {exc}")
            return 1

        duration_ms = (time.monotonic() - start) * 1000
        complaints = _evaluate(response, runner, args.query)
        passed = not complaints

        invocation = next(
            (i for i in response.selected_agents if i.agent is AgentName.INCIDENT), None
        )
        run.event(
            "delegation",
            outcome="passed" if passed else "failed",
            duration_ms=duration_ms,
            type="llm_call",
            data={
                "query": args.query,
                "intent": None if response.intent.value is None else response.intent.value.value,
                "selected": sorted(i.agent.value for i in response.selected_agents),
                "skipped": sorted(s.agent.value for s in response.skipped_agents),
                "status": response.status.value,
                "cost": response.cost.model_dump(),
                "context_words": len(invocation.context_passed.split()) if invocation else 0,
                "sentinel_in_context": bool(invocation and SENTINEL in invocation.context_passed),
                "unresolved_gaps": len(response.unresolved_gaps),
                "answer_confidence": response.answer.confidence,
                "complaints": complaints,
            },
            message="; ".join(complaints) if complaints else "delegation held end to end",
        )
        run.artifact("coordinator_response.json", response.model_dump_json(indent=2))
        if runner.client.prompts:
            run.artifact("agent_prompt.txt", runner.client.prompts[-1])

        print(f"  intent    {response.intent.value.value if response.intent.value else None}")
        print(f"  selected  {sorted(i.agent.value for i in response.selected_agents)}")
        for skip in response.skipped_agents:
            print(f"  skipped   {skip.agent.value}: {skip.reason[:80]}")
        if invocation:
            words = len(invocation.context_passed.split())
            print(f"\n  context passed to incident ({words} words), verbatim to the agent:")
            for line in invocation.context_passed.splitlines():
                print(f"    | {line}")
            print(
                f"\n  sentinel in the coordinator's view: yes; "
                f"in the agent's context: "
                f"{'YES - LEAKED' if SENTINEL in invocation.context_passed else 'no'}"
            )
        print(f"\n  status    {response.status.value}")
        print(f"  cost      {response.cost.input_tokens} in / {response.cost.output_tokens} out")
        print(f"  gaps      {len(response.unresolved_gaps)} unresolved")
        for gap in response.unresolved_gaps:
            flag = "resolvable" if gap.resolvable else "NOT resolvable"
            print(f"            [{flag}] {gap.description[:88]}")
        print(f"  answer    ({response.answer.confidence:.2f}) {response.answer.value}")

        for complaint in complaints:
            print(f"  ! {complaint}")

    print(f"\n--- delegation: {'PASS' if passed else 'FAIL'} ---")
    print(f"records: {run.dir}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
