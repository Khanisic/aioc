"""The audit log of approval decisions (Day 17): every `ApprovalDecision`, written before
it is returned, to a store that only appends.

The gate (`aioc.hitl.gate.HitlGate`) records each decision through an `AuditLog` as it
makes it - approved, denied, and ``not_required`` alike, because "why nobody was asked"
is as much a fact as "who answered". Two stores implement the seam:

- `MemoryAuditLog` - a list behind a lock. The gate's default, so a gate always has a
  log; tests read it back, and short-lived tools that only print use it.
- `PostgresAuditLog` - the `hitl_audit_log` table (``docker/postgres/init/05-hitl-audit.sql``),
  which is append-only at the database: triggers refuse UPDATE, DELETE, and TRUNCATE.
  Nothing here can rewrite a row because nothing anywhere can. The table is applied on
  first use when it is missing, the way the ingest script applies 04, because ``init/``
  only runs on a fresh volume.

The store is the second half of failing closed. The gate treats a decision it could not
record as one it did not make: a release the log refused becomes a denial that says so.
That rule lives in the gate, next to the other fail-closed rules; this module only
promises that `append` either persists the record or raises.

The records are not part of the frozen contract (`aioc.hitl.gate` says why) and neither
is this table; it can churn with the record.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from aioc.tools.incident.store import dsn as default_dsn

from .gate import ApprovalDecision, Decision

AUDIT_SQL = (
    Path(__file__).resolve().parents[3] / "docker" / "postgres" / "init" / "05-hitl-audit.sql"
)


class AuditLog(Protocol):
    """Append a decision, or raise; read decisions back in the order they were appended."""

    def append(self, decision: ApprovalDecision) -> None: ...

    def decisions(
        self,
        *,
        request_id: str | None = None,
        decision: Decision | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[ApprovalDecision]: ...


class AuditLogError(RuntimeError):
    """The store could not persist or read a record. The gate turns a failed append into
    a denial; a reader reports it."""


def _select(
    records: list[ApprovalDecision],
    *,
    request_id: str | None,
    decision: Decision | None,
    since: datetime | None,
    limit: int | None,
) -> list[ApprovalDecision]:
    chosen = [
        r
        for r in records
        if (request_id is None or r.request.request_id == request_id)
        and (decision is None or r.decision is decision)
        and (since is None or r.decided_at >= since)
    ]
    # Append order, never a sort by time: decisions made under one clock tick (a scripted
    # review, a fixed test clock) would otherwise come back shuffled by id.
    return chosen[-limit:] if limit is not None and limit >= 0 else chosen


class MemoryAuditLog:
    """Append-only in this process: there is no method that removes or changes a record."""

    def __init__(self) -> None:
        self._records: list[ApprovalDecision] = []
        self._lock = Lock()

    def append(self, decision: ApprovalDecision) -> None:
        with self._lock:
            if any(r.decision_id == decision.decision_id for r in self._records):
                raise AuditLogError(f"decision {decision.decision_id} is already recorded")
            self._records.append(decision)

    def decisions(
        self,
        *,
        request_id: str | None = None,
        decision: Decision | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[ApprovalDecision]:
        with self._lock:
            records = list(self._records)
        return _select(records, request_id=request_id, decision=decision, since=since, limit=limit)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class PostgresAuditLog:
    """The durable store. A DSN (defaulting to the stack's, the same resolution every
    tool server uses) or an injected connection - tests hand in one whose transaction
    is rolled back, so the shared database is never left with test rows."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        connection: psycopg.Connection[Any] | None = None,
    ) -> None:
        self._dsn = dsn
        self._connection = connection

    @contextmanager
    def _conn(self) -> Iterator[psycopg.Connection[Any]]:
        if self._connection is not None:
            yield self._connection
            return
        try:
            with psycopg.connect(self._dsn or default_dsn()) as owned:
                yield owned
                owned.commit()
        except psycopg.Error as exc:
            raise AuditLogError(f"audit log unreachable: {str(exc).splitlines()[0]}") from exc

    def ensure_schema(self) -> bool:
        """Apply 05-hitl-audit.sql when the table predates this database. True if applied."""
        with self._conn() as conn:
            return _ensure_table(conn)

    def append(self, decision: ApprovalDecision) -> None:
        req = decision.request
        try:
            with self._conn() as conn:
                _ensure_table(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO hitl_audit_log (
                            decision_id, request_id, invocation_id, agent, source, action_id,
                            decision, decided_by, decided_at, note, record
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            decision.decision_id,
                            req.request_id,
                            req.invocation_id,
                            req.agent.value,
                            req.source.value,
                            req.action_id,
                            decision.decision.value,
                            decision.decided_by,
                            decision.decided_at,
                            decision.note,
                            Jsonb(decision.model_dump(mode="json")),
                        ),
                    )
        except psycopg.Error as exc:
            raise AuditLogError(
                f"audit log refused {decision.decision_id}: {str(exc).splitlines()[0]}"
            ) from exc

    def decisions(
        self,
        *,
        request_id: str | None = None,
        decision: Decision | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> list[ApprovalDecision]:
        clauses: list[str] = []
        params: list[Any] = []
        if request_id is not None:
            clauses.append("request_id = %s")
            params.append(request_id)
        if decision is not None:
            clauses.append("decision = %s")
            params.append(decision.value)
        if since is not None:
            clauses.append("decided_at >= %s")
            params.append(since)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        try:
            with self._conn() as conn:
                _ensure_table(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT record FROM hitl_audit_log"  # noqa: S608 - clauses are fixed text
                        + where
                        + " ORDER BY seq",
                        params,
                    )
                    rows = cur.fetchall()
        except psycopg.Error as exc:
            raise AuditLogError(f"audit log unreadable: {str(exc).splitlines()[0]}") from exc
        records = [ApprovalDecision.model_validate(row[0]) for row in rows]
        return records[-limit:] if limit is not None and limit >= 0 else records


def _ensure_table(conn: psycopg.Connection[Any]) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('hitl_audit_log')")
        row = cur.fetchone()
        if row and row[0] is not None:
            return False
        cur.execute(AUDIT_SQL.read_text(encoding="utf-8"))
    return True


def default_audit_log() -> PostgresAuditLog:
    """The durable log on the stack's database - what a live entry point passes to the
    gate. Nothing connects until the first append or read."""
    return PostgresAuditLog()
