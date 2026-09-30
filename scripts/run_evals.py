"""Day 19: run the eval set, score it against the seed's answer key, and price the run.

    uv run python scripts/run_evals.py --list                   # free - the set, item by item
    uv run python scripts/run_evals.py --show case_04           # free - what the agent is shown
    uv run python scripts/run_evals.py --dry-run                # free - every request, sized
    uv run python scripts/run_evals.py                          # LIVE, realtime: 38 calls
    uv run python scripts/run_evals.py --mode batch             # LIVE, Batch API: half price
    uv run python scripts/run_evals.py --no-cache               # LIVE, the uncached baseline
    uv run python scripts/run_evals.py --tasks diagnose --limit 4
    uv run python scripts/run_evals.py --smoke                  # LIVE, 4 calls: is it wired?
    uv run python scripts/run_evals.py --resume test-results/runs/<date>/<run>   # LIVE, the rest
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

**`--smoke` is the first thing to run after anything changes the wire** (a new key, a
prompt edit, an SDK upgrade): four diagnoses, about $0.12, and a verdict rather than a
report to read. It fails when an item got no response, and when prompt caching is on and
the later requests did not read the prefix the first one wrote - the failure that costs
money and breaks nothing.

**A stopped run is continued, not repeated.** Every item is written to `progress.jsonl`
in the run directory as it finishes. `--resume <run-dir>` starts a new run that takes from
that directory every item worth keeping - the answered ones, and the ones whose agent gave
up - and asks only for the rest, which includes anything the API itself failed. It refuses
a directory from a different set, model, or configuration. A run stops itself, and says
how to resume, when the key or the account is refused or three items in a row fail on the
API.

Every run is recorded under `test-results/` (`report.md`, `eval.json`, `responses.json`)
and priced by `scripts/cost_review.py`; `--write` also puts the report where it can be
committed. `scripts/eval_baseline.py` puts several runs side by side.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RESULTS_ROOT, RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.agents import RetryLog  # noqa: E402
from aioc.contracts import AgentResponse, CoordinatorResponse  # noqa: E402
from aioc.evals import (  # noqa: E402
    EvalAborted,
    EvalItem,
    EvalRun,
    EvalSet,
    ItemResult,
    ProgressFile,
    RunConfig,
    StoreError,
    Task,
    cache_health,
    load_cases,
    read_run,
    render_markdown,
    restore,
    run_batch,
    run_item,
    run_realtime,
    to_record,
    tool_success,
)
from aioc.llm import (  # noqa: E402
    BATCH_DISCOUNT,
    PRICES,
    BatchRun,
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

# The projection's two constants, both measured on the first nine live items
# (2026-09-29, claude-sonnet-5) after the first projection came out 40% low on input:
#
# - A request is mostly JSON Schema, which tokenises far denser than prose: 7.9k tokens
#   measured against 19k characters, so 2.4 characters a token rather than the usual 4.
# - A diagnosis is a long report and a recall is a short one: 2.2k-3.5k output tokens
#   against 1.0k-1.7k. One number for both was wrong in both directions.
#
# Still an estimate. The report's measured tokens are what a run cost.
_CHARS_PER_TOKEN = 2.4
_ASSUMED_OUTPUT_TOKENS = {Task.DIAGNOSE: 3_000, Task.RECALL: 1_400}


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


@dataclass(frozen=True, slots=True)
class Projection:
    """Every request built and sized, nothing sent."""

    sized: list[tuple[str, int, int]]  # (item key, estimated input, assumed output)
    unbuildable: list[tuple[str, str]]  # (item key, why)
    realtime_uncached_usd: float | None

    @property
    def input_tokens(self) -> int:
        return sum(tokens for _, tokens, _ in self.sized)

    @property
    def output_tokens(self) -> int:
        return sum(tokens for _, _, tokens in self.sized)

    @property
    def batch_uncached_usd(self) -> float | None:
        flat = self.realtime_uncached_usd
        return None if flat is None else flat * BATCH_DISCOUNT


def project(items: list[EvalItem], settings: LLMSettings) -> Projection:
    """Build every request without sending it. Retrieval is lexical-only here, so not
    even Voyage is called; a live recall's documents may differ in order."""
    client = DeferredClient(settings)
    retriever = CorpusSearcher(None) if any(i.task is Task.RECALL for i in items) else None
    sized: list[tuple[str, int, int]] = []
    unbuildable: list[tuple[str, str]] = []
    for item in items:
        before = set(client.pending)
        try:
            run_item(item, client, retriever, usage=Usage(), retry_log=RetryLog())
        except PendingRequest:
            pass
        except Exception as exc:  # noqa: BLE001 - say which item cannot be built, carry on
            unbuildable.append((item.key, f"{type(exc).__name__}: {exc}"))
            continue
        (key,) = set(client.pending) - before
        body = json.dumps(client.pending[key], default=str)
        sized.append(
            (item.key, round(len(body) / _CHARS_PER_TOKEN), _ASSUMED_OUTPUT_TOKENS[item.task])
        )
    total = Usage(
        input_tokens=sum(tokens for _, tokens, _ in sized),
        output_tokens=sum(tokens for _, _, tokens in sized),
    )
    return Projection(sized, unbuildable, price(settings.model, total))


