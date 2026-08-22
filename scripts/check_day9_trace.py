"""Day 9 done-when, live: one Langfuse trace showing two agents running concurrently.

    uv run python scripts/check_day9_trace.py                 # ~3 live Claude calls
    uv run python scripts/check_day9_trace.py --fake-agents   # ZERO Claude calls

Needs `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` in the environment or `.env` - the
whole point of this check is the trace, so it refuses to run without them and never
degrades to `NullTracer` silently.

Two modes, both producing a real trace in the Langfuse UI:

- **Default (live)**: plans and executes one request end to end through `respond()`.
  Costs one planning call plus one call per selected agent - typically 3 Claude calls for
  the default query (Incident + Docs in parallel), plus a fraction-of-a-cent Voyage query
  embed when that key is set. Needs the Docker stack up (the Docs agent reads the corpus).
  This is the Day 9 checkpoint artifact: open the printed trace and the two agent spans
  visibly overlap.
- **--fake-agents**: skips the model entirely. A hand-built parallel plan runs two
  scripted agents that sleep instead of thinking, through the same executor and the same
  tracer. The trace is real and the overlap is real executor concurrency; only the agent
  *output* is scripted (and labeled as such in the trace metadata). Use it to verify the
  Langfuse wiring for free.

The script verifies concurrency locally too - each runner records its wall-clock window
and the check fails if the windows do not overlap - so a PASS does not depend on reading
the UI correctly.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.contracts import (  # noqa: E402
    AgentName,
    CoordinatorResponse,
    DocsAgentResponse,
    IncidentAgentResponse,  # noqa: E402
)
from aioc.coordinator import Coordinator, Executor, SelectionPlan  # noqa: E402
from aioc.coordinator.executor import respond  # noqa: E402
from aioc.llm import LLMSettings, Usage  # noqa: E402
from aioc.observability.tracing import LangfuseTracer, TracingSettings  # noqa: E402

DEFAULT_QUERY = (
    "checkout-api error rate just spiked - diagnose what is failing right now, and pull "
    "up how we resolved similar payments-api incidents in the past."
)

_FAKE_SLEEP_SECONDS = 1.5


# ------------------------------------------------------------------- fake-agents fixtures


def _fake_plan(query: str) -> SelectionPlan:
    incident_context = (
        "checkout-api 5xx rose from 0.1% to 2.4% in the last 15 minutes while payments-api "
        "p99 went 130ms -> 1900ms; inventory-api is nominal. Determine the failing service "
        "and the likely failure mode."
    )
    docs_context = (
        "An active latency incident implicates payments-api. Search the historical incident "
        "corpus for past payments-api latency or 5xx incidents and report how they were "
        "resolved, citing the documents."
    )
    return SelectionPlan.model_validate(
        {
            "intent": {
                "value": "incident_diagnosis",
                "confidence": 0.9,
                "evidence": [],
                "reasoning": "scripted plan for the Day 9 tracing check",
                "detail": None,
            },
            "selected_agents": [
                {
                    "invocation_id": "inv_incident",
                    "agent": "incident",
                    "reason": "a live symptom needs a diagnosis of the failing service",
                    "mode": "parallel",
                    "depends_on": [],
                    "context_passed": incident_context,
                    "round": 0,
                },
                {
                    "invocation_id": "inv_docs",
                    "agent": "docs",
                    "reason": "past resolutions for the implicated service inform the fix",
                    "mode": "parallel",
                    "depends_on": [],
                    "context_passed": docs_context,
                    "round": 0,
                },
            ],
            "skipped_agents": [
                {
                    "agent": "github",
                    "reason": "no code artefact is named and no deploy is implicated yet",
                },
                {
                    "agent": "deployment",
                    "reason": "no release comparison is needed to answer this query",
                },
            ],
            "gaps": [],
        }
    )


def _fake_incident_response(request_id: str, invocation_id: str) -> IncidentAgentResponse:
    def assessment(value: str, reasoning: str) -> dict[str, Any]:
        return {
            "value": value,
            "confidence": 0.7,
            "evidence": ["ev_1"],
            "reasoning": reasoning,
            "detail": None,
        }

    return IncidentAgentResponse.model_validate(
        {
            "agent": "incident",
            "request_id": request_id,
            "invocation_id": invocation_id,
            "status": "complete",
            "status_detail": None,
            "summary": "SCRIPTED (Day 9 tracing check): payments-api latency is degrading "
            "checkout-api.",
            "findings": {
                "incident_window": {"start": "2026-08-21T14:00:00Z", "end": None},
                "affected_services": ["checkout-api", "payments-api"],
                "severity": assessment("sev2", "customer-facing errors without a full outage"),
                "failure_mode": assessment(
                    "downstream_latency", "payments-api p99 rose 14x while checkout-api errored"
                ),
                "root_cause": assessment(
                    "payments-api latency breaching checkout-api's timeout",
                    "the 502 pattern matches downstream timeouts",
                ),
                "contributing_factors": [],
                "timeline": [],
                "impact": {
                    "error_rate_before": None,
                    "error_rate_after": None,
                    "p50_latency_ms_before": None,
                    "p50_latency_ms_after": None,
                    "p99_latency_ms_before": None,
                    "p99_latency_ms_after": None,
                    "requests_affected": None,
                    "duration_seconds": None,
                },
                "recommended_actions": [],
                "similar_incidents": [],
            },
            "evidence": [
                {
                    "id": "ev_1",
                    "source_type": "metric",
                    "source_type_detail": None,
                    "source_ref": 'http_request_duration_seconds{service="payments-api"}',
                    "excerpt": "payments-api p99 went 130ms -> 1900ms.",
                    "observed_at": "2026-08-21T14:15:00Z",
                    "uri": None,
                    "tool_call_id": None,
                }
            ],
            "gaps": [],
            "overall_confidence": 0.7,
            "generated_at": datetime.now(UTC).isoformat(),
        }
    )


def _fake_docs_response(request_id: str, invocation_id: str) -> DocsAgentResponse:
    question = "How were past payments-api latency incidents resolved?"
    quote = "Resolved by resizing the payments-api connection pool and adding a timeout."
    return DocsAgentResponse.model_validate(
        {
            "agent": "docs",
            "request_id": request_id,
            "invocation_id": invocation_id,
            "status": "complete",
            "status_detail": None,
            "summary": "SCRIPTED (Day 9 tracing check): past incidents were resolved by pool "
            "resizing.",
            "findings": {
                "answer": {
                    "value": "Past payments-api latency incidents were resolved by connection "
                    "pool resizing and stricter timeouts.",
                    "confidence": 0.7,
                    "evidence": ["ev_doc_1"],
                    "reasoning": "one corpus document describes the same resolution",
                    "detail": None,
                },
                "claims": [
                    {
                        "id": "claim_1",
                        "statement": "A previous payments-api latency incident was resolved by "
                        "resizing the connection pool.",
                        "supported": True,
                        "sources": [
                            {
                                "document_id": "inc_fake_0001",
                                "title": "payments-api latency (scripted)",
                                "chunk_id": None,
                                "uri": None,
                                "quote": quote,
                                "relevance": 0.82,
                            }
                        ],
                        "confidence": 0.7,
                    }
                ],
                "coverage": {
                    "sub_questions": [question],
                    "answered": [question],
                    "unanswered": [],
                    "documents_searched": 18,
                    "documents_retrieved": 3,
                    "documents_cited": 1,
                    "corpus_snapshot": None,
                },
            },
            "evidence": [
                {
                    "id": "ev_doc_1",
                    "source_type": "document",
                    "source_type_detail": None,
                    "source_ref": "inc_fake_0001",
                    "excerpt": quote,
                    "observed_at": None,
                    "uri": None,
                    "tool_call_id": None,
                }
            ],
            "gaps": [],
            "overall_confidence": 0.7,
            "generated_at": datetime.now(UTC).isoformat(),
        }
    )


class _SleepingRunner:
    """A scripted agent that records its wall-clock window - the local concurrency proof."""

    def __init__(self, make_response: Any, windows: dict[str, tuple[float, float]]) -> None:
        self._make_response = make_response
        self._windows = windows

    def run(
        self, query: str, *, context: str, request_id: str, invocation_id: str, usage: Usage
    ) -> Any:
        started = time.monotonic()
        time.sleep(_FAKE_SLEEP_SECONDS)
        self._windows[invocation_id] = (started, time.monotonic())
        return self._make_response(request_id, invocation_id)


def _overlap_ms(windows: dict[str, tuple[float, float]]) -> float | None:
    if len(windows) != 2:
        return None
    (a_start, a_end), (b_start, b_end) = windows.values()
    return max(0.0, (min(a_end, b_end) - max(a_start, b_start)) * 1000)


# ------------------------------------------------------------------------------ the check


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--query", default=DEFAULT_QUERY, help="the operational query to run")
    parser.add_argument(
        "--fake-agents",
        action="store_true",
        help="prove executor concurrency with scripted agents - zero Claude calls",
    )
    args = parser.parse_args(argv)

    tracing = TracingSettings()
    if not tracing.configured:
        print(
            "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set (shell or .env); "
            "this check exists to produce a trace, so there is nothing to do without them.",
            file=sys.stderr,
        )
        return 2
    if not args.fake_agents and LLMSettings().anthropic_api_key is None:
        print(
            "ANTHROPIC_API_KEY is not set (shell or .env). "
            "Use --fake-agents for the zero-cost mode.",
            file=sys.stderr,
        )
        return 2

    tracer = LangfuseTracer(tracing)
    mode = "fake-agents (0 Claude calls)" if args.fake_agents else "live (~3 Claude calls)"
    print(f"Day 9 trace check - {mode}; traces -> {tracing.host}\n")

    windows: dict[str, tuple[float, float]] = {}
    start = time.monotonic()

    with RunRecorder(
        kind="llm",
        name="day9-trace",
        command="check_day9_trace.py",
        metadata={"query": args.query, "mode": "fake" if args.fake_agents else "live"},
    ) as run:
        try:
            resp: CoordinatorResponse
            if args.fake_agents:
                plan = _fake_plan(args.query)
                executor = Executor(
                    {
                        AgentName.INCIDENT: _SleepingRunner(_fake_incident_response, windows),
                        AgentName.DOCS: _SleepingRunner(_fake_docs_response, windows),
                    },
                    tracer=tracer,
                )
                resp = executor.execute(plan, args.query)
            else:
                resp = respond(args.query, coordinator=Coordinator(), tracer=tracer)
        except Exception as exc:
            run.event(
                "trace",
                outcome="failed",
                duration_ms=(time.monotonic() - start) * 1000,
                type="llm_call",
                message=f"{type(exc).__name__}: {exc}",
            )
            print(f"  FAIL  {type(exc).__name__}: {exc}")
            return 1
        finally:
            tracer.flush()

        duration_ms = (time.monotonic() - start) * 1000
        complaints: list[str] = []
        if resp.trace_id is None:
            complaints.append("trace_id is null despite a configured tracer")
        parallel_agents = [inv.agent.value for inv in resp.selected_agents if not inv.depends_on]
        if len(parallel_agents) < 2:
            complaints.append(
                f"the plan put fewer than two agents in the parallel group ({parallel_agents}); "
                "the trace cannot show concurrency"
            )
        if len(resp.agent_responses) < 2:
            complaints.append(
                f"only {len(resp.agent_responses)} agent(s) responded; see unresolved_gaps"
            )
        overlap = _overlap_ms(windows)
        if args.fake_agents and (overlap is None or overlap <= 0):
            complaints.append("the two runners' wall-clock windows did not overlap")

        passed = not complaints
        run.event(
            "trace",
            outcome="passed" if passed else "failed",
            duration_ms=duration_ms,
            type="llm_call",
            data={
                "query": args.query,
                "trace_id": resp.trace_id,
                "status": resp.status.value,
                "parallel_agents": parallel_agents,
                "agents_responded": [r.agent.value for r in resp.agent_responses],
                "overlap_ms": overlap,
                "cost": {"in": resp.cost.input_tokens, "out": resp.cost.output_tokens},
                "complaints": complaints,
            },
            message="; ".join(complaints) if complaints else "trace recorded with parallel agents",
        )
        run.artifact("coordinator_response.json", resp.model_dump_json(indent=2))

        print(f"  status     {resp.status.value}")
        print(f"  parallel   {', '.join(parallel_agents) or '-'}")
        print(f"  responded  {', '.join(r.agent.value for r in resp.agent_responses) or '-'}")
        if overlap is not None:
            print(f"  overlap    {overlap:.0f} ms measured locally")
        print(f"  cost       {resp.cost.input_tokens} in / {resp.cost.output_tokens} out")
        print(f"  trace_id   {resp.trace_id}")
        trace_url = tracer.trace_url(resp.trace_id) if resp.trace_id else None
        print(f"  open       {trace_url or tracing.host}")
        for complaint in complaints:
            print(f"  ! {complaint}")

    print(f"\n--- day 9 trace: {'PASS' if passed else 'FAIL'} ---")
    print(f"records: {run.dir}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
