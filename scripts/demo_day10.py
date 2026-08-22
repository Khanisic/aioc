"""Day 10: the first real end-to-end demo - one query through the whole system.

    uv run python scripts/demo_day10.py                # ~3 Claude calls
    uv run python scripts/demo_day10.py --skip-inject  # reuse whatever chaos is active

Costs ~3 Claude API calls (one plan + one per selected agent, typically Incident + Docs)
plus a fraction-of-a-cent Voyage query embed when that key is set. Needs the Docker stack
up and `ANTHROPIC_API_KEY`; traces to Langfuse when those keys are set.

The story, end to end, with nothing scripted after the injection:

1. Break the app for real (`downstream_latency`: payments-api slows down 15x).
2. Read the resulting metrics out of Prometheus - the coordinator's situation block.
3. Ask the execution plan's canonical query: "Why did latency spike after the last deploy?"
4. The coordinator plans (dynamic selection, explicit context per agent, reasons for every
   skipped agent), the executor runs Incident + Docs **in parallel**, and the response is
   one contract-valid `CoordinatorResponse` with measured cost and a Langfuse trace id.

The deploy-log line in the situation block is scenario framing (the on-call's knowledge),
same as the Day 5 checkpoint's "no deploys" note - the metrics are real, the injected
fault is real, and the agents see only what the coordinator wrote into their context.

Every line printed here is also recorded with its wall-clock offset into a transcript
artifact, which `scripts/render_demo_gif.py` turns into the Day 10 checkpoint GIF.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # noqa: S404 - runs our own chaos injector, with fixed arguments
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.contracts import DocsAgentResponse, IncidentAgentResponse  # noqa: E402
from aioc.coordinator.executor import respond  # noqa: E402
from aioc.llm import LLMSettings  # noqa: E402
from aioc.observability import (  # noqa: E402
    PrometheusClient,
    PrometheusError,
    Window,
    build_incident_context,
)
from aioc.observability.tracing import LangfuseTracer, default_tracer  # noqa: E402

QUERY = "Why did latency spike after the last deploy?"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_INJECTOR = _REPO_ROOT / "demo-app" / "chaos" / "inject.py"

# The on-call's framing for the canonical query: a deploy recently happened. The metrics
# themselves come live from Prometheus; this note is the human context around them.
_DEPLOY_NOTE = (
    "Deploy log: payments-api rolled out a dependency update roughly 20 minutes ago; "
    "checkout-api and inventory-api have not been deployed in 24 hours.\n"
    "On-call note: no infrastructure maintenance scheduled."
)


class _Transcript:
    """Prints and records every line with its wall-clock offset, for the GIF renderer."""

    def __init__(self) -> None:
        self._start = time.monotonic()
        self.lines: list[tuple[float, str]] = []

    def say(self, text: str = "") -> None:
        for line in text.split("\n"):
            print(line, flush=True)
            self.lines.append((round(time.monotonic() - self._start, 3), line))

    def to_json(self) -> str:
        return json.dumps(
            [{"t": t, "line": line} for t, line in self.lines], indent=2, ensure_ascii=True
        )


def _inject(mode: str, t: _Transcript) -> bool:
    result = subprocess.run(  # noqa: S603 - our own script, fixed args
        [sys.executable, str(_INJECTOR), "--mode", mode],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    for line in (result.stdout or "").strip().splitlines():
        t.say(f"    {line}")
    if result.returncode != 0:
        for line in (result.stderr or "").strip().splitlines():
            t.say(f"    {line}")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--mode",
        choices=[
            "resource_exhaustion",
            "downstream_latency",
            "code_regression",
            "bad_config_deploy",
        ],
        default="downstream_latency",
        help="failure mode to inject (default matches the canonical latency query)",
    )
    parser.add_argument(
        "--settle-seconds",
        type=int,
        default=45,
        help="wait after injection so rate() windows fill (default 45)",
    )
    parser.add_argument(
        "--skip-inject",
        action="store_true",
        help="do not inject or wait; diagnose whatever the app is doing right now",
    )
    parser.add_argument("--window-minutes", type=int, default=10)
    parser.add_argument(
        "--query",
        default=QUERY,
        help="override the canonical query - e.g. append a docs question to route two "
        "agents in parallel (the coordinator selects dynamically, so a pure diagnostic "
        "query legitimately runs Incident alone)",
    )
    args = parser.parse_args(argv)

    if LLMSettings().anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2

    t = _Transcript()
    say = t.say

    say("=" * 72)
    say("AIOC - Enterprise AI Operations Center")
    say("Day 10: one query, end to end - plan, parallel agents, cited answer")
    say("=" * 72)
    say()

    if not args.skip_inject:
        say(f"[1/4] Injecting a real fault: {args.mode}")
        if not _inject(args.mode, t):
            print("chaos injection failed - is the stack up?", file=sys.stderr)
            return 2
        say(f"      waiting {args.settle_seconds}s for Prometheus rate() windows to fill...")
        time.sleep(args.settle_seconds)
    else:
        say("[1/4] Skipping injection; diagnosing the app as it is.")
    say()

    say("[2/4] Reading live metrics from Prometheus -> the coordinator's situation block")
    prom = PrometheusClient()
    try:
        situation = build_incident_context(
            prom, Window.last(args.window_minutes), extra_notes=_DEPLOY_NOTE
        )
    except PrometheusError as exc:
        print(f"Prometheus is unreachable: {exc}", file=sys.stderr)
        return 2
    for line in situation.splitlines():
        say(f"    {line}")
    say()

    tracer = default_tracer()
    traced = isinstance(tracer, LangfuseTracer)
    say(f'[3/4] Asking the coordinator: "{args.query}"')
    say(
        "      (planning + parallel agent execution: ~3 Claude calls"
        + (", traced to Langfuse)" if traced else "; tracing off - no Langfuse keys)")
    )
    say()

    with RunRecorder(
        kind="llm",
        name="day10-demo",
        command=f"demo_day10.py --mode {args.mode}",
        metadata={"query": args.query, "mode": args.mode, "traced": traced},
    ) as run:
        run.artifact("situation.txt", situation)
        start = time.monotonic()
        try:
            resp = respond(args.query, situation=situation, tracer=tracer)
        except Exception as exc:
            run.event(
                "demo",
                outcome="failed",
                duration_ms=(time.monotonic() - start) * 1000,
                type="llm_call",
                message=f"{type(exc).__name__}: {exc}",
            )
            tracer.flush()
            print(f"\nFAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        wall_seconds = time.monotonic() - start
        tracer.flush()

        say("[4/4] The coordinator's answer")
        say()
        intent = resp.intent.value.value if resp.intent.value else "unclassified"
        say(f"  intent      {intent} @ {resp.intent.confidence:.2f}")
        for inv in resp.selected_agents:
            deps = f" after {', '.join(inv.depends_on)}" if inv.depends_on else ""
            say(f"  selected    {inv.agent.value:<11} {inv.mode.value}{deps}")
        for skipped in resp.skipped_agents:
            say(f"  skipped     {skipped.agent.value:<11} {skipped.reason}")
        say()

        for agent_resp in resp.agent_responses:
            say(
                f"  -- {agent_resp.agent.value} agent "
                f"({agent_resp.status.value}, confidence {agent_resp.overall_confidence:.2f})"
            )
            say(f"     {agent_resp.summary}")
            if isinstance(agent_resp, IncidentAgentResponse):
                mode_found = agent_resp.findings.failure_mode
                mode_value = mode_found.value.value if mode_found.value else "undetermined"
                say(f"     failure_mode: {mode_value} @ {mode_found.confidence:.2f}")
                say(f"     affected: {', '.join(agent_resp.findings.affected_services) or '-'}")
            if isinstance(agent_resp, DocsAgentResponse):
                for claim in agent_resp.findings.claims:
                    if not claim.supported:
                        continue
                    docs = ", ".join(s.document_id for s in claim.sources)
                    say(f"     claim [{docs}]: {claim.statement}")
            say()

        say(f"  answer ({resp.answer.confidence:.2f}): {resp.answer.value}")
        say()
        say(
            f"  status {resp.status.value} | cost {resp.cost.input_tokens} in / "
            f"{resp.cost.output_tokens} out | {wall_seconds:.1f}s wall | "
            f"gaps {len(resp.unresolved_gaps)}"
        )
        trace_url = (
            tracer.trace_url(resp.trace_id) if traced and resp.trace_id is not None else None
        )
        say(f"  trace  {trace_url or resp.trace_id or 'not traced'}")

        parallel_agents = [inv.agent.value for inv in resp.selected_agents if not inv.depends_on]
        run.artifact("response.json", resp.model_dump_json(indent=2))
        run.artifact("transcript.json", t.to_json())
        run.event(
            "demo",
            outcome="passed",
            duration_ms=wall_seconds * 1000,
            type="llm_call",
            data={
                "query": args.query,
                "mode": args.mode,
                "status": resp.status.value,
                "intent": intent,
                "parallel_agents": parallel_agents,
                "agents_responded": [r.agent.value for r in resp.agent_responses],
                "trace_id": resp.trace_id,
                "trace_url": trace_url,
                "cost": {"in": resp.cost.input_tokens, "out": resp.cost.output_tokens},
                "wall_seconds": round(wall_seconds, 1),
                "unresolved_gaps": len(resp.unresolved_gaps),
            },
            message=(resp.answer.value or resp.synthesis)[:300],
        )

    say()
    say(f"records: {run.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
