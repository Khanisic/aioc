"""Day 20 checkpoint: the full eval run, three ways, and the baseline committed from it.

    uv run python scripts/check_day20_baseline.py --plan       # free - what it will do and cost
    uv run python scripts/check_day20_baseline.py              # LIVE - 118 calls
    uv run python scripts/check_day20_baseline.py --limit 6    # LIVE - a rehearsal, 22 calls
    uv run python scripts/check_day20_baseline.py --resume     # LIVE - only what is not done

**Cost.** A smoke test of 4 calls, then the whole eval set (38 items) once for each
configuration: realtime and uncached, realtime and cached, batch and cached. 118 Claude
calls plus one per validation retry, and one Voyage query embedding per recall per run.
`--plan` prints the projection for the selection in hand; for the full set it is about
$2.70 before caching lowers the two cached runs. Needs the Docker stack (the recalls
retrieve from the corpus). The batch run waits on the Batch API: usually minutes, up to
an hour.

**The order is the point.** The smoke test runs first and the rest is not started unless it
passes, because the two things it checks fail silently: a request whose prefix varies is
served and billed as a cache write every time, and a wire that is wrong for one item is
wrong for thirty-eight. Four calls find out for about twelve cents.

**`--resume` never pays for an answer twice.** For each configuration it looks under
`test-results/` for a run of the same set, model, and configuration, takes the one that
got furthest, and asks only for the items that run did not finish. A configuration that
was already run to the end costs nothing the second time. This is what to run after the
checkpoint has been stopped - by an empty balance, a revoked key, or a closed laptop.

**What it writes.** Each run records itself under `test-results/` as `run_evals.py` would.
On the full set it also writes `evaluations/results/<configuration>.md` for each run and
`evaluations/baseline.md` with `evaluations/baseline.json` beside it - the file Day 24's
re-run is compared against (`scripts/eval_baseline.py --against`). On a selection nothing
is written under `evaluations/`: a baseline on part of the set is a rehearsal, and a
committed one would be compared with as if it were the whole.

**What it holds the runs to.** Not a score - the scores are the measurement. It checks the
things that would make the measurement wrong: a run that answered nothing, a cache that did
not read, a lever that saved nothing on its own tokens, and runs that disagree with each
other on more than a fifth of the items, which would mean the modes are not sending the same
request.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_evals  # noqa: E402 - needs the sys.path insert above
from eval_baseline import _started, _write  # noqa: E402
from runlog import RESULTS_ROOT, RunRecorder  # noqa: E402

from aioc.evals import (  # noqa: E402
    EvalAborted,
    EvalItem,
    EvalSet,
    RunConfig,
    StoreError,
    Task,
    build_baseline,
    cache_health,
    load_cases,
    read_run,
    render_baseline,
)
from aioc.llm import LLMSettings  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]

SMOKE_ITEMS = 4

# The most the runs may disagree on before it stops being model noise.
MAX_DISAGREEMENT = 0.2


@dataclass(frozen=True, slots=True)
class Config:
    name: str
    mode: str
    caching: bool
    cache_ttl: str = "5m"

    @property
    def label(self) -> str:
        return f"{self.mode}, {'cached' if self.caching else 'uncached'}"

    def settings(self, base: LLMSettings) -> LLMSettings:
        return base.model_copy(
            update={"prompt_caching": self.caching, "prompt_cache_ttl": self.cache_ttl}
        )


# A batch may wait longer than five minutes between its first request and its last, so
# its cache entries are written with the hour-long lifetime.
CONFIGS = {
    "realtime-uncached": Config("realtime-uncached", "realtime", caching=False),
    "realtime-cached": Config("realtime-cached", "realtime", caching=True),
    "batch-uncached": Config("batch-uncached", "batch", caching=False),
    "batch-cached": Config("batch-cached", "batch", caching=True, cache_ttl="1h"),
}
DEFAULT_CONFIGS = ("realtime-uncached", "realtime-cached", "batch-cached")


def find_resumable(
    results: Path, cases: EvalSet, config: RunConfig, items: list[EvalItem]
) -> tuple[Path, int] | None:
    """The run under ``results`` that this configuration can continue from, and how many
    of ``items`` it has already paid for - finished, or waiting in a batch it submitted:
    the one that got furthest, the newest of those."""
    wanted = {item.key for item in items}
    best: tuple[int, str, Path] | None = None
    for run_dir in results.glob(f"runs/*/*__llm__evals-{config.mode}*"):
        try:
            stored = read_run(run_dir, cases)
        except StoreError:
            continue
        if config.differs_from(stored.config):
            continue
        kept = len(wanted & set(stored.reusable()))
        # A batch submitted and never read back is paid for and waiting: worth as much
        # as the items it will answer, which is all of the ones not yet done.
        waiting = 0 if stored.complete or not stored.batches else len(wanted) - kept
        worth = kept + waiting
        if worth and (best is None or (worth, _started(run_dir)) > (best[0], best[1])):
            best = (worth, _started(run_dir), run_dir)
    return None if best is None else (best[2], best[0])


def smoke_items(cases: EvalSet) -> list[EvalItem]:
    return list(cases.select(tasks={Task.DIAGNOSE}))[:SMOKE_ITEMS]


def plan(
    items: list[EvalItem],
    configs: list[Config],
    settings: LLMSettings,
    *,
    smoke: list[EvalItem] | None,
) -> tuple[list[str], float | None]:
    """What will run and what it is projected to cost, before caching. ``smoke`` is the
    smoke test's items, or ``None`` when it is skipped."""
    projection = run_evals.project(items, settings)
    flat = projection.realtime_uncached_usd
    lines = [f"model {settings.model}, {len(items)} item(s) a run"]
    total = 0.0 if flat is not None else None
    calls = 0
    if smoke is not None:
        share = run_evals.project(smoke, settings).realtime_uncached_usd
        lines.append(f"  smoke test          {len(smoke):>4} calls   {_usd(share)}")
        calls += len(smoke)
        if total is not None and share is not None:
            total += share
    for config in configs:
        cost = None if flat is None else flat * (0.5 if config.mode == "batch" else 1.0)
        lines.append(f"  {config.label:<19} {len(items):>4} calls   {_usd(cost)}")
        calls += len(items)
        if total is not None and cost is not None:
            total += cost
    lines.append(f"  {'total':<19} {calls:>4} calls   {_usd(total)}")
    lines.append(
        f"  projected from ~{projection.input_tokens:,} input and "
        f"~{projection.output_tokens:,} output tokens a run, both estimated from what the "
        "first live items measured; the cached runs will cost less, and a validation "
        "retry is one more call"
    )
    for key, why in projection.unbuildable:
        lines.append(f"  CANNOT BUILD {key}: {why}")
    return lines, total