def _dry_run(items: list[EvalItem], settings: LLMSettings, mode: str) -> int:
    projection = project(items, settings)
    for key, why in projection.unbuildable:
        print(f"  {key:<20} cannot be built: {why}")
    print(f"{len(projection.sized)} request(s), model {settings.model}, mode {mode}")
    for key, tokens, _ in projection.sized:
        print(f"  {key:<20} ~{tokens:>6,} input tokens")
    print(
        f"\n  ~{projection.input_tokens:,} input tokens "
        f"(characters / {_CHARS_PER_TOKEN} - an estimate, not a count)"
    )
    print(
        f"  ~{projection.output_tokens:,} output tokens assumed "
        f"({_ASSUMED_OUTPUT_TOKENS[Task.DIAGNOSE]:,} a diagnosis, "
        f"{_ASSUMED_OUTPUT_TOKENS[Task.RECALL]:,} a recall)"
    )
    if projection.realtime_uncached_usd is None:
        print(f"  {settings.model} has no listed price")
        return 0
    print(f"  projected, realtime and uncached: ${projection.realtime_uncached_usd:.2f}")
    print(f"  projected, batch and uncached:    ${projection.batch_uncached_usd:.2f}")
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


def _rescore(run_dir: Path, cases: EvalSet, write: Path | None, *, save: bool = False) -> int:
    """A recorded run scored again with today's rules. Free: the responses are on disk.

    ``save`` keeps the result beside the run as `eval.rescored.json` and
    `report.rescored.md`. The originals are left alone: they are the record of what
    was scored at the time, and the difference between the two is the rule that changed."""
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
        batches=[
            BatchRun(
                batch_id=b["batch_id"],
                requests=b["requests"],
                succeeded=b["succeeded"],
                failed=b["failed"],
                seconds=b["seconds"],
            )
            for b in record["run"]["batches"]
        ],
    )
    for key, entry in old.items():
        item = items.get(key)
        if item is None:
            print(f"note: {key} is no longer in the case file; skipped", file=sys.stderr)
            continue
        saved = kept.get(key) or {}
        run.results.append(restore(item, entry, saved.get("response"), saved.get("retrieved")))
    report = render_markdown(run, cases, heading=f"Eval run, rescored: {run_dir.name}")
    print(report)
    if save:
        rescored = to_record(run, cases, run_id=record["run"].get("run_id") or run_dir.name)
        (run_dir / "eval.rescored.json").write_text(
            json.dumps(rescored, indent=2), encoding="utf-8"
        )
        (run_dir / "report.rescored.md").write_text(report, encoding="utf-8")
        print(f"saved: {run_dir / 'eval.rescored.json'}")
    if write is not None:
        write.parent.mkdir(parents=True, exist_ok=True)
        write.write_text(report, encoding="utf-8")
        print(f"written: {write}")
    return 0


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


@dataclass(frozen=True, slots=True)
class Executed:
    """One live run, as it was recorded."""

    run: EvalRun
    record: dict[str, Any]
    report: str
    dir: Path
    run_id: str


def _resumed(
    resume_from: Path, cases: EvalSet, config: RunConfig, items: list[EvalItem]
) -> tuple[dict[str, ItemResult], set[str], list[str]]:
    """The items of ``resume_from`` a run with this configuration need not ask again, which
    of them that run already recorded the cost of, and the batches it submitted and never
    read back - whose answers were paid for and are waiting."""
    stored = read_run(resume_from, cases)
    differences = config.differs_from(stored.config)
    if differences:
        raise StoreError(
            f"{resume_from.name} is a different run and cannot be continued by this one - "
            + "; ".join(differences)
        )
    wanted = {item.key for item in items}
    done = {key: result for key, result in stored.reusable().items() if key in wanted}
    counted: set[str] = set()
    events = resume_from / "events.jsonl"
    if events.is_file():
        for line in events.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            # Only an item this run keeps, and only one whose tokens were recorded there:
            # the earlier run wrote an event for every item it scored, failures included.
            if str(event.get("name")) in done and (event.get("data") or {}).get("usage"):
                counted.add(str(event.get("name")))
    return done, counted, list(stored.batches) if not stored.complete else []


