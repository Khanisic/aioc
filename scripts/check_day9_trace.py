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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.api.smoke import overlap_ms, smoke_plan, smoke_runners  # noqa: E402
from aioc.contracts import CoordinatorResponse  # noqa: E402
from aioc.coordinator import Coordinator, Executor  # noqa: E402
from aioc.coordinator.executor import respond  # noqa: E402
from aioc.llm import LLMSettings  # noqa: E402
from aioc.observability.tracing import LangfuseTracer, TracingSettings  # noqa: E402

DEFAULT_QUERY = (
    "checkout-api error rate just spiked - diagnose what is failing right now, and pull "
    "up how we resolved similar payments-api incidents in the past."
)


# The fake-agents fixtures live in `aioc.api.smoke` since Day 24: the deployed service's
# zero-cost smoke request runs the same scripted plan and agents.


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
    # Fail fast on bad credentials: span export runs on a background thread, so without
    # this check a 401 surfaces as a logged-and-swallowed batch failure while the run
    # reports success - and the trace this script exists to produce is silently lost.
    try:
        authed = tracer.auth_check()
    except Exception as exc:
        authed = False
        auth_error: str | None = f"{type(exc).__name__}: {exc}"
    else:
        auth_error = None if authed else "auth_check returned false"
    if not authed:
        print(
            f"Langfuse rejected the credentials against {tracing.host} ({auth_error}).\n"
            "If the account is US-region, set LANGFUSE_HOST=https://us.cloud.langfuse.com "
            "in .env (the default is the EU host, and a wrong region reads as 401 invalid "
            "credentials); otherwise re-check the keys.",
            file=sys.stderr,
        )
        return 2

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
                plan = smoke_plan(args.query)
                executor = Executor(smoke_runners(windows), tracer=tracer)
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
        overlap = overlap_ms(windows)
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
                "cost": resp.cost.model_dump(),
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