def _usd(value: float | None) -> str:
    return "unpriced" if value is None else f"~${value:.2f}"


def evaluate(baseline: dict[str, Any], runs: dict[str, Any]) -> list[str]:
    """The list of complaints. Empty means the baseline can be trusted as a measurement."""
    complaints: list[str] = []
    for entry in baseline["runs"]:
        name = entry["label"]
        answered = entry["rates"]["answered"]
        if not answered["hits"]:
            complaints.append(f"{name}: no item was answered")
        elif answered["hits"] < answered["total"]:
            # Not a complaint on its own - an agent that gave up is a scored result - but
            # a run that lost a tenth of its items is not measuring the set any more.
            if answered["rate"] < 0.9:
                complaints.append(
                    f"{name}: only {answered['hits']}/{answered['total']} items were answered"
                )
        change = entry["same_tokens"]["change"]
        if entry["label"] != "realtime, uncached" and change is not None and change >= 0:
            complaints.append(
                f"{name}: cost {change:+.0%} against its own tokens realtime and uncached - "
                "the lever saved nothing"
            )
        health = cache_health(runs[name]) if name in runs else None
        if health is not None and health.healthy is False:
            complaints.append(f"{name}: {health.note}")
    agreement = baseline["agreement"]
    if agreement["items"]:
        differing = len(agreement["differing"]) / agreement["items"]
        if differing > MAX_DISAGREEMENT:
            complaints.append(
                f"the runs scored {len(agreement['differing'])} of {agreement['items']} items "
                "differently - more than model noise, so check they send the same request"
            )
    return complaints


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true", help="free: print the plan and stop")
    parser.add_argument(
        "--configs",
        default=",".join(DEFAULT_CONFIGS),
        help=f"comma-separated, from: {', '.join(CONFIGS)}",
    )
    parser.add_argument("--skip-smoke", action="store_true", help="go straight to the runs")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="continue from the recorded runs of this set: ask only for what is not done",
    )
    parser.add_argument("--results", type=Path, default=RESULTS_ROOT, help="test-results directory")
    parser.add_argument("--set", type=Path, default=None, help="a case file (default: seeded)")
    parser.add_argument("--tasks", default=None, help="diagnose, recall, or both comma-separated")
    parser.add_argument("--cases", default=None, help="case ids, comma-separated")
    parser.add_argument("--limit", type=int, default=None, help="only the first N items")
    parser.add_argument("--model", default=None, help="override AIOC_MODEL for these runs")
    parser.add_argument("--poll-seconds", type=float, default=20.0, help="batch poll interval")
    parser.add_argument(
        "--out", type=Path, default=_REPO / "evaluations", help="where the baseline is written"
    )
    args = parser.parse_args(argv)

    unknown = [name for name in args.configs.split(",") if name not in CONFIGS]
    if unknown:
        raise SystemExit(f"unknown configuration(s) {unknown}; choose from {sorted(CONFIGS)}")
    configs = [CONFIGS[name] for name in args.configs.split(",")]

    cases = load_cases(args.set)
    items = run_evals._select(cases, args)
    if not items:
        raise SystemExit("the selection is empty")
    whole_set = len(items) == len(cases.items)

    settings = LLMSettings()
    if args.model:
        settings = settings.model_copy(update={"model": args.model})

    lines, _total = plan(
        items, configs, settings, smoke=None if args.skip_smoke else smoke_items(cases)
    )
    print("Day 20 baseline - the plan")
    print("\n".join(lines))

    resumable: dict[str, Path] = {}
    if args.resume:
        print("  continuing from what is recorded, so less than that:")
        for config in configs:
            found = find_resumable(
                args.results,
                cases,
                RunConfig(
                    set_sha256=cases.sha256,
                    model=settings.model,
                    mode=config.mode,
                    prompt_caching=config.caching,
                    cache_ttl=config.cache_ttl,
                ),
                items,
            )
            if found is None:
                print(f"    {config.label:<19} nothing recorded; {len(items)} to run")
            else:
                resumable[config.label] = found[0]
                print(
                    f"    {config.label:<19} {found[1]} paid for in {found[0].name}; "
                    f"{len(items) - found[1]} to run"
                )
    if not whole_set:
        print(
            f"  a rehearsal: {len(items)} of {len(cases.items)} items, so nothing is written "
            f"under {args.out}"
        )
    if args.plan:
        return 0
    if settings.anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2

    with RunRecorder(
        # Not `llm`: the runs below record their own usage, and the cost review would
        # count every token twice if this record carried it as well.
        kind="checkpoint",
        name="day20-baseline",
        command="check_day20_baseline.py " + " ".join(sys.argv[1:]),
        metadata={
            "set_sha256": cases.sha256,
            "items": len(items),
            "configs": [config.name for config in configs],
            "model": settings.model,
        },
    ) as recorder:
        try:
            if not args.skip_smoke:
                print("\n--- smoke test")
                smoke = run_evals.execute(
                    smoke_items(cases),
                    cases,
                    CONFIGS["realtime-cached"].settings(settings),
                    mode="realtime",
                    command="check_day20_baseline.py (smoke)",
                )
                complaints = run_evals.smoke_complaints(smoke.run)
                recorder.event(
                    "smoke",
                    outcome="failed" if complaints else "passed",
                    message="; ".join(complaints) or cache_health(smoke.run).note,
                    data={"run_id": smoke.run_id, "complaints": complaints},
                )
                if complaints:
                    for complaint in complaints:
                        print(f"SMOKE FAIL: {complaint}", file=sys.stderr)
                    print(
                        f"\nStopped after {SMOKE_ITEMS} calls; the full runs were not started. "
                        f"records: {smoke.dir}",
                        file=sys.stderr,
                    )
                    return 1
                print(f"SMOKE PASS: {cache_health(smoke.run).note}")

            done: dict[str, run_evals.Executed] = {}
            for config in configs:
                print(f"\n--- {config.label}")
                done[config.label] = run_evals.execute(
                    items,
                    cases,
                    config.settings(settings),
                    mode=config.mode,
                    poll_seconds=args.poll_seconds,
                    command=f"check_day20_baseline.py ({config.name})",
                    resume_from=resumable.get(config.label),
                )
                recorder.event(
                    config.name,
                    outcome="passed",
                    data={
                        "run_id": done[config.label].run_id,
                        "as_run_usd": done[config.label].record["cost"]["as_run_usd"],
                    },
                )
        except EvalAborted as exc:
            print(f"\nABORTED: {exc}", file=sys.stderr)
            print(
                "  what was finished is kept. When the cause is fixed, continue with:\n"
                "    uv run python scripts/check_day20_baseline.py --resume --skip-smoke",
                file=sys.stderr,
            )
            recorder.event("aborted", outcome="error", message=str(exc))
            return 2

        baseline = build_baseline([executed.record for executed in done.values()])
        report = render_baseline(baseline, heading="Eval baseline (Day 20)")
        complaints = evaluate(baseline, {name: executed.run for name, executed in done.items()})
        recorder.artifact("baseline.md", report)
        recorder.artifact("baseline.json", json.dumps(baseline, indent=2))
        recorder.event(
            "baseline",
            outcome="failed" if complaints else "passed",
            message="; ".join(complaints) or None,
            data={"complaints": complaints, "runs": [run["run_id"] for run in baseline["runs"]]},
        )

    print()
    print(report)
    if whole_set:
        for config in configs:
            target = args.out / "results" / f"{config.name}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(done[config.label].report, encoding="utf-8")
            print(f"written: {target}")
        _write(args.out / "baseline.md", report, baseline)
    else:
        print(f"rehearsal - nothing written under {args.out}; records: {recorder.dir}")

    if complaints:
        print("\n--- FAIL")
        for complaint in complaints:
            print(f"  - {complaint}")
        return 1
    print("\n--- PASS: the runs are comparable and every lever saved on its own tokens")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