def _record_items(
    recorder: RunRecorder,
    results: list[ItemResult],
    settings: LLMSettings,
    *,
    batch: bool,
    reused_from: str | None = None,
    counted: set[str] | None = None,
) -> None:
    for result in results:
        score = result.score
        data: dict[str, Any] = {
            **score.to_dict(),
            "model": settings.model,
            "batch": batch,
            "cache_ttl": settings.prompt_cache_ttl,
        }
        if reused_from is not None and score.key in (counted or set()):
            # Taken from an earlier run that already recorded these tokens. Said here so
            # the cost review counts them once. ``counted`` is the kept items only: the
            # earlier run may have recorded the items it failed too, and those were run
            # here and paid for here.
            data["reused_from"] = reused_from
        recorder.event(
            score.key,
            # An eval item is a measurement: a wrong answer is recorded as `failed`
            # so the index can be queried, and the run itself still completed.
            outcome="error" if not score.answered else "passed" if score.correct else "failed",
            duration_ms=None if score.seconds is None else score.seconds * 1000,
            type="llm_call",
            message=score.error,
            data=data,
        )


def execute(
    items: list[EvalItem],
    cases: EvalSet,
    settings: LLMSettings,
    *,
    mode: str,
    poll_seconds: float = 20.0,
    command: str | None = None,
    resume_from: Path | None = None,
    say: Callable[[str], None] = print,
) -> Executed:
    """Run the items, record the run, return what was recorded. Spends money.

    Each item is written to `progress.jsonl` as it finishes. With ``resume_from`` the items
    that directory already finished are taken from it and not asked again. Raises
    `EvalAborted` when the environment stops the run; what it had finished is recorded,
    and the exception's note says how to continue.
    """
    config = RunConfig(
        set_sha256=cases.sha256,
        model=settings.model,
        mode=mode,
        prompt_caching=settings.prompt_caching,
        cache_ttl=settings.prompt_cache_ttl,
    )
    done: dict[str, ItemResult] = {}
    counted: set[str] = set()
    submitted: list[str] = []
    if resume_from is not None:
        done, counted, submitted = _resumed(resume_from, cases, config, items)

    needs_corpus = any(item.task is Task.RECALL for item in items if item.key not in done)
    retriever = CorpusSearcher(default_embedder()) if needs_corpus else None
    caching = f"on, {settings.prompt_cache_ttl}" if settings.prompt_caching else "off"
    say(
        f"{len(items)} item(s) from {cases.name} v{cases.version}, model {settings.model}, "
        f"mode {mode}, prompt caching {caching}"
    )
    if resume_from is not None:
        say(
            f"  continuing {resume_from.name}: {len(done)} item(s) kept, "
            f"{len(items) - len(done)} to run"
            + (f", {len(submitted)} submitted batch(es) to read back" if submitted else "")
        )
    reused_from = None if resume_from is None else resume_from.name
    with RunRecorder(
        kind="llm",
        name=f"evals-{mode}",
        command=command or "run_evals.py " + " ".join(sys.argv[1:]),
        metadata={
            "set": cases.name,
            "set_version": cases.version,
            "set_sha256": cases.sha256,
            "items": len(items),
            "mode": mode,
            "model": settings.model,
            "prompt_caching": settings.prompt_caching,
            "cache_ttl": settings.prompt_cache_ttl,
            "resumed_from": reused_from,
            "items_kept": len(done),
        },
    ) as recorder:
        progress = ProgressFile(recorder.dir, config)
        for kept in done.values():
            progress.write(kept)
        try:
            if mode == "batch":
                batcher = MessageBatcher.from_settings(
                    settings,
                    poll_seconds=poll_seconds,
                    on_poll=lambda batch_id, status, elapsed: say(
                        f"    {batch_id} {status} after {elapsed:.0f}s"
                    ),
                )
                run = run_batch(
                    items,
                    DeferredClient(settings),
                    batcher,
                    retriever,
                    progress=say,
                    done=done,
                    on_result=progress.write,
                    submitted=submitted,
                    on_submit=progress.batch,
                )
            else:
                run = run_realtime(
                    items,
                    LLMClient(settings),
                    retriever,
                    progress=say,
                    done=done,
                    on_result=progress.write,
                )
        except EvalAborted as exc:
            # What it finished was paid for: on the record, and there to be continued from.
            _record_items(
                recorder,
                exc.results,
                settings,
                batch=mode == "batch",
                reused_from=reused_from,
                counted=counted,
            )
            exc.add_note(f"to continue it: run_evals.py --resume {recorder.dir}")
            exc.add_note(str(recorder.dir))
            raise

        _record_items(
            recorder,
            run.results,
            settings,
            batch=run.mode == "batch",
            reused_from=reused_from,
            counted=counted,
        )
        report = render_markdown(run, cases, heading=f"Eval run: {recorder.run_id}")
        record = to_record(run, cases, run_id=recorder.run_id)
        recorder.artifact("report.md", report)
        recorder.artifact("eval.json", json.dumps(record, indent=2))
        recorder.artifact("responses.json", json.dumps(_responses(run), indent=2))
        # The run completed; its scores are measurements, not the run's verdict.
        recorder.set_outcome("passed" if any(s.answered for s in run.scores) else "error")
    return Executed(run, record, report, recorder.dir, recorder.run_id)


