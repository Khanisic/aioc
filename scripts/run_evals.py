"""Day 19: run the eval set, score it against the seed's answer key, and price the run.

    uv run python scripts/run_evals.py --list                   # free - the set, item by item
    uv run python scripts/run_evals.py --show case_04           # free - what the agent is shown
    uv run python scripts/run_evals.py --dry-run                # free - every request, sized
    uv run python scripts/run_evals.py                          # LIVE, realtime: 38 calls
    uv run python scripts/run_evals.py --mode batch             # LIVE, Batch API: half price
    uv run python scripts/run_evals.py --no-cache               # LIVE, the uncached baseline
    uv run python scripts/run_evals.py --tasks diagnose --limit 4
    uv run python scripts/run_evals.py --rescore test-results/runs/<date>/<run>   # free
    uv run python scripts/run_evals.py --recorded-tools         # free - tool success, recorded

**Cost.** One Claude call per item (38 on the full set) plus one per validation retry; a
recall also makes one Voyage query embedding when `VOYAGE_API_KEY` is set. `--dry-run`
prints the size of every request and a projected price before anything is spent, and the
report prices the run three ways from its measured tokens. Needs the Docker stack for the
recall tasks (the corpus is what the Docs agent retrieves from); `--tasks diagnose` needs
nothing but a key.

**What is scored.** The shipped agents, called the way the executor calls them - not the
whole `respond()` pipeline, which costs about a dollar a query and whose planner and
synthesis the seeded incidents carry no answer key for. A diagnosis is scored on failure
mode, severity, and services against the seed's `true_*` columns, and every checkable
statement in it is looked for in the context it was given; a recall is scored on whether
it cited the incident's own post-mortem; a no-precedent probe on whether it declined.

**The modes are the Day 19 cost levers, measured rather than claimed.** `--no-cache` and
the default differ only in the cache marker; realtime and `--mode batch` differ only in
the client the agents are handed. Day 20 runs the combinations and commits the deltas.

Every run is recorded under `test-results/` (`report.md`, `eval.json`, `responses.json`)
and priced by `scripts/cost_review.py`; `--write` also puts the report where it can be
committed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RESULTS_ROOT, RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.contracts import (  # noqa: E402
    AgentResponse,
    CoordinatorResponse,
    DocsAgentResponse,
    IncidentAgentResponse,
)
from aioc.evals import (  # noqa: E402
    EvalAborted,
    EvalItem,
    EvalRun,
    EvalSet,
    ItemResult,
    RetryStats,
    Task,
    load_cases,
    render_markdown,
    run_batch,
    run_item,
    run_realtime,
    score_failure,
    score_item,
    to_record,
    tool_success,
)
from aioc.llm import (  # noqa: E402
    BATCH_DISCOUNT,
    PRICES,
    DeferredClient,
    LLMClient,
    LLMSettings,
    MessageBatcher,
    PendingRequest,
    Usage,
    price,
)
from aioc.llm.pricing import price_key  # noqa: E402
from aioc.retrieval import CorpusSearcher, default_embedder  # noqa: E402

# What a report has cost in output tokens on the recorded live runs (Day 10: 12.4k output
# over ~8 calls). A projection needs a number; this one is measured, and rough.
_ASSUMED_OUTPUT_TOKENS = 2_000


def _select(cases: EvalSet, args: argparse.Namespace) -> list[EvalItem]:
    tasks = {Task(name) for name in args.tasks.split(",")} if args.tasks else None
    wanted = set(args.cases.split(",")) if args.cases else None
    items = list(cases.select(tasks=tasks, cases=wanted))
    if wanted is not None:
        missing = wanted - {item.case_id for item in items}
        if missing:
            raise SystemExit(f"no such case(s) in {cases.path.name}: {sorted(missing)}")
    return items[: args.limit] if args.limit else items


def _settings(args: argparse.Namespace) -> LLMSettings:
    settings = LLMSettings()
    updates: dict[str, Any] = {}
    if args.no_cache:
        updates["prompt_caching"] = False
    if args.cache_ttl:
        updates["prompt_cache_ttl"] = args.cache_ttl
    if args.model:
        updates["model"] = args.model
    return settings.model_copy(update=updates) if updates else settings


# ------------------------------------------------------------------------------ free paths


def _list(cases: EvalSet, items: list[EvalItem]) -> int:
    print(f"{cases.name} v{cases.version} - {cases.description}")
    print(f"  file   {cases.path}")
    print(f"  sha256 {cases.sha256}")
    tasks = Counter(item.task.value for item in items)
    print(f"  items  {len(items)} ({', '.join(f'{n} {t}' for t, n in sorted(tasks.items()))})")
    modes = Counter(
        item.incident.true_failure_mode
        for item in items
        if item.task is Task.DIAGNOSE and item.incident is not None
    )
    print(f"  truth  {', '.join(f'{mode} x{n}' for mode, n in sorted(modes.items()))}")
    print()
    for item in items:
        if item.incident is None:
            truth = "no precedent in the corpus"
        elif item.task is Task.DIAGNOSE:
            truth = f"{item.incident.true_failure_mode}, {item.incident.true_severity}"
        else:
            truth = f"cites {item.expected_document}"
        print(f"  {item.key:<20} {item.agent:<9} {truth}")
    return 0


def _show(items: list[EvalItem], case_id: str) -> int:
    shown = [item for item in items if item.case_id == case_id]
    if not shown:
        raise SystemExit(f"no such case: {case_id}")
    for item in shown:
        print(f"=== {item.key} -> the {item.agent} agent")
        print(f"--- query\n{item.query}")
        print(f"--- context\n{item.context}\n")
    return 0


def _dry_run(items: list[EvalItem], settings: LLMSettings, mode: str) -> int:
    """Every request built and sized, nothing sent. Retrieval is lexical-only here, so
    not even Voyage is called; a live recall's documents may differ in order."""
    client = DeferredClient(settings)
    retriever = CorpusSearcher(None) if any(i.task is Task.RECALL for i in items) else None
    from aioc.agents import RetryLog

    sized: list[tuple[str, int]] = []
    for item in items:
        before = set(client.pending)
        try:
            run_item(item, client, retriever, usage=Usage(), retry_log=RetryLog())
        except PendingRequest:
            pass
        except Exception as exc:  # noqa: BLE001 - say which item cannot be built, carry on
            print(f"  {item.key:<20} cannot be built: {type(exc).__name__}: {exc}")
            continue
        (key,) = set(client.pending) - before
        body = json.dumps(client.pending[key], default=str)
        sized.append((item.key, len(body) // 4))

    print(f"{len(sized)} request(s), model {settings.model}, mode {mode}")
    for key, tokens in sized:
        print(f"  {key:<20} ~{tokens:>6,} input tokens")
    total_in = sum(tokens for _, tokens in sized)
    total_out = _ASSUMED_OUTPUT_TOKENS * len(sized)
    print(f"\n  ~{total_in:,} input tokens (chars / 4 - an estimate, not a count)")
    print(f"  ~{total_out:,} output tokens assumed ({_ASSUMED_OUTPUT_TOKENS:,} per report)")
    flat = price(settings.model, Usage(input_tokens=total_in, output_tokens=total_out))
    if flat is None:
        print(f"  {settings.model} has no listed price")
        return 0
    print(f"  projected, realtime and uncached: ${flat:.2f}")
    print(f"  projected, batch and uncached:    ${flat * BATCH_DISCOUNT:.2f}")
    print("  caching lowers both; by how much is what a live run measures")
    return 0


def _recorded_tools(results: Path) -> int:
    """Tool success over every recorded `respond()` response - the agents that drive
    real tools (GitHub, Deployment) are in those, not in the seeded set."""
    paths = sorted(results.glob("runs/*/*/response.json"))
    responses: list[AgentResponse] = []
    by_agent: dict[str, list[AgentResponse]] = {}
    for path in paths:
        try:
            recorded = CoordinatorResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        for response in recorded.agent_responses:
            responses.append(response)
            by_agent.setdefault(response.agent.value, []).append(response)
    if not responses:
        print(f"no recorded responses under {results}")
        return 0
    print(f"Tool success over {len(responses)} recorded agent report(s) in {len(paths)} run(s)")
    for agent, reports in sorted(by_agent.items()):
        rate = tool_success(reports)
        failed = Counter(
            call.error_class.value
            for report in reports
            for call in report.tool_calls
            if not call.ok and call.error_class is not None
        )
        classes = f"  failed by class: {dict(failed)}" if failed else ""
        print(f"  {agent:<11} {rate.render()}{classes}")
    print(f"  {'all':<11} {tool_success(responses).render()}")
    return 0


def _rescore(run_dir: Path, cases: EvalSet, write: Path | None) -> int:
    """A recorded run scored again with today's rules. Free: the responses are on disk."""
    record = json.loads((run_dir / "eval.json").read_text(encoding="utf-8"))
    kept = json.loads((run_dir / "responses.json").read_text(encoding="utf-8"))
    if record["set"]["sha256"] != cases.sha256:
        print(
            f"note: the run was made on set {record['set']['sha256'][:12]} and the case file "
            f"is now {cases.sha256[:12]}; items are matched by key",
            file=sys.stderr,
        )
    items = {item.key: item for item in cases.items}
    old = {entry["key"]: entry for entry in record["items"]}
    run = EvalRun(
        mode=record["run"]["mode"],
        model=record["run"]["model"],
        prompt_caching=record["run"]["prompt_caching"],
        cache_ttl=record["run"]["cache_ttl"],
        seconds=record["run"]["seconds"],
        retrieval_degraded=record["run"]["retrieval_degraded"],
    )
    for key, entry in old.items():
        item = items.get(key)
        if item is None:
            print(f"note: {key} is no longer in the case file; skipped", file=sys.stderr)
            continue
        saved = kept.get(key) or {}
        response: AgentResponse | None = None
        retrieved = None if saved.get("retrieved") is None else tuple(saved["retrieved"])
        if saved.get("response") is None:
            score = score_failure(item, RuntimeError(entry["error"] or "no response recorded"))
            score = _with(score, error=entry["error"])
        else:
            model = IncidentAgentResponse if item.task is Task.DIAGNOSE else DocsAgentResponse
            response = model.model_validate(saved["response"])
            score = score_item(item, response, retrieved=retrieved)
        usage = entry["usage"]
        score = _with(
            score,
            usage=Usage(
                input_tokens=usage["in"],
                output_tokens=usage["out"],
                cache_read_tokens=usage.get("cache_read"),
                cache_write_tokens=usage.get("cache_write"),
            ),
            retry=RetryStats(
                attempts=entry["retry"]["attempts"],
                rejections=tuple(entry["retry"]["rejections"]),
                outcome=entry["retry"]["outcome"],
            ),
            seconds=entry["seconds"],
        )
        run.results.append(ItemResult(item, score, response, retrieved))
    report = render_markdown(run, cases, heading=f"Eval run, rescored: {run_dir.name}")
    print(report)
    if write is not None:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(report, encoding="utf-8")
        print(f"written: {write}")
    return 0


def _with(score: Any, **changes: Any) -> Any:
    from dataclasses import replace

    return replace(score, **changes)


# ------------------------------------------------------------------------------- live path


def _responses(run: EvalRun) -> dict[str, Any]:
    return {
        result.item.key: {
            "response": (
                None if result.response is None else result.response.model_dump(mode="json")
            ),
            "retrieved": None if result.retrieved is None else list(result.retrieved),
        }
        for result in run.results
    }


def _live(items: list[EvalItem], cases: EvalSet, args: argparse.Namespace) -> int:
    settings = _settings(args)
    if settings.anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2
    if price_key(settings.model) is None:
        print(f"note: {settings.model} has no listed price; known: {sorted(PRICES)}")

    needs_corpus = any(item.task is Task.RECALL for item in items)
    retriever = CorpusSearcher(default_embedder()) if needs_corpus else None
    caching = f"on, {settings.prompt_cache_ttl}" if settings.prompt_caching else "off"
    print(
        f"{len(items)} item(s) from {cases.name} v{cases.version}, model {settings.model}, "
        f"mode {args.mode}, prompt caching {caching}"
    )

    with RunRecorder(
        kind="llm",
        name=f"evals-{args.mode}",
        command="run_evals.py " + " ".join(sys.argv[1:]),
        metadata={
            "set": cases.name,
            "set_version": cases.version,
            "set_sha256": cases.sha256,
            "items": len(items),
            "mode": args.mode,
            "model": settings.model,
            "prompt_caching": settings.prompt_caching,
            "cache_ttl": settings.prompt_cache_ttl,
        },
    ) as recorder:
        if args.mode == "batch":
            batcher = MessageBatcher.from_settings(
                settings,
                poll_seconds=args.poll_seconds,
                on_poll=lambda batch_id, status, elapsed: print(
                    f"    {batch_id} {status} after {elapsed:.0f}s"
                ),
            )
            run = run_batch(items, DeferredClient(settings), batcher, retriever, progress=print)
        else:
            run = run_realtime(items, LLMClient(settings), retriever, progress=print)

        for result in run.results:
            score = result.score
            recorder.event(
                score.key,
                # An eval item is a measurement: a wrong answer is recorded as `failed`
                # so the index can be queried, and the run itself still completed.
                outcome="error" if not score.answered else "passed" if score.correct else "failed",
                duration_ms=None if score.seconds is None else score.seconds * 1000,
                type="llm_call",
                message=score.error,
                data={
                    **score.to_dict(),
                    "model": settings.model,
                    "batch": run.mode == "batch",
                    "cache_ttl": settings.prompt_cache_ttl,
                },
            )
        report = render_markdown(run, cases, heading=f"Eval run: {recorder.run_id}")
        recorder.artifact("report.md", report)
        recorder.artifact("eval.json", json.dumps(to_record(run, cases), indent=2))
        recorder.artifact("responses.json", json.dumps(_responses(run), indent=2))
        # The run completed; its scores are measurements, not the run's verdict.
        recorder.set_outcome("passed" if any(s.answered for s in run.scores) else "error")

    print()
    print(report)
    print(f"records: {recorder.dir}")
    if args.write is not None:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(report, encoding="utf-8")
        print(f"written: {args.write}")
    return 0 if any(score.answered for score in run.scores) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--set", type=Path, default=None, help="a case file (default: seeded)")
    parser.add_argument("--tasks", default=None, help="diagnose, recall, or both comma-separated")
    parser.add_argument("--cases", default=None, help="case ids, comma-separated")
    parser.add_argument("--limit", type=int, default=None, help="only the first N items")
    parser.add_argument("--mode", choices=("realtime", "batch"), default="realtime")
    parser.add_argument("--no-cache", action="store_true", help="prompt caching off")
    parser.add_argument("--cache-ttl", choices=("5m", "1h"), default=None)
    parser.add_argument("--model", default=None, help="override AIOC_MODEL for this run")
    parser.add_argument("--poll-seconds", type=float, default=20.0, help="batch poll interval")
    parser.add_argument("--write", type=Path, default=None, help="also write the report here")
    free = parser.add_mutually_exclusive_group()
    free.add_argument("--list", action="store_true", help="free: list the items")
    free.add_argument("--show", metavar="CASE", default=None, help="free: print one case")
    free.add_argument("--dry-run", action="store_true", help="free: build and size requests")
    free.add_argument("--rescore", type=Path, default=None, help="free: a recorded run directory")
    free.add_argument("--recorded-tools", action="store_true", help="free: recorded tool success")
    parser.add_argument("--results", type=Path, default=RESULTS_ROOT, help="test-results directory")
    args = parser.parse_args(argv)

    if args.recorded_tools:
        return _recorded_tools(args.results)
    cases = load_cases(args.set)
    if args.rescore is not None:
        return _rescore(args.rescore, cases, args.write)
    items = _select(cases, args)
    if not items:
        raise SystemExit("the selection is empty")
    if args.list:
        return _list(cases, items)
    if args.show is not None:
        return _show(list(cases.items), args.show)
    if args.dry_run:
        return _dry_run(items, _settings(args), args.mode)
    try:
        return _live(items, cases, args)
    except EvalAborted as exc:
        # Recorded by the run's own recorder on the way out; said here for the terminal.
        print(f"\nABORTED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
