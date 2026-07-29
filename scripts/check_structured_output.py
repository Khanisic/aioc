"""Live check: does `IncidentAgent.diagnose` return a contract-valid response, per model?

This is the Day 4 done-when condition exercised against the real API rather than a scripted
fake client, and it is the gate Day 5's checkpoint sits behind. It doubles as the evidence for
choosing a default model: run it across a matrix and the cheapest model that passes wins.

    uv run python scripts/check_structured_output.py
    uv run python scripts/check_structured_output.py --models claude-haiku-4-5-20251001
    uv run python scripts/check_structured_output.py --repeat 3

Every attempt is recorded under `test-results/` (see that directory's README). A failure records
the individual validation errors, so "which invariant did it break" survives the terminal
scrollback - that list is what tells you whether the fix is a prompt change or a retry loop.

Needs `ANTHROPIC_API_KEY` (shell or `.env`) and costs real tokens: one call per model per repeat.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.agents import IncidentAgent, IncidentAgentError  # noqa: E402
from aioc.llm import LLMClient, LLMSettings  # noqa: E402

# Cheapest first, so the matrix answers "what is the cheapest model that holds the contract?"
# in reading order. Opus is the coordinator's model (Day 23), included as the ceiling.
DEFAULT_MODELS = ("claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5")

QUERY = (
    "Checkout success rate is dropping and customers are complaining. "
    "What is going on and what should we do first?"
)

# Deliberately the same fixture as examples/incident_structured_demo.py: a known-answer case
# (payments-api degrading, inventory-api clean, no deploys) so a wrong diagnosis is visible.
CONTEXT = """\
Service inventory: checkout-api (front), payments-api, inventory-api (downstreams).

Prometheus observations, window 14:00-14:15 UTC:
- payments-api http_requests_total 5xx ratio: 0.1% at 14:00, 7.4% at 14:10, still rising.
- payments-api http_request_duration_seconds p99: 120ms at 14:00, 2100ms at 14:10.
- checkout-api 5xx ratio: 0.1% -> 2.1% over the same window (calls payments-api per checkout).
- inventory-api: all metrics flat and nominal.
- process_resident_memory_bytes for payments-api: steady growth from 180MB at 12:00
  to 1.4GB at 14:10.

Deploy log: no deploys to any of the three services in the last 24 hours.
On-call note: no infrastructure maintenance scheduled."""


def _attempt(model: str, run: RunRecorder, index: int) -> bool:
    """One live call. Returns True if the response validated against the frozen contract."""
    agent = IncidentAgent(LLMClient(LLMSettings(model=model)))
    label = f"{model}#{index}" if index else model
    start = time.monotonic()

    try:
        response = agent.diagnose(QUERY, context=CONTEXT)
    except ValidationError as exc:
        errors = [
            {
                "field": ".".join(str(part) for part in err["loc"]),
                "type": err["type"],
                "message": err["msg"],
            }
            for err in exc.errors()
        ]
        run.event(
            label,
            outcome="failed",
            duration_ms=(time.monotonic() - start) * 1000,
            type="llm_call",
            data={"model": model, "error_class": "ValidationError", "errors": errors},
            message=f"{len(errors)} contract violation(s): "
            + "; ".join(sorted({e["message"] for e in errors}))[:300],
            detail=str(exc),
        )
        return False
    except IncidentAgentError as exc:
        # No forced tool call at all - a different failure from a bad payload, and a much worse
        # one: it means tool_choice did not hold.
        run.event(
            label,
            outcome="error",
            duration_ms=(time.monotonic() - start) * 1000,
            type="llm_call",
            data={"model": model, "error_class": type(exc).__name__},
            message=str(exc),
        )
        return False

    duration_ms = (time.monotonic() - start) * 1000
    run.artifact(f"{_slug(label)}.response.json", response.model_dump_json(indent=2))
    findings = response.findings
    run.event(
        label,
        outcome="passed",
        duration_ms=duration_ms,
        type="llm_call",
        data={
            "model": model,
            "status": response.status.value,
            "overall_confidence": response.overall_confidence,
            "failure_mode": None
            if findings.failure_mode.value is None
            else findings.failure_mode.value.value,
            "failure_mode_confidence": findings.failure_mode.confidence,
            "severity": None if findings.severity.value is None else findings.severity.value.value,
            "affected_services": findings.affected_services,
            "evidence_count": len(response.evidence),
            "gap_count": len(response.gaps),
            "action_count": len(findings.recommended_actions),
        },
        message=response.summary[:300],
    )
    return True


def _slug(value: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--repeat", type=int, default=1, help="calls per model (non-determinism)")
    args = parser.parse_args(argv)

    if LLMSettings().anthropic_api_key is None:
        print(
            "ANTHROPIC_API_KEY is not set.\n"
            "  Add it to .env (see the DAY 2 block in .env.example) or export it.",
            file=sys.stderr,
        )
        return 2

    tally: dict[str, dict[str, int]] = {m: {"passed": 0, "failed": 0} for m in args.models}
    with RunRecorder(
        kind="llm",
        name="structured-output",
        command=f"check_structured_output.py --models {' '.join(args.models)}",
        metadata={"models": args.models, "repeat": args.repeat, "query": QUERY},
    ) as run:
        for model in args.models:
            for index in range(args.repeat):
                ok = _attempt(model, run, index if args.repeat > 1 else 0)
                tally[model]["passed" if ok else "failed"] += 1
                print(f"  {'PASS' if ok else 'FAIL'}  {model}")
        run.metadata["tally"] = tally

    print("\n--- structured output vs the frozen contract ---")
    for model, counts in tally.items():
        print(f"  {model:<32} {counts['passed']}/{counts['passed'] + counts['failed']} valid")
    print(f"\nrecords: {run.dir}")
    return 0 if all(c["failed"] == 0 for c in tally.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
