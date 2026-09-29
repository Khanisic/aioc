"""Day 17: read the audit log of approval decisions.

    uv run python scripts/audit_log.py                       # every decision, oldest first
    uv run python scripts/audit_log.py --request req_1a2b3c4d
    uv run python scripts/audit_log.py --decision approved --since 2026-09-28
    uv run python scripts/audit_log.py --limit 20 --json

Free: no API calls. Reads `hitl_audit_log` on the stack's Postgres
(docker/postgres/init/05-hitl-audit.sql), which `aioc.hitl.HitlGate` writes every
decision to when it is given the Postgres log - `scripts/gate_recorded_run.py --persist`
and `respond(gate=...)` do. The table is append-only at the database, so what this prints
is what was decided, not what someone later edited.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from aioc.hitl import AuditLog, AuditLogError, Decision, default_audit_log

_MARK = {
    Decision.APPROVED: "APPROVED",
    Decision.DENIED: "WITHHELD",
    Decision.NOT_REQUIRED: "released",
}


def _since(text: str | None) -> datetime | None:
    if text is None:
        return None
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def main(argv: list[str] | None = None, *, log: AuditLog | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--request", help="only decisions about this request_id")
    parser.add_argument("--decision", choices=[d.value for d in Decision])
    parser.add_argument("--since", help="ISO date or timestamp; naive means UTC")
    parser.add_argument("--limit", type=int, help="the newest N")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    store = log if log is not None else default_audit_log()
    try:
        decisions = store.decisions(
            request_id=args.request,
            decision=Decision(args.decision) if args.decision else None,
            since=_since(args.since),
            limit=args.limit,
        )
    except AuditLogError as exc:
        print(f"{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([d.model_dump(mode="json") for d in decisions], indent=2))
        return 0
    for d in decisions:
        req = d.request
        print(
            f"{d.decided_at.isoformat()}  {_MARK[d.decision]:<9} {req.request_id} "
            f"{req.agent.value}/{req.action_id}: {req.action[:80]}"
        )
        print(f"{'':27}by {d.decided_by}: {d.note[:140]}")
    print(f"{len(decisions)} decision(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