def smoke_complaints(run: EvalRun) -> list[str]:
    """What a smoke test holds against a run. Empty means the wire works."""
    complaints = [
        f"{score.key} got no response: {score.error}" for score in run.scores if not score.answered
    ]
    health = cache_health(run)
    if health.healthy is False:
        complaints.append(f"prompt caching is on and not working: {health.note}")
    return complaints


def _live(items: list[EvalItem], cases: EvalSet, args: argparse.Namespace) -> int:
    settings = _settings(args)
    if settings.anthropic_api_key is None:
        print("ANTHROPIC_API_KEY is not set (shell or .env).", file=sys.stderr)
        return 2
    if price_key(settings.model) is None:
        print(f"note: {settings.model} has no listed price; known: {sorted(PRICES)}")

    done = execute(
        items,
        cases,
        settings,
        mode=args.mode,
        poll_seconds=args.poll_seconds,
        resume_from=args.resume,
    )
    print()
    print(done.report)
    print(f"records: {done.dir}")
    if args.write is not None:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(done.report, encoding="utf-8")
        print(f"written: {args.write}")
    if args.smoke:
        complaints = smoke_complaints(done.run)
        for complaint in complaints:
            print(f"SMOKE FAIL: {complaint}", file=sys.stderr)
        if complaints:
            return 1
        print(f"SMOKE PASS: {len(done.run.scores)} item(s) answered; {cache_health(done.run).note}")
        return 0
    return 0 if any(score.answered for score in done.run.scores) else 1


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
    parser.add_argument(
        "--resume",
        type=Path,
        default=None,
        help="live: continue this stopped run, asking only for what it did not finish",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="live, 4 calls: the first four diagnoses, with a pass or fail verdict",
    )
    free = parser.add_mutually_exclusive_group()
    free.add_argument("--list", action="store_true", help="free: list the items")
    free.add_argument("--show", metavar="CASE", default=None, help="free: print one case")
    free.add_argument("--dry-run", action="store_true", help="free: build and size requests")
    free.add_argument("--rescore", type=Path, default=None, help="free: a recorded run directory")
    parser.add_argument(
        "--save",
        action="store_true",
        help="with --rescore: keep the result beside the run as eval.rescored.json",
    )
    free.add_argument("--recorded-tools", action="store_true", help="free: recorded tool success")
    parser.add_argument("--results", type=Path, default=RESULTS_ROOT, help="test-results directory")
    args = parser.parse_args(argv)

    if args.recorded_tools:
        return _recorded_tools(args.results)
    cases = load_cases(args.set)
    if args.smoke and not (args.tasks or args.cases or args.limit):
        # One agent, so one prefix: the second request onward must read what the first wrote.
        args.tasks, args.limit = "diagnose", 4
    if args.rescore is not None:
        return _rescore(args.rescore, cases, args.write, save=args.save)
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
        for note in getattr(exc, "__notes__", [])[:1]:
            print(f"  {note}", file=sys.stderr)
        return 2
    except StoreError as exc:
        print(f"cannot resume: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
