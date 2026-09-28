"""Day 18: field-level confidence and Docs provenance over the recorded live responses.

    uv run python scripts/confidence_report.py                      # every recorded response
    uv run python scripts/confidence_report.py --run test-results/runs/<date>/<run-dir>
    uv run python scripts/confidence_report.py --json

Free: no API calls, no network. Every `respond()`-shaped live check records its
`CoordinatorResponse` as `response.json`; this reads those and prints, per request, every
judgement with its band and its flags (`aioc.coordinator.confidence`) and, for each Docs
report, the claim -> source chain and the coverage gaps (`aioc.coordinator.provenance`).
The totals at the end are the calibration floor the Day 19 eval harness starts from: how
the agents' stated confidence is distributed, and how often a stated band promised more
evidence than the field cites.

A record that no longer validates as a `CoordinatorResponse` is reported and skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aioc.contracts import CoordinatorResponse, DocsAgentResponse
from aioc.coordinator import BANDS, DocsProvenance, RequestProfile, provenance, request_profile

_RESULTS = Path(__file__).resolve().parents[1] / "test-results"


def _responses(run: Path | None, results: Path) -> list[Path]:
    if run is not None:
        return [run / "response.json" if run.is_dir() else run]
    return sorted(results.glob("runs/*/*/response.json"))


def _docs_reports(response: CoordinatorResponse) -> list[DocsProvenance]:
    return [provenance(r) for r in response.agent_responses if isinstance(r, DocsAgentResponse)]


def _print(path: Path, rp: RequestProfile, docs: list[DocsProvenance]) -> None:
    print(f"\n== {path.parent.name}")
    for line in rp.render():
        print(f"   {line}")
    for p in docs:
        for line in p.render():
            print(f"   {line}")


def _totals(profiles: list[RequestProfile], docs: list[DocsProvenance]) -> dict[str, Any]:
    fields = [f for rp in profiles for f in rp.fields]
    by_band = Counter(f.band.value for f in fields)
    flags = Counter(x.value for f in fields for x in f.flags)
    flags.update(x.value for rp in profiles for p in rp.agents for x in p.flags)
    sub_questions = sum(len(p.coverage.sub_questions) for p in docs)
    answered = sum(len(p.coverage.answered) for p in docs)
    return {
        "responses": len(profiles),
        "judgements": len(fields),
        "nulls": sum(1 for f in fields if f.null),
        "by_band": {b.value: by_band.get(b.value, 0) for _, b, _ in BANDS},
        "flags": dict(flags),
        "docs_reports": len(docs),
        "sub_questions": sub_questions,
        "answered": answered,
        "claims": sum(len(p.claims) for p in docs),
        "supported_claims": sum(len(p.supported) for p in docs),
        "claims_with_evidence_chain": sum(
            1 for p in docs for c in p.claims if any(s.evidence_ids for s in c.sources)
        ),
        "unanswered_without_gap": sum(
            1 for p in docs for g in p.coverage.unanswered if g.gap_id is None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, help="one run directory or response.json")
    parser.add_argument("--results", type=Path, default=_RESULTS, help="the test-results root")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    profiles: list[RequestProfile] = []
    docs_all: list[DocsProvenance] = []
    runs: list[dict[str, Any]] = []
    skipped: list[str] = []
    for path in _responses(args.run, args.results):
        try:
            response = CoordinatorResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as exc:
            skipped.append(
                f"{path.parent.name}: not a CoordinatorResponse ({exc.error_count()} errors)"
            )
            continue
        rp = request_profile(response)
        docs = _docs_reports(response)
        profiles.append(rp)
        docs_all.extend(docs)
        runs.append(
            {"run": path.parent.name, "profile": rp.to_dict(), "docs": [d.to_dict() for d in docs]}
        )
        if not args.json:
            _print(path, rp, docs)

    totals = _totals(profiles, docs_all)
    if args.json:
        print(json.dumps({"runs": runs, "totals": totals, "skipped": skipped}, indent=2))
        return 0
    bands = ", ".join(f"{n} {b}" for b, n in totals["by_band"].items() if n)
    print(
        f"\n{totals['responses']} response(s), {totals['judgements']} judgement(s) "
        f"({totals['nulls']} null): {bands or 'none'}"
    )
    if totals["flags"]:
        print("flags: " + ", ".join(f"{n} {name}" for name, n in sorted(totals["flags"].items())))
    else:
        print("flags: none")
    if totals["docs_reports"]:
        print(
            f"coverage: {totals['answered']}/{totals['sub_questions']} sub-question(s) answered "
            f"across {totals['docs_reports']} docs report(s); {totals['supported_claims']}/"
            f"{totals['claims']} claim(s) supported, {totals['claims_with_evidence_chain']} "
            f"traced to an evidence entry; {totals['unanswered_without_gap']} unanswered "
            "without a gap"
        )
    for line in skipped:
        print(f"skipped {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
