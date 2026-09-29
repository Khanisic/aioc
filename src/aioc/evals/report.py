"""The eval report (Day 19): what a run scored, and what it cost to find out.

Two renderings of one `EvalRun`: `to_record` is the machine form (kept with the run, and
what a later run is compared against) and `render_markdown` is the form a person reads
and `evaluations/` commits. Neither format is frozen.

The cost section prices the run three ways from the same measured tokens - as it was run,
without the cache, and without the batch discount - because the deltas are the point of
Day 19's second half and a saving is only a number if its baseline is on the same page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aioc.llm import price, price_uncached

from .cases import EvalSet, Task
from .runner import EvalRun
from .scoring import ItemScore, Rate, Summary, summarise


@dataclass(frozen=True, slots=True)
class CostView:
    """One run's tokens, priced. Every figure is ``None`` for a model with no listed price."""

    as_run: float | None
    without_cache: float | None  # same mode, every prompt token at the input rate
    realtime_uncached: float | None  # the Day 15 way: no cache, no batch
    per_item: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_run_usd": _round(self.as_run),
            "without_cache_usd": _round(self.without_cache),
            "realtime_uncached_usd": _round(self.realtime_uncached),
            "per_item_usd": _round(self.per_item),
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def cost_view(run: EvalRun, summary: Summary) -> CostView:
    batch = run.mode == "batch"
    as_run = price(run.model, summary.usage, batch=batch, cache_ttl=run.cache_ttl)
    return CostView(
        as_run=as_run,
        without_cache=price_uncached(run.model, summary.usage, batch=batch),
        realtime_uncached=price_uncached(run.model, summary.usage, batch=False),
        per_item=None if as_run is None or not summary.items else as_run / summary.items,
    )


def to_record(run: EvalRun, cases: EvalSet) -> dict[str, Any]:
    summary = summarise(run.scores)
    return {
        "set": {"name": cases.name, "version": cases.version, "sha256": cases.sha256},
        "run": {
            "mode": run.mode,
            "model": run.model,
            "prompt_caching": run.prompt_caching,
            "cache_ttl": run.cache_ttl,
            "seconds": round(run.seconds, 1),
            "batches": [
                {
                    "batch_id": b.batch_id,
                    "requests": b.requests,
                    "succeeded": b.succeeded,
                    "failed": b.failed,
                    "seconds": round(b.seconds, 1),
                }
                for b in run.batches
            ],
            "retrieval_degraded": list(run.retrieval_degraded),
        },
        "summary": summary.to_dict(),
        "cost": cost_view(run, summary).to_dict(),
        "items": [score.to_dict() for score in run.scores],
    }


# ---------------------------------------------------------------------------- markdown


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _usd(value: float | None) -> str:
    return "unpriced" if value is None else f"${value:.4f}"


def _saving(base: float | None, actual: float | None) -> str:
    """The change against the baseline, signed: ``-50%`` is half the price."""
    if base is None or actual is None or not base:
        return "n/a"
    return f"{(actual - base) / base:+.0%}"


def _row(label: str, rate: Rate) -> str:
    return f"| {label} | {rate.render()} |"


def _answer(score: ItemScore) -> tuple[str, str, str]:
    """(truth, answer, confidence) for the items table."""
    if not score.answered:
        return "-", "no response", "-"
    if score.task is Task.DIAGNOSE:
        mode = next(j for j in score.judgements if j.field == "failure_mode")
        return mode.expected, mode.actual or "abstained", f"{mode.confidence:.2f}"
    cited = ", ".join(score.cited_documents) or "nothing cited"
    confidence = "-" if score.overall_confidence is None else f"{score.overall_confidence:.2f}"
    if score.incident_id is None:
        return "no precedent", cited, confidence
    return "doc_" + score.incident_id.removeprefix("inc_"), cited, confidence


def _mark(score: ItemScore) -> str:
    if score.correct is None:
        return "failed"
    return "yes" if score.correct else "no"


