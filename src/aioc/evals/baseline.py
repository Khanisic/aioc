"""The baseline (Day 20): several runs of one eval set, side by side, and kept.

A baseline answers two questions, and they need different arithmetic.

**What did each cost lever save?** Two runs never produce the same output tokens, so
comparing one run's bill with another's measures the lever plus the noise. Each run is
therefore also priced against *its own tokens* at the realtime, uncached rate
(`same_tokens`): that delta is the lever and nothing else. The run-against-run delta is
reported beside it (`measured`), because that is what was actually paid.

**Did the lever change the answers?** It should not: caching and batching change how a
request is billed, not what it says. `agreement` lists every item the runs scored
differently. A few is model noise; many means the modes are not sending the same request,
which is a bug and not a finding.

Runs are comparable only on the same set, the same model, and the same items, and
`build_baseline` refuses anything else rather than averaging over it. The same rule holds
for `compare`, which is how a later run (Day 24's, after the context is trimmed) is read
against this one: same set, same configuration, and then the token reduction is a number.

Everything here reads the records `aioc.evals.report.to_record` writes. No model call.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

# (mode, prompt_caching) in the order a reader wants them: the reference first.
_ORDER = (("realtime", False), ("realtime", True), ("batch", False), ("batch", True))

REFERENCE = "realtime, uncached"

# The headline rates, as (key in `summary`, path, label).
_RATES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("answered", ("answered",), "Answered"),
    ("failure_mode", ("accuracy", "failure_mode"), "Failure mode correct"),
    ("severity", ("accuracy", "severity"), "Severity exact"),
    ("severity_within_one", ("accuracy", "severity_within_one"), "Severity within one level"),
    ("recall_cited", ("accuracy", "recall_cited"), "Recall cites the post-mortem"),
    ("recall_retrieved", ("accuracy", "recall_retrieved"), "Retrieval returned it"),
    ("probes_abstained", ("accuracy", "probes_abstained"), "Probes declined"),
    ("ungrounded", ("hallucination", "ungrounded"), "Ungrounded statements"),
    ("stitched", ("hallucination", "stitched"), "Evidence joined from verbatim lines"),
    (
        "invented_precedents",
        ("hallucination", "invented_precedents"),
        "Invented precedents",
    ),
    ("grounding_refusals", ("hallucination", "grounding_refusals"), "Grounding refusals"),
    ("tool_success", ("tool_success",), "Tool calls ok"),
)


class BaselineError(ValueError):
    """The runs cannot be compared, and the message says which rule they broke."""


def label(record: Mapping[str, Any]) -> str:
    """A run's configuration, as the two levers: `realtime, cached`, `batch, uncached`."""
    run = record["run"]
    return f"{run['mode']}, {'cached' if run['prompt_caching'] else 'uncached'}"


