"""Day 17 - the audit log of approval decisions.

Every decision the gate makes is written before it is returned, `not_required` included;
a decision the log refuses is not released; the Postgres store round-trips the record and
refuses to rewrite a row at the database. The store tests that need the stack are marked
`integration` and run inside a rolled-back transaction, so the shared database is never
left with test rows.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import audit_log as audit_script
import gate_recorded_run as replay
import pytest

from aioc.contracts import AgentName
from aioc.coordinator import Executor, respond
from aioc.coordinator.executor import _review
from aioc.hitl import (
    ApprovalDecision,
    AuditLogError,
    Decision,
    HitlGate,
    MemoryAuditLog,
    PostgresAuditLog,
    ScriptedApprover,
)
from aioc.hitl.audit import AUDIT_SQL
from tests.test_executor import _assessment, _FakeTracer, _invocation, _RecordingRunner, _skip
from tests.test_hitl import _action, _payload, _response, _rollback_now

_FIXED = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def _write(action: dict[str, Any] | None = None) -> dict[str, Any]:
    """A medium-risk production write the gate must send to a human."""
    return action or _payload(
        _action(
            id="act_write",
            action="Roll back checkout-api to v1.4.2",
            requires_approval=True,
            risk="medium",
        )
    )


# ------------------------------------------------------------------- the memory store


def test_the_memory_log_appends_and_reads_back_in_decision_order():
    log = MemoryAuditLog()
    gate = HitlGate(ScriptedApprover({"act_write": True}, identity="alice"), audit=log)
    result = gate.review(_response([_write(), _payload(_action())]))

    assert len(log) == 2
    assert log.decisions() == result.decisions
    assert [d.decision for d in log.decisions()] == [Decision.APPROVED, Decision.NOT_REQUIRED]
    assert log.decisions(decision=Decision.APPROVED) == [result.decisions[0]]
    assert log.decisions(request_id="req_nobody") == []
    assert log.decisions(request_id=result.decisions[0].request.request_id) == result.decisions
    assert log.decisions(limit=1) == [result.decisions[-1]]
    assert log.decisions(since=_FIXED + timedelta(days=365)) == []


def test_the_memory_log_refuses_a_duplicate_decision_id():
    log = MemoryAuditLog()
    (decision,) = HitlGate(audit=log).review(_response()).decisions
    with pytest.raises(AuditLogError, match="already recorded"):
        log.append(decision)
    assert len(log) == 1


# --------------------------------------------------------------- the gate writes first


def test_the_gate_has_a_log_by_default_and_writes_every_decision_to_it():
    gate = HitlGate(ScriptedApprover({}))
    result = gate.review(_response([_write(), _payload(_action())], deployment=_rollback_now()))

    assert isinstance(gate.audit, MemoryAuditLog)
    logged = gate.audit.decisions()
    assert sorted(d.decision_id for d in logged) == sorted(d.decision_id for d in result.decisions)
    assert {d.decision for d in logged} == {Decision.DENIED, Decision.NOT_REQUIRED}
    assert len(logged) == 3  # the write, the read-only action, and the rollback


class _Down:
    """A store that is down: every append raises."""

    def __init__(self) -> None:
        self.attempts: list[ApprovalDecision] = []

    def append(self, decision: ApprovalDecision) -> None:
        self.attempts.append(decision)
        raise AuditLogError("connection refused")

    def decisions(self, **_: Any) -> list[ApprovalDecision]:
        return []


def test_a_release_the_log_could_not_record_comes_back_denied_and_says_why():
    store = _Down()
    gate = HitlGate(ScriptedApprover({"act_write": True}, identity="alice"), audit=store)
    result = gate.review(_response([_write(), _payload(_action())]))

    assert [d.decision for d in result.decisions] == [Decision.DENIED, Decision.DENIED]
    approved, read_only = result.decisions
    assert approved.decided_by == "_Down"
    assert "approved by alice" in approved.note
    assert "could not be recorded" in approved.note and "connection refused" in approved.note
    assert "fails closed" in approved.note
    assert "not_required by policy" in read_only.note
    assert result.released == []
    # It tried to write the original and then the denial, for each request.
    assert [d.decision for d in store.attempts] == [
        Decision.APPROVED,
        Decision.DENIED,
        Decision.NOT_REQUIRED,
        Decision.DENIED,
    ]


def test_a_denial_the_log_could_not_record_stays_a_denial_that_says_so():
    gate = HitlGate(audit=_Down())
    (decision,) = gate.review(_response()).decisions
    assert decision.decision is Decision.DENIED
    assert "denied by deny_all" in decision.note and "could not be recorded" in decision.note


class _Crashes(_Down):
    def append(self, decision: ApprovalDecision) -> None:
        raise RuntimeError("boom")


def test_a_crashing_log_never_crashes_the_review():
    result = HitlGate(audit=_Crashes()).review(_response([_write(), _payload(_action())]))
    assert len(result.decisions) == 2 and result.released == []


# ------------------------------------------------------------ respond(gate=...) opt-in


def test_review_records_the_gate_on_the_trace_without_tokens():
    tracer = _FakeTracer()
    trace = tracer.start_request("coordinator_request", request_id="req_x", query="q")
    gate = HitlGate(ScriptedApprover({}, identity="bob"))
    _review(gate, _response([_write(), _payload(_action())]), trace)

    (span,) = trace.spans
    assert span.name == "hitl_gate"
    assert span.metadata["approver"] == "ScriptedApprover"
    assert span.ended["status"] == "ok"
    assert span.ended["input_tokens"] == 0 and span.ended["output_tokens"] == 0
    assert span.ended["output"] == "2 recommendation(s): 1 released, 1 withheld"
    assert len(gate.audit.decisions()) == 2


def test_respond_with_a_gate_decides_and_logs_by_the_responses_request_id():
    from types import SimpleNamespace

    from anthropic.types import ToolUseBlock

    from aioc.coordinator import SELECT_TOOL_NAME, Coordinator
    from aioc.llm import LLMClient, LLMSettings

    plan_payload = {
        "intent": _assessment("incident_diagnosis", 0.92),
        "selected_agents": [_invocation("incident")],
        "skipped_agents": [_skip("docs"), _skip("github"), _skip("deployment")],
        "gaps": [],
    }
    scripted = SimpleNamespace(
        stop_reason="tool_use",
        model="claude-sonnet-5",
        content=[
            ToolUseBlock(type="tool_use", id="toolu_1", name=SELECT_TOOL_NAME, input=plan_payload)
        ],
        usage=SimpleNamespace(input_tokens=300, output_tokens=180),
    )

    class _FakeMessages:
        def create(self, **kwargs: Any) -> Any:
            return scripted

    fake = SimpleNamespace(messages=_FakeMessages())
    coordinator = Coordinator(LLMClient(LLMSettings(model="claude-sonnet-5"), client=fake))  # type: ignore[arg-type]
    gate = HitlGate()
    tracer = _FakeTracer()

    resp = respond(
        "Why is checkout failing?",
        coordinator=coordinator,
        executor=Executor({AgentName.INCIDENT: _RecordingRunner()}),
        tracer=tracer,
        gate=gate,
    )

    # The recording runner's report recommends nothing, so the gate decided nothing -
    # and still ran, on its own span, after the agents.
    assert gate.audit.decisions(request_id=resp.request_id) == []
    (trace,) = tracer.traces
    assert [s.name for s in trace.spans][-1] == "hitl_gate"
    assert trace.spans[-1].metadata["request_id"] == resp.request_id


def test_respond_without_a_gate_decides_nothing():
    tracer = _FakeTracer()
    from tests.test_executor import _plan

    Executor({AgentName.INCIDENT: _RecordingRunner()}, tracer=tracer).execute(
        _plan([_invocation("incident")]), "q"
    )
    (trace,) = tracer.traces
    assert "hitl_gate" not in [s.name for s in trace.spans]


# ----------------------------------------------------------------------- the scripts


def test_the_replay_script_persists_through_the_log_it_is_given(tmp_path: Path, capsys):
    run = tmp_path / "run"
    run.mkdir()
    (run / "response.json").write_text(_response([_write()]).model_dump_json(), encoding="utf-8")
    log = MemoryAuditLog()

    assert replay.main(["--run", str(run), "--persist"], audit=log) == 0

    out = capsys.readouterr().out
    assert "WITHHELD" in out and "1 decision(s) written to the audit log" in out
    (decision,) = log.decisions()
    assert decision.decision is Decision.DENIED and decision.request.action_id == "act_write"


def test_the_replay_script_without_persist_writes_nowhere_durable(tmp_path: Path, capsys):
    run = tmp_path / "run"
    run.mkdir()
    (run / "response.json").write_text(_response([_write()]).model_dump_json(), encoding="utf-8")
    assert replay.main(["--run", str(run)]) == 0
    assert "not persisted" in capsys.readouterr().out


def test_the_audit_script_lists_and_filters_the_log(capsys):
    log = MemoryAuditLog()
    gate = HitlGate(ScriptedApprover({"act_write": True}, identity="alice"), audit=log)
    gate.review(_response([_write(), _payload(_action())]))

    assert audit_script.main([], log=log) == 0
    out = capsys.readouterr().out
    assert "APPROVED" in out and "released" in out and "alice" in out
    assert "2 decision(s)" in out

    assert audit_script.main(["--decision", "approved", "--json"], log=log) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["decision"] for r in rows] == ["approved"]
    assert rows[0]["request"]["action_id"] == "act_write"

    assert audit_script.main(["--request", "req_nobody"], log=log) == 0
    assert "0 decision(s)" in capsys.readouterr().out


def test_the_audit_script_reports_an_unreachable_log(capsys):
    assert audit_script.main([], log=_Down()) == 0  # _Down.decisions returns nothing
    assert "0 decision(s)" in capsys.readouterr().out

    class _Unreadable(_Down):
        def decisions(self, **_: Any) -> list[ApprovalDecision]:
            raise AuditLogError("audit log unreachable: connection refused")

    assert audit_script.main([], log=_Unreadable()) == 2
    assert "unreachable" in capsys.readouterr().err


# ---------------------------------------------------------------------- the SQL itself


def test_the_init_file_is_re_applicable_and_append_only_by_construction():
    sql = AUDIT_SQL.read_text(encoding="utf-8")
    assert AUDIT_SQL.name == "05-hitl-audit.sql"
    assert "CREATE TABLE IF NOT EXISTS hitl_audit_log" in sql
    assert sql.count("CREATE OR REPLACE TRIGGER") == 2
    assert "BEFORE UPDATE OR DELETE ON hitl_audit_log" in sql
    assert "BEFORE TRUNCATE ON hitl_audit_log" in sql
    assert "restrict_violation" in sql
    # Nothing in the repo updates or deletes from it either.
    src = Path(__file__).resolve().parents[1] / "src"
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert "update hitl_audit_log" not in text and "delete from hitl_audit_log" not in text


# ---------------------------------------------------------------------- integration


@pytest.fixture(autouse=True)
def _require_reachable_store(request: pytest.FixtureRequest) -> None:
    if "integration" not in request.keywords:
        return
    import psycopg

    from aioc.tools.incident.store import dsn

    try:
        with psycopg.connect(dsn(), connect_timeout=3):
            return
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unreachable ({str(exc).splitlines()[0]})")


@pytest.fixture
def rollback_conn():
    import psycopg

    from aioc.tools.incident.store import dsn

    with psycopg.connect(dsn()) as conn:
        yield conn
        conn.rollback()


@pytest.mark.integration
def test_the_postgres_log_round_trips_and_filters(rollback_conn: Any) -> None:
    log = PostgresAuditLog(connection=rollback_conn)
    log.ensure_schema()  # a no-op on a database that already has the table
    gate = HitlGate(
        ScriptedApprover({"act_write": True}, identity="alice"), audit=log, clock=lambda: _FIXED
    )
    result = gate.review(_response([_write(), _payload(_action())]))

    # Every read is scoped to this request: the table is shared with every other run that
    # ever persisted (the live replay's rows are in it), which is the point of the store.
    rid = result.decisions[0].request.request_id
    assert result.decisions == log.decisions(request_id=rid)
    assert log.decisions(request_id=rid, decision=Decision.NOT_REQUIRED, since=_FIXED) == [
        result.decisions[1]
    ]
    assert log.decisions(request_id=rid, since=_FIXED + timedelta(seconds=1)) == []
    assert log.decisions(request_id=rid, limit=1) == [result.decisions[1]]
    (approved,) = log.decisions(request_id=rid, decision=Decision.APPROVED, since=_FIXED)
    assert approved.decided_by == "alice" and approved.decided_at == _FIXED
    with pytest.raises(AuditLogError, match="refused"):
        log.append(result.decisions[0])  # the primary key holds


@pytest.mark.integration
def test_the_postgres_log_is_append_only_at_the_database(rollback_conn: Any) -> None:
    import psycopg

    log = PostgresAuditLog(connection=rollback_conn)
    (decision,) = HitlGate(audit=log, clock=lambda: _FIXED).review(_response()).decisions
    for statement in (
        "UPDATE hitl_audit_log SET decision = 'approved' WHERE decision_id = %s",
        "DELETE FROM hitl_audit_log WHERE decision_id = %s",
    ):
        with (
            pytest.raises(psycopg.errors.RestrictViolation, match="append-only"),
            rollback_conn.transaction(),
            rollback_conn.cursor() as cur,
        ):
            cur.execute(statement, (decision.decision_id,))
    with (
        pytest.raises(psycopg.errors.RestrictViolation, match="append-only"),
        rollback_conn.transaction(),
        rollback_conn.cursor() as cur,
    ):
        cur.execute("TRUNCATE hitl_audit_log")
    # The row is still there, untouched, after every refused rewrite.
    assert log.decisions(request_id=decision.request.request_id) == [decision]


@pytest.mark.integration
def test_the_init_file_applies_twice_without_complaint(rollback_conn: Any) -> None:
    sql = AUDIT_SQL.read_text(encoding="utf-8")
    with rollback_conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(sql)
        cur.execute("SELECT count(*) FROM hitl_audit_log")
        assert cur.fetchone() is not None
