"""Day 20: put recorded eval runs side by side, and read a later run against the result.

    uv run python scripts/eval_baseline.py <run-dir> [<run-dir> ...]
    uv run python scripts/eval_baseline.py --latest
    uv run python scripts/eval_baseline.py --latest --write evaluations/baseline.md
    uv run python scripts/eval_baseline.py --against evaluations/baseline.json <run-dir>

Free: no API calls, no network. It reads the `eval.json` that `scripts/run_evals.py` leaves
in every run directory.

**A baseline is several runs of one set.** One run for each configuration of the two cost
levers - realtime or batch, cached or not - on the same eval set, the same model, and the
same items. Runs that differ in any of those are refused rather than averaged over, because
the number that comes out would be about the difference between the runs and would be read
as a fact about the system.

`--latest` finds the newest complete run of each configuration on the eval set as it is now:
the whole set, at least one item answered, the same model as the newest of them. A run that
was aborted, or made on a selection, or made before the case file last changed, is not a
candidate.

`--write PATH.md` also writes `PATH.json` beside it. The Markdown is what a person reads;
the JSON is what `--against` compares a later run with, which is how Day 24's token
reduction becomes a number: the same set, the same configuration, before and after.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runlog import RESULTS_ROOT  # noqa: E402 - needs the sys.path insert above

from aioc.evals import (  # noqa: E402
    BaselineError,
    EvalSet,
    build_baseline,
    compare,
    load_cases,
    render_baseline,
    render_comparison,
    run_label,
)


def read_record(run_dir: Path) -> dict[str, Any]:
    """A run directory's `eval.json`, with the directory's name as its id when the record
    predates the id being written into it."""
    path = run_dir / "eval.json" if run_dir.is_dir() else run_dir
    if not path.is_file():
        raise SystemExit(f"{path} does not exist - is {run_dir} an eval run directory?")
    record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if not record["run"].get("run_id"):
        record["run"]["run_id"] = path.parent.name
    return record


def _started(run_dir: Path) -> str:
    """When the run began, to the millisecond (`run.json`); the directory's name, which
    is stamped to the second, when the summary is missing."""
    summary = run_dir / "run.json"
    if summary.is_file():
        try:
            return str(json.loads(summary.read_text(encoding="utf-8"))["started_at"])
        except (ValueError, KeyError):
            pass
    return run_dir.name


def latest(results: Path, cases: EvalSet) -> list[dict[str, Any]]:
    """The newest complete run of each configuration on ``cases``, newest model wins."""
    candidates: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(results.glob("runs/*/*/eval.json")):
        try:
            record = read_record(path.parent)
        except (ValueError, KeyError):
            continue
        complete = (
            record["set"]["sha256"] == cases.sha256
            and record["summary"]["items"] == len(cases.items)
            and record["summary"]["answered"]["hits"] > 0
        )
        if complete:
            candidates.append((_started(path.parent), record))
    if not candidates:
        return []
    candidates.sort(key=lambda entry: entry[0])
    model = candidates[-1][1]["run"]["model"]
    newest: dict[str, dict[str, Any]] = {}
    for _name, record in candidates:
        if record["run"]["model"] == model:
            newest[run_label(record)] = record
    return list(newest.values())


def _write(path: Path, markdown: str, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"written: {path} and {path.with_suffix('.json')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", nargs="*", type=Path, help="eval run directories")
    parser.add_argument("--latest", action="store_true", help="the newest run of each kind")
    parser.add_argument("--against", type=Path, default=None, help="a baseline.json to compare")
    parser.add_argument("--write", type=Path, default=None, help="write PATH.md and PATH.json")
    parser.add_argument("--heading", default=None, help="the report's title")
    parser.add_argument("--set", type=Path, default=None, help="a case file (default: seeded)")
    parser.add_argument("--results", type=Path, default=RESULTS_ROOT, help="test-results directory")
    args = parser.parse_args(argv)

    try:
        if args.against is not None:
            if len(args.runs) != 1:
                raise SystemExit("--against compares exactly one run directory")
            baseline = json.loads(args.against.read_text(encoding="utf-8"))
            comparison = compare(baseline, read_record(args.runs[0]))
            report = render_comparison(comparison, heading=args.heading or "Against the baseline")
            print(report)
            if args.write is not None:
                _write(args.write, report, comparison)
            return 0

        if args.latest:
            if args.runs:
                raise SystemExit("--latest chooses the runs; do not name any as well")
            records = latest(args.results, load_cases(args.set))
            if not records:
                print(
                    f"no complete eval run on the current set under {args.results} - "
                    "run scripts/run_evals.py on the whole set first",
                    file=sys.stderr,
                )
                return 1
        else:
            if not args.runs:
                raise SystemExit("name at least one run directory, or pass --latest")
            records = [read_record(run) for run in args.runs]

        baseline = build_baseline(records)
    except BaselineError as exc:
        print(f"cannot compare these runs: {exc}", file=sys.stderr)
        return 1

    report = render_baseline(baseline, heading=args.heading or "Eval baseline")
    print(report)
    if args.write is not None:
        _write(args.write, report, baseline)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