def _rate(record: Mapping[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    node: Any = record["summary"]
    for part in path:
        node = node[part]
    return dict(node)


def _change(base: float | None, actual: float | None) -> float | None:
    if base is None or actual is None or not base:
        return None
    return (actual - base) / base


def _run(record: Mapping[str, Any]) -> dict[str, Any]:
    usage, cost, run = record["summary"]["usage"], record["cost"], record["run"]
    return {
        "label": label(record),
        "run_id": run.get("run_id"),
        "mode": run["mode"],
        "prompt_caching": run["prompt_caching"],
        "cache_ttl": run["cache_ttl"],
        "seconds": run["seconds"],
        "batches": len(run["batches"]),
        "items": record["summary"]["items"],
        "rates": {name: _rate(record, path) for name, path, _ in _RATES},
        "failure_mode_abstained": record["summary"]["accuracy"]["failure_mode_abstained"],
        "retries": dict(record["summary"]["retries"]),
        "calibration": list(record["summary"]["calibration"]),
        "tokens": dict(usage),
        "cache": dict(record.get("cache") or {}),
        "usd": cost["as_run_usd"],
        "same_tokens": {
            "realtime_uncached_usd": cost["realtime_uncached_usd"],
            "change": _change(cost["realtime_uncached_usd"], cost["as_run_usd"]),
        },
    }


def _check_comparable(records: Sequence[Mapping[str, Any]]) -> None:
    if not records:
        raise BaselineError("a baseline needs at least one run")
    first = records[0]
    for record in records[1:]:
        if record["set"]["sha256"] != first["set"]["sha256"]:
            raise BaselineError(
                f"runs on different eval sets cannot share a baseline: "
                f"{first['set']['sha256'][:12]} and {record['set']['sha256'][:12]}"
            )
        if record["run"]["model"] != first["run"]["model"]:
            raise BaselineError(
                f"runs on different models cannot share a baseline: "
                f"{first['run']['model']} and {record['run']['model']}"
            )
        if _keys(record) != _keys(first):
            raise BaselineError(
                f"runs over different items cannot share a baseline: {label(first)} has "
                f"{len(_keys(first))} and {label(record)} has {len(_keys(record))}"
            )
    labels = [label(record) for record in records]
    repeated = sorted({name for name in labels if labels.count(name) > 1})
    if repeated:
        raise BaselineError(f"more than one run for the same configuration: {repeated}")


def _keys(record: Mapping[str, Any]) -> list[str]:
    return [item["key"] for item in record["items"]]


def build_baseline(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The runs, side by side. Refuses runs that are not comparable."""
    _check_comparable(records)
    ordered = sorted(
        records, key=lambda r: _ORDER.index((r["run"]["mode"], r["run"]["prompt_caching"]))
    )
    runs = [_run(record) for record in ordered]
    reference = next((run for run in runs if run["label"] == REFERENCE), None)
    for run in runs:
        run["measured"] = (
            None
            if reference is None
            else {
                "against": reference["label"],
                "reference_usd": reference["usd"],
                "change": _change(reference["usd"], run["usd"]),
            }
        )

    items: dict[str, dict[str, Any]] = {}
    for record in ordered:
        for item in record["items"]:
            items.setdefault(item["key"], {})[label(record)] = {
                "correct": item["correct"],
                "answered": item["answered"],
                "input": item["usage"]["in"],
                "output": item["usage"]["out"],
            }
    differing = {
        key: {name: entry["correct"] for name, entry in by_run.items()}
        for key, by_run in items.items()
        if len({entry["correct"] for entry in by_run.values()}) > 1
    }
    first = ordered[0]
    return {
        "set": dict(first["set"]),
        "model": first["run"]["model"],
        "reference": None if reference is None else reference["label"],
        "runs": runs,
        "agreement": {
            "items": len(items),
            "same": len(items) - len(differing),
            "differing": differing,
        },
        "items": items,
    }


# --------------------------------------------------------------- a later run, compared


def compare(baseline: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    """A later run against the baseline run of the same configuration.

    The point of a baseline: after the context is trimmed (Day 21) the same set is run
    again, and the token reduction is this function's ``tokens`` block. Refused unless the
    set and the model are the baseline's - a smaller prompt on a different question is
    not a reduction.
    """
    if record["set"]["sha256"] != baseline["set"]["sha256"]:
        raise BaselineError(
            f"the run is on eval set {record['set']['sha256'][:12]} and the baseline on "
            f"{baseline['set']['sha256'][:12]}; a changed set needs a new baseline"
        )
    if record["run"]["model"] != baseline["model"]:
        raise BaselineError(
            f"the run used {record['run']['model']} and the baseline {baseline['model']}"
        )
    wanted = label(record)
    base = next((run for run in baseline["runs"] if run["label"] == wanted), None)
    if base is None:
        have = [run["label"] for run in baseline["runs"]]
        raise BaselineError(f"the baseline has no `{wanted}` run to compare with; it has {have}")

    now = _run(record)
    rates = {}
    for name, _path, _label in _RATES:
        before, after = base["rates"][name], now["rates"][name]
        rates[name] = {
            "before": before,
            "after": after,
            "change": (
                None
                if before["rate"] is None or after["rate"] is None
                else after["rate"] - before["rate"]
            ),
        }
    tokens = {
        name: {
            "before": base["tokens"][name],
            "after": now["tokens"][name],
            "change": _change(base["tokens"][name], now["tokens"][name]),
        }
        for name in ("in", "out")
    }
    flipped = {}
    for item in record["items"]:
        earlier = baseline["items"].get(item["key"], {}).get(wanted)
        if earlier is not None and earlier["correct"] != item["correct"]:
            flipped[item["key"]] = {"before": earlier["correct"], "after": item["correct"]}
    return {
        "configuration": wanted,
        "baseline_run": base["run_id"],
        "run": now["run_id"],
        "rates": rates,
        "tokens": tokens,
        "usd": {
            "before": base["usd"],
            "after": now["usd"],
            "change": _change(base["usd"], now["usd"]),
        },
        "flipped": flipped,
    }


# ---------------------------------------------------------------------------- markdown


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _signed(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.0%}"


def _points(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.0f} pts"


def _usd(value: float | None) -> str:
    return "unpriced" if value is None else f"${value:.4f}"


def _cell(rate: Mapping[str, Any]) -> str:
    if not rate["total"]:
        return "n/a (0)"
    return f"{rate['hits']}/{rate['total']} = {rate['rate']:.0%}"


def _mark(correct: bool | None) -> str:
    return {True: "yes", False: "no", None: "failed"}[correct]


def render_baseline(baseline: Mapping[str, Any], *, heading: str = "Eval baseline") -> str:
    runs = baseline["runs"]
    names = [run["label"] for run in runs]
    head = "| | " + " | ".join(names) + " |"
    rule = "|---|" + "---|" * len(runs)
    lines = [
        f"# {heading}",
        "",
        f"- Set: `{baseline['set']['name']}` v{baseline['set']['version']}, "
        f"{baseline['agreement']['items']} items (sha256 `{baseline['set']['sha256'][:12]}`).",
        f"- Model: `{baseline['model']}`.",
        f"- Runs: {len(runs)}.",
    ]
    lines.extend(f"  - {run['label']}: `{run['run_id'] or 'unrecorded'}`" for run in runs)
    lines.extend(["", "## Scores", "", head, rule])
    for name, _path, text in _RATES:
        lines.append(f"| {text} | " + " | ".join(_cell(r["rates"][name]) for r in runs) + " |")
    lines.append(
        "| Failure mode abstentions | "
        + " | ".join(str(r["failure_mode_abstained"]) for r in runs)
        + " |"
    )
    lines.append(
        "| Retry calls | " + " | ".join(str(r["retries"]["retry_calls"]) for r in runs) + " |"
    )

    lines.extend(["", "## Cost", "", head, rule])
    rows: tuple[tuple[str, Callable[[Mapping[str, Any]], str]], ...] = (
        ("Input tokens", lambda r: f"{r['tokens']['in']:,}"),
        ("of which read from the cache", lambda r: f"{r['tokens']['cache_read'] or 0:,}"),
        ("of which written to the cache", lambda r: f"{r['tokens']['cache_write'] or 0:,}"),
        ("Output tokens", lambda r: f"{r['tokens']['out']:,}"),
        ("Paid", lambda r: _usd(r["usd"])),
        (
            "The same tokens, realtime and uncached",
            lambda r: _usd(r["same_tokens"]["realtime_uncached_usd"]),
        ),
        ("What the levers changed, same tokens", lambda r: _signed(r["same_tokens"]["change"])),
        (
            "Paid, against the reference run",
            lambda r: "n/a" if r["measured"] is None else _signed(r["measured"]["change"]),
        ),
        ("Wall clock", lambda r: f"{r['seconds']:.0f}s"),
        ("Batches", lambda r: str(r["batches"])),
    )
    for text, cell in rows:
        lines.append(f"| {text} | " + " | ".join(cell(run) for run in runs) + " |")
    lines.append("")
    if baseline["reference"] is None:
        lines.append(f"There is no `{REFERENCE}` run, so there is no reference to measure against.")
        lines.append("The same-tokens row is the only cost comparison this baseline can make.")
    else:
        lines.append(
            "The same-tokens row prices each run's own tokens both ways, so it is the lever "
            "and nothing else."
        )
        lines.append(
            "The reference row compares two different runs, so it carries the difference in "
            "what the model happened to write as well."
        )
    for run in runs:
        note = run["cache"].get("note")
        if note:
            lines.append(f"Cache, {run['label']}: {note}.")

    agreement = baseline["agreement"]
    lines.extend(
        [
            "",
            "## Agreement between the runs",
            "",
            f"{agreement['same']} of {agreement['items']} items were scored the same in every run.",
        ]
    )
    if agreement["differing"]:
        lines.append(
            "A lever changes how a request is billed and not what it says, so these are "
            "the model answering the same request differently."
        )
        lines.extend(["", "| Item | " + " | ".join(names) + " |", rule])
        for key, by_run in agreement["differing"].items():
            lines.append(
                f"| `{key}` | " + " | ".join(_mark(by_run.get(name)) for name in names) + " |"
            )

    lines.extend(["", "## Calibration", "", "| Band | " + " | ".join(names) + " |", rule])
    bands = [row["band"] for row in runs[0]["calibration"]]
    for index, band in enumerate(bands):
        cells = []
        for run in runs:
            row = run["calibration"][index]
            cells.append(
                "none"
                if not row["judgements"]
                else f"{row['correct']}/{row['judgements']} = {row['accuracy']:.0%}"
            )
        lines.append(f"| `{band}` | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def render_comparison(
    comparison: Mapping[str, Any], *, heading: str = "Against the baseline"
) -> str:
    lines = [
        f"# {heading}",
        "",
        f"- Configuration: {comparison['configuration']}.",
        f"- Baseline run: `{comparison['baseline_run'] or 'unrecorded'}`.",
        f"- This run: `{comparison['run'] or 'unrecorded'}`.",
        "",
        "## Tokens and cost",
        "",
        "| | Baseline | This run | Change |",
        "|---|---|---|---|",
    ]
    tokens = comparison["tokens"]
    lines.append(
        f"| Input tokens | {tokens['in']['before']:,} | {tokens['in']['after']:,} "
        f"| {_signed(tokens['in']['change'])} |"
    )
    lines.append(
        f"| Output tokens | {tokens['out']['before']:,} | {tokens['out']['after']:,} "
        f"| {_signed(tokens['out']['change'])} |"
    )
    usd = comparison["usd"]
    lines.append(
        f"| Paid | {_usd(usd['before'])} | {_usd(usd['after'])} | {_signed(usd['change'])} |"
    )
    lines.extend(["", "## Scores", "", "| | Baseline | This run | Change |", "|---|---|---|---|"])
    for name, _path, text in _RATES:
        entry = comparison["rates"][name]
        lines.append(
            f"| {text} | {_cell(entry['before'])} | {_cell(entry['after'])} "
            f"| {_points(entry['change'])} |"
        )
    flipped = comparison["flipped"]
    lines.extend(["", "## Items that changed", ""])
    if not flipped:
        lines.append("None: every item was scored as it was in the baseline.")
    else:
        lines.extend(["| Item | Baseline | This run |", "|---|---|---|"])
        lines.extend(
            f"| `{key}` | {_mark(entry['before'])} | {_mark(entry['after'])} |"
            for key, entry in flipped.items()
        )
    return "\n".join(lines) + "\n"
