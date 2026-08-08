"""Day 5 checkpoint: break the app, and check the Incident agent produces valid JSON about it.

    uv run python scripts/check_day5_checkpoint.py --mode downstream_latency

Costs exactly one Claude API call. Needs the stack up and `ANTHROPIC_API_KEY` set.

This is the Day 19 eval in embryo, and worth reading as such. It injects a known fault, reads
the resulting metrics out of Prometheus, hands the agent nothing but those metrics, and then
scores the agent's `failure_mode` against the fault that was actually injected. The agent never
sees `chaos_knob_value`, so the comparison is real rather than a transcription check - that
exclusion is enforced in `aioc.observability.prometheus`, not just intended here.

Ground truth is read from Prometheus rather than from the injector's own return value, because
the eval on Day 19 will read it the same way: whatever is in the gauges is what actually
happened to the app, which is a stronger claim than what a script believes it requested.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.agents import IncidentAgent  # noqa: E402
from aioc.llm import LLMSettings  # noqa: E402
from aioc.observability import (  # noqa: E402
    PrometheusClient,
    PrometheusError,
    Window,
    build_incident_context,
)

# Which knob each failure mode moves, mirroring demo-app/chaos/inject.py. Used only to read the
# ground truth back out of Prometheus, never to tell the agent anything.
_MODE_FINGERPRINT = {
    "resource_exhaustion": ("checkout-api", "leak_mb_per_request"),
    "downstream_latency": ("payments-api", "extra_latency_ms"),
    "code_regression": ("checkout-api", "error_rate"),
    "bad_config_deploy": ("payments-api", "error_rate"),
}

QUERY = (
    "Checkout success rate is dropping and customers are complaining. "
    "What is going on, and what should we do first?"
)


def _injected_mode(client: PrometheusClient) -> tuple[str | None, dict[str, float]]:
    """Recover the injected mode from the chaos gauges - the eval's ground truth."""
    active: dict[str, float] = {}
    for series in client.instant("chaos_knob_value"):
        metric = series.get("metric", {})
        try:
            value = float(series["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if value:
            active[f"{metric.get('service')}::{metric.get('knob')}"] = value

    for mode, (service, knob) in _MODE_FINGERPRINT.items():
        if f"{service}::{knob}" in active:
            return mode, active
    return None, active


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=sorted(_MODE_FINGERPRINT), default="downstream_latency")
    parser.add_argument(
        "--settle-seconds",
        type=int,
        default=45,
        help="wait before reading metrics; rate() needs samples to accumulate (default 45)",
    )
    parser.add_argument("--window-minutes", type=int, default=10)
    args = parser.parse_args(argv)

    if LLMSettings().anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2

    prom = PrometheusClient()
    try:
        truth_mode, knobs = _injected_mode(prom)
    except PrometheusError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if truth_mode is None:
        print(
            "No chaos is currently injected, so there is nothing to diagnose. Inject one first:\n"
            f"  uv run python demo-app/chaos/inject.py --mode {args.mode}",
            file=sys.stderr,
        )
        return 2

    print(f"Injected ground truth (from chaos_knob_value): {truth_mode}  {knobs}")
    print(f"Waiting {args.settle_seconds}s for rate() windows to fill...")
    time.sleep(args.settle_seconds)

    with RunRecorder(
        kind="llm",
        name="day5-checkpoint",
        command=f"check_day5_checkpoint.py --mode {args.mode}",
        metadata={"injected_mode": truth_mode, "chaos_knobs": knobs},
    ) as run:
        window = Window.last(args.window_minutes)
        context = build_incident_context(
            prom,
            window,
            extra_notes=(
                "Deploy log: no deploys to any service in the last 24 hours.\n"
                "On-call note: no infrastructure maintenance scheduled."
            ),
        )
        run.artifact("context.txt", context)
        print("\n--- context handed to the agent (live Prometheus) ---")
        print(context)

        start = time.monotonic()
        try:
            response = IncidentAgent().diagnose(QUERY, context=context)
        except Exception as exc:  # noqa: BLE001 - the record is the point, then re-raise
            run.event(
                "diagnose",
                outcome="failed",
                duration_ms=(time.monotonic() - start) * 1000,
                type="llm_call",
                message=f"{type(exc).__name__}: {exc}",
                detail=str(exc),
                data={"injected_mode": truth_mode},
            )
            print(f"\nFAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        duration_ms = (time.monotonic() - start) * 1000
        run.artifact("response.json", response.model_dump_json(indent=2))

        found = response.findings.failure_mode
        found_value = None if found.value is None else found.value.value
        correct = found_value == truth_mode

        run.event(
            "diagnose",
            outcome="passed",
            duration_ms=duration_ms,
            type="llm_call",
            data={
                "injected_mode": truth_mode,
                "diagnosed_mode": found_value,
                "mode_correct": correct,
                "mode_confidence": found.confidence,
                "status": response.status.value,
                "affected_services": response.findings.affected_services,
                "evidence_count": len(response.evidence),
                "gap_count": len(response.gaps),
                "overall_confidence": response.overall_confidence,
            },
            message=response.summary[:300],
        )

    print("\n--- checkpoint ---")
    print("  schema-validated response : yes (diagnose would have raised otherwise)")
    print(f"  injected                  : {truth_mode}")
    print(f"  diagnosed                 : {found_value} @ confidence {found.confidence}")
    print(f"  mode matches ground truth : {'YES' if correct else 'NO'}")
    print(f"  affected services         : {response.findings.affected_services}")
    print(f"  evidence / gaps           : {len(response.evidence)} / {len(response.gaps)}")
    print(f"  summary                   : {response.summary[:160]}")
    print(f"\nrecords: {run.dir}")

    # The Day 5 done-when is "produces valid JSON about it", which validation already proves.
    # A wrong mode is a quality signal for Day 19 to score, not a failure of this checkpoint -
    # so it is reported loudly and does not change the exit code.
    if not correct:
        print(
            "\nNote: the response is contract-valid but the failure mode does not match the "
            "injected truth. That is exactly what the Day 19 eval exists to measure.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
