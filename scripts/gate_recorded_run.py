"""Day 16: put recorded live responses through the HITL approval gate.

    uv run python scripts/gate_recorded_run.py                      # every recorded response
    uv run python scripts/gate_recorded_run.py --run test-results/runs/<date>/<run-dir>
    uv run python scripts/gate_recorded_run.py --approver console --identity you@oncall
    uv run python scripts/gate_recorded_run.py --json

Free: no API calls, no network. Every `respond()`-shaped live check records its
`CoordinatorResponse` as `response.json`; this reads those and runs `aioc.hitl.HitlGate`
over each, so the gate is exercised against what the agents actually recommended in live
runs rather than against fixtures written to pass it. That is how the first classifier's
three misreadings were found (tests/test_hitl.py pins them verbatim).

The default approver is the gate's own default, `DenyAll`: the report shows what would be
withheld with no human wired in. `--approver console` asks at the terminal for each
request that needs a human - the approval flow as an operator would see it.

Every decision is written to the gate's audit log before it is returned (Day 17). Without
`--persist` that log is in memory and the run is a rehearsal; with it, the decisions go to
`hitl_audit_log` on the stack's Postgres (append-only at the database) and
`scripts/audit_log.py` reads them back.

A record that no longer validates as a `CoordinatorResponse` (the Day 5 checkpoint
predates the coordinator) is reported and skipped, not coerced.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aioc.contracts import CoordinatorResponse
from aioc.hitl import (
    Approver,
    AuditLog,
    ConsoleApprover,
    Decision,
    DenyAll,
    GateResult,
    HitlGate,
    MemoryAuditLog,
    default_audit_log,
)

_RESULTS = Path(__file__).resolve().parents[1] / "test-results"


def _responses(run: Path | None) -> list[Path]:
    if run is not None:
        path = run / "response.json" if run.is_dir() else run
        return [path]
    return sorted(_RESULTS.glob("runs/*/*/response.json"))


def _approver(kind: str, identity: str | None) -> Approver:
    if kind == "console":
        if not identity:
            raise SystemExit("--approver console needs --identity (who is approving)")
        return ConsoleApprover(identity)
    return DenyAll()


def _summary(path: Path, result: GateResult) -> dict[str, Any]:
    return {
        "run": path.parent.name,
        "requests": len(result.decisions),
        "needs_human": sum(1 for d in result.decisions if d.request.needs_human),
        "released": len(result.released),
        "withheld": len(result.withheld),
        "decisions": [d.model_dump(mode="json") for d in result.decisions],
    }


def _print(path: Path, result: GateResult) -> None:
    print(f"\n== {path.parent.name}")
    if not result.decisions:
        print("   no recommendation to gate")
        return
    for d in result.decisions:
        mark = {
            Decision.APPROVED: "APPROVED",
            Decision.DENIED: "WITHHELD",
            Decision.NOT_REQUIRED: "released",
        }[d.decision]
        req = d.request
        print(f"   {mark:<9} {req.agent.value}/{req.action_id}: {req.action[:90]}")
        for reason in req.reasons:
            print(f"             - {reason}")
        print(f"             decided by {d.decided_by}: {d.note}")


def main(argv: list[str] | None = None, *, audit: AuditLog | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", type=Path, help="one run directory or response.json")
    parser.add_argument("--approver", choices=("deny_all", "console"), default="deny_all")
    parser.add_argument("--identity", help="who is approving (console approver)")
    parser.add_argument(
        "--persist",
        action="store_true",
        help="write the decisions to hitl_audit_log on the stack's Postgres",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    if audit is None:
        audit = default_audit_log() if args.persist else MemoryAuditLog()
    gate = HitlGate(_approver(args.approver, args.identity), audit=audit)
    summaries: list[dict[str, Any]] = []
    skipped: list[str] = []
    for path in _responses(args.run):
        try:
            response = CoordinatorResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError as exc:
            skipped.append(
                f"{path.parent.name}: not a CoordinatorResponse ({exc.error_count()} errors)"
            )
            continue
        result = gate.review(response)
        summaries.append(_summary(path, result))
        if not args.json:
            _print(path, result)

    total = sum(s["requests"] for s in summaries)
    if args.json:
        print(
            json.dumps({"runs": summaries, "skipped": skipped, "persisted": args.persist}, indent=2)
        )
    else:
        human = sum(s["needs_human"] for s in summaries)
        withheld = sum(s["withheld"] for s in summaries)
        print(
            f"\n{len(summaries)} response(s), {total} recommendation(s), {human} needing a "
            f"human, {withheld} withheld."
        )
        if args.persist:
            print(f"{total} decision(s) written to the audit log (hitl_audit_log).")
        else:
            print(
                f"{total} decision(s) recorded in memory only, not persisted "
                "(pass --persist to write them to hitl_audit_log)."
            )
        for line in skipped:
            print(f"skipped {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