def render_markdown(run: EvalRun, cases: EvalSet, *, heading: str | None = None) -> str:
    """The report, one sentence per line where it is prose (the house style for long
    Markdown) and tables everywhere a number is compared with another."""
    summary = summarise(run.scores)
    cost = cost_view(run, summary)
    usage = summary.usage
    caching = f"on ({run.cache_ttl} TTL)" if run.prompt_caching else "off"
    lines: list[str] = [
        f"# {heading or 'Eval run'}",
        "",
        f"- Set: `{cases.name}` v{cases.version}, {summary.items} items "
        f"(sha256 `{cases.sha256[:12]}`).",
        f"- Model: `{run.model}`.",
        f"- Mode: {run.mode}, prompt caching {caching}.",
        f"- Wall clock: {run.seconds:.0f}s.",
        f"- Answered: {summary.answered.render()}.",
        "",
        "## Accuracy",
        "",
        "| Measure | Result |",
        "|---|---|",
        _row("Failure mode matches the recorded truth", summary.failure_mode),
        f"| Failure mode abstentions (null value, counted as not correct) "
        f"| {summary.failure_mode_abstained} |",
        _row("Severity matches exactly", summary.severity),
        _row("Severity within one level", summary.severity_within_one),
        f"| Affected services, mean precision | {_pct(summary.services_precision)} |",
        f"| Affected services, mean recall | {_pct(summary.services_recall)} |",
        _row("Recall cites the incident's own post-mortem", summary.recall_cited),
        _row("Retrieval returned that post-mortem", summary.recall_retrieved),
        _row("No-precedent probes answered with no answer", summary.probes_abstained),
        "",
        "### Failure mode, by recorded truth",
        "",
        "| Truth | Correct |",
        "|---|---|",
    ]
    lines.extend(
        f"| `{mode}` | {rate.render()} |" for mode, rate in summary.failure_mode_by_mode.items()
    )
    lines.extend(
        [
            "",
            "## Hallucination",
            "",
            "| Measure | Result |",
            "|---|---|",
            _row(
                "Diagnosis statements with nothing behind them in the context", summary.ungrounded
            ),
            _row("Diagnoses carrying at least one", summary.items_with_ungrounded),
            _row("Probes answered with an invented precedent", summary.invented_precedents),
            _row("Reports the agents' own grounding rule refused", summary.grounding_refusals),
            "",
            "## Tool success",
            "",
            "| Measure | Result |",
            "|---|---|",
            _row("Tool calls that returned ok", summary.tool_success),
            "",
            "## Calibration",
            "",
            "Judgements that stated a value, in the contract band their confidence names.",
            "",
            "| Band | From | Judgements | Correct | Accuracy | Mean confidence |",
            "|---|---|---|---|---|---|",
        ]
    )
    for row in summary.calibration:
        mean = "n/a" if row.mean_confidence is None else f"{row.mean_confidence:.2f}"
        lines.append(
            f"| `{row.band.value}` | {row.lower:.2f} | {row.judgements} | {row.correct} "
            f"| {_pct(row.accuracy)} | {mean} |"
        )
    retries = summary.retries
    lines.extend(
        [
            "",
            "## Validation retries",
            "",
            "| Measure | Count |",
            "|---|---|",
            f"| Reports accepted first try | {retries['accepted_first_try']} |",
            f"| Recovered by a retry | {retries['recovered']} |",
            f"| Exhausted | {retries['exhausted']} |",
            f"| Retry calls made | {retries['retry_calls']} |",
            "",
            "## Cost",
            "",
            "| Tokens | Count |",
            "|---|---|",
            f"| Input, all of it | {usage.input_tokens:,} |",
            f"| of which read from the cache | {(usage.cache_read_tokens or 0):,} |",
            f"| of which written to the cache | {(usage.cache_write_tokens or 0):,} |",
            f"| of which billed at the input rate | {usage.uncached_input_tokens:,} |",
            f"| Output | {usage.output_tokens:,} |",
            "",
            "| The same tokens, priced | USD | Against realtime, uncached |",
            "|---|---|---|",
            f"| Realtime, no cache | {_usd(cost.realtime_uncached)} | baseline |",
            f"| This mode, no cache | {_usd(cost.without_cache)} "
            f"| {_saving(cost.realtime_uncached, cost.without_cache)} |",
            f"| As run | {_usd(cost.as_run)} | {_saving(cost.realtime_uncached, cost.as_run)} |",
            f"| As run, per item | {_usd(cost.per_item)} | |",
        ]
    )
    if run.batches:
        lines.extend(
            ["", "| Batch | Requests | Succeeded | Failed | Seconds |", "|---|---|---|---|---|"]
        )
        lines.extend(
            f"| `{b.batch_id}` | {b.requests} | {b.succeeded} | {b.failed} | {b.seconds:.0f} |"
            for b in run.batches
        )
    if run.retrieval_degraded:
        lines.extend(["", "Retrieval ran degraded:"])
        lines.extend(f"- {reason}" for reason in run.retrieval_degraded)
    lines.extend(
        [
            "",
            "## Items",
            "",
            "| Item | Truth | Answer | Confidence | Correct | Ungrounded | Attempts "
            "| Input | Output |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for score in run.scores:
        truth, answer, confidence = _answer(score)
        lines.append(
            f"| `{score.key}` | {truth} | {answer} | {confidence} | {_mark(score)} "
            f"| {len(score.grounding.ungrounded)} | {score.retry.attempts} "
            f"| {score.usage.input_tokens:,} | {score.usage.output_tokens:,} |"
        )
    detail = [s for s in run.scores if s.error or s.grounding.ungrounded]
    if detail:
        lines.extend(["", "## What was refused or ungrounded", ""])
        for score in detail:
            if score.error:
                lines.append(f"- `{score.key}` produced no response: {score.error}")
            lines.extend(f"- `{score.key}`: {what}" for what in score.grounding.ungrounded)
    return "\n".join(lines) + "\n"
