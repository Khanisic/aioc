"""Day 15 cost review: what the recorded live runs spent, priced, against the spend alert.

    uv run python scripts/cost_review.py                  # free - reads test-results/ only
    uv run python scripts/cost_review.py --alert 100      # the Console alert, in USD
    uv run python scripts/cost_review.py --since 2026-09-01
    uv run python scripts/cost_review.py --json           # the same tally, machine-readable

Free: no API calls, no network. It reads the run records `scripts/runlog.py` writes (every
live check records itself) and adds up the token counts the checks measured.

**This is a floor, not the bill.** Three things it cannot see, and says so in its output:

- runs that recorded no usage - the early checks (Days 4-7) predate the `Usage` seam, and a
  run that raised before its first response has nothing to record;
- anything that never went through the recorder (`examples/`, an ad-hoc REPL call);
- Voyage embedding calls, which are a different bill.

The Anthropic Console is the truth; this is the breakdown the Console does not give - which
check spent it, on which day, at how many tokens a run. The review compares the two: a
Console figure far above this floor means unrecorded spend worth finding.

Prices are per million tokens, from the published table as cached on 2026-06-24. A model
missing from `PRICES` is reported unpriced rather than guessed at. No recorded run used
prompt caching (HANDOFF sec 7 item 4), so every input token is priced at the full rate;
when caching lands (Day 19) the records will need the cache counters for this to stay right.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_RESULTS = Path(__file__).resolve().parents[1] / "test-results"

DEFAULT_MODEL = "claude-sonnet-5"

# USD per million tokens: (input, output).
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def _price_key(model: str) -> str | None:
    """`claude-haiku-4-5-20251001` is priced as `claude-haiku-4-5`."""
    return next((known for known in PRICES if model.startswith(known)), None)


@dataclass(slots=True)
class Tally:
    runs: int = 0
    unmeasured_runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    unpriced_models: set[str] = field(default_factory=set)

    def add(self, other: Tally) -> None:
        self.runs += other.runs
        self.unmeasured_runs += other.unmeasured_runs
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.usd += other.usd
        self.unpriced_models |= other.unpriced_models


def _tokens(data: dict[str, Any]) -> tuple[int, int] | None:
    """The two shapes the checks have recorded over time: `usage` or `cost`, keyed `in`/`out`
    or `input_tokens`/`output_tokens`. None when the event measured nothing."""
    block = data.get("usage") or data.get("cost")
    if not isinstance(block, dict):
        return None
    tokens_in = block.get("in", block.get("input_tokens"))
    tokens_out = block.get("out", block.get("output_tokens"))
    if not isinstance(tokens_in, int) or not isinstance(tokens_out, int):
        return None
    return tokens_in, tokens_out


def _event_model(event: dict[str, Any], run: dict[str, Any]) -> str:
    name = event.get("name")
    if isinstance(name, str) and name.startswith("claude-"):
        return name  # the per-model checks name each event after the model it called
    data = event.get("data") or {}
    model = data.get("model") or (run.get("env") or {}).get("aioc_model")
    return model if isinstance(model, str) and model else DEFAULT_MODEL


def tally_run(run_dir: Path) -> Tally:
    """One run directory -> its measured tokens and price. A run none of whose events
    measured usage counts as unmeasured, never as zero."""
    tally = Tally(runs=1)
    run_file, events_file = run_dir / "run.json", run_dir / "events.jsonl"
    run = json.loads(run_file.read_text(encoding="utf-8")) if run_file.is_file() else {}
    measured = False
    if events_file.is_file():
        for line in events_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            tokens = _tokens(event.get("data") or {})
            if tokens is None or tokens == (0, 0):
                continue
            measured = True
            tokens_in, tokens_out = tokens
            tally.input_tokens += tokens_in
            tally.output_tokens += tokens_out
            model = _event_model(event, run)
            key = _price_key(model)
            if key is None:
                tally.unpriced_models.add(model)
                continue
            price_in, price_out = PRICES[key]
            tally.usd += tokens_in / 1e6 * price_in + tokens_out / 1e6 * price_out
    if not measured:
        tally.unmeasured_runs = 1
    return tally


def review(results: Path, since: str | None) -> dict[str, Any]:
    index = results / "index.jsonl"
    if not index.is_file():
        raise SystemExit(f"{index} does not exist - nothing has been recorded on this machine")
    by_check: dict[str, Tally] = defaultdict(Tally)
    by_day: dict[str, Tally] = defaultdict(Tally)
    total = Tally()
    for line in index.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        day = str(row.get("started_at", ""))[:10]
        if row.get("kind") != "llm" or (since is not None and day < since):
            continue
        tally = tally_run(results / row["path"])
        by_check[row["name"]].add(tally)
        by_day[day].add(tally)
        total.add(tally)
    return {"by_check": dict(by_check), "by_day": dict(by_day), "total": total}


def _row(label: str, t: Tally) -> str:
    unmeasured = f"{t.unmeasured_runs}" if t.unmeasured_runs else "-"
    return (
        f"  {label:<22} {t.runs:>4} {unmeasured:>10} {t.input_tokens:>12,} "
        f"{t.output_tokens:>10,} {t.usd:>9.2f}"
    )


def _as_json(t: Tally) -> dict[str, Any]:
    return {
        "runs": t.runs,
        "unmeasured_runs": t.unmeasured_runs,
        "input_tokens": t.input_tokens,
        "output_tokens": t.output_tokens,
        "usd": round(t.usd, 4),
        "unpriced_models": sorted(t.unpriced_models),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--alert", type=float, default=100.0, help="the spend alert, in USD")
    parser.add_argument("--since", default=None, help="only runs on or after YYYY-MM-DD")
    parser.add_argument("--results", default=str(_RESULTS), help="the test-results directory")
    parser.add_argument("--json", action="store_true", help="print the tally as JSON")
    args = parser.parse_args(argv)

    result = review(Path(args.results), args.since)
    total: Tally = result["total"]

    if args.json:
        payload = {
            "alert_usd": args.alert,
            "since": args.since,
            "total": _as_json(total),
            "by_check": {k: _as_json(v) for k, v in sorted(result["by_check"].items())},
            "by_day": {k: _as_json(v) for k, v in sorted(result["by_day"].items())},
        }
        print(json.dumps(payload, indent=2))
        return 0

    header = f"  {'':<22} {'runs':>4} {'unmeasured':>10} {'input tok':>12} {'output tok':>10} "
    header += f"{'USD':>9}"
    print("Recorded live runs, by check")
    print(header)
    for name, tally in sorted(result["by_check"].items(), key=lambda kv: -kv[1].usd):
        print(_row(name, tally))
    print("\nBy day")
    print(header)
    for day, tally in sorted(result["by_day"].items()):
        print(_row(day, tally))
    print()
    print(_row("TOTAL (measured)", total))
    share = total.usd / args.alert * 100 if args.alert else 0.0
    print(f"\n  measured spend is {share:.1f}% of the ${args.alert:,.0f} alert")
    if total.unmeasured_runs:
        print(
            f"  {total.unmeasured_runs} of {total.runs} runs recorded no usage (early checks, or "
            "runs that raised first) - this is a floor, and the Console is the bill"
        )
    if total.unpriced_models:
        print(f"  unpriced models (tokens counted, USD not): {sorted(total.unpriced_models)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
