"""Day 24: the schema files as migrations, applied once each through a ledger.

`docker/postgres/init/` never runs against a hosted database, and `02-incidents.sql`
fails on a second run by design, so "apply them all again" is not an upgrade path. The
rules these tests hold:

- a file the ledger lacks is applied, one already applied with the same bytes is
  skipped, and one whose bytes changed after it was applied is refused - before anything
  else is applied, so no run leaves a database half-upgraded;
- a file the ledger names and the directory does not have is refused (a rename or a
  delete of an applied file);
- a file and its ledger row commit together;
- ``status`` (a dry run) changes nothing, not even by creating the ledger;
- ``baseline`` records a database ``init/`` built, and refuses one that does not hold
  the schema or already has a ledger.

The planning is pure and tested offline. The rest runs against a scratch database
created and dropped on the stack's server (`integration`), never the stack's own.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest

from aioc.db.__main__ import main as db_main
from aioc.db.schema import (
    INIT_DIR,
    LEDGER,
    SchemaError,
    SchemaFile,
    baseline,
    migrate,
    plan,
    schema_files,
)


def _file(tmp_path: Path, name: str, sql: str) -> SchemaFile:
    path = tmp_path / name
    path.write_text(sql, encoding="utf-8")
    return SchemaFile.read(path)


# ============================================================================ planning


def test_the_files_are_the_init_directory_in_filename_order():
    names = [file.name for file in schema_files()]
    assert names == sorted(path.name for path in INIT_DIR.glob("*.sql"))
    assert names[0] == "01-extensions.sql" and "05-hitl-audit.sql" in names


def test_an_empty_directory_is_refused(tmp_path: Path):
    with pytest.raises(SchemaError, match="no schema files"):
        schema_files(tmp_path)


def test_each_file_is_applied_skipped_or_refused(tmp_path: Path):
    a = _file(tmp_path, "01-a.sql", "SELECT 1;")
    b = _file(tmp_path, "02-b.sql", "SELECT 2;")
    c = _file(tmp_path, "03-c.sql", "SELECT 3;")
    steps = plan([a, b, c], {a.name: a.sha256, b.name: "0" * 64})
    assert [(s.file.name, s.action) for s in steps] == [
        ("01-a.sql", "skip"),
        ("02-b.sql", "refuse"),
        ("03-c.sql", "apply"),
    ]
    assert "new, later-numbered file" in steps[1].why


def test_an_applied_file_that_is_gone_is_refused(tmp_path: Path):
    a = _file(tmp_path, "01-a.sql", "SELECT 1;")
    with pytest.raises(SchemaError, match=r"\['00-gone.sql'\].*renamed or deleted"):
        plan([a], {"00-gone.sql": "0" * 64})


def test_a_files_identity_is_its_bytes(tmp_path: Path):
    first = _file(tmp_path, "01-a.sql", "SELECT 1;")
    same = _file(tmp_path, "01-a.sql", "SELECT 1;")
    changed = _file(tmp_path, "01-a.sql", "SELECT 1; -- a comment is a change too")
    assert first.sha256 == same.sha256 != changed.sha256


# ========================================================================= integration


@pytest.fixture
def scratch() -> Iterator[str]:
    """A fresh, empty database on the stack's server, dropped afterwards."""
    import psycopg

    from aioc.tools.incident.store import dsn

    base = dsn()
    name = f"aioc_test_{uuid4().hex[:10]}"
    try:
        admin = psycopg.connect(base, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"database unreachable ({str(exc).splitlines()[0]})")
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield urlunsplit(urlsplit(base)._replace(path=f"/{name}"))
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _query(dsn: str, sql: str) -> list[tuple[object, ...]]:
    import psycopg

    with psycopg.connect(dsn) as conn:
        return list(conn.execute(sql).fetchall())  # type: ignore[arg-type]


def _quiet(_line: str) -> None:
    return None


@pytest.mark.integration
def test_an_empty_database_gets_the_whole_schema_and_the_corpus(scratch: str):
    steps = migrate(scratch, say=_quiet)
    assert {step.action for step in steps} == {"apply"}
    assert _query(scratch, "SELECT count(*) FROM incidents") == [(18,)]
    assert _query(scratch, "SELECT count(*) FROM incident_timeline_events") == [(65,)]
    assert _query(scratch, "SELECT to_regclass('hitl_audit_log') IS NOT NULL") == [(True,)]
    ledger = _query(scratch, f"SELECT filename, how FROM {LEDGER} ORDER BY filename")  # noqa: S608
    assert ledger == [(file.name, "applied") for file in schema_files()]


@pytest.mark.integration
def test_a_second_run_applies_nothing(scratch: str):
    migrate(scratch, say=_quiet)
    steps = migrate(scratch, say=_quiet)
    assert {step.action for step in steps} == {"skip"}
    assert _query(scratch, "SELECT count(*) FROM incidents") == [(18,)]


@pytest.mark.integration
def test_status_changes_nothing_not_even_the_ledger(scratch: str):
    steps = migrate(scratch, dry_run=True, say=_quiet)
    assert {step.action for step in steps} == {"apply"}
    assert _query(scratch, f"SELECT to_regclass('{LEDGER}')") == [(None,)]
    assert _query(scratch, "SELECT to_regclass('incidents')") == [(None,)]


@pytest.mark.integration
def test_a_changed_file_is_refused_before_anything_is_applied(scratch: str, tmp_path: Path):
    files = tmp_path / "init"
    shutil.copytree(INIT_DIR, files)
    migrate(scratch, directory=files, say=_quiet)
    # Edit an applied file and add a new one after it: the new one must not run either.
    applied = files / "02-incidents.sql"
    applied.write_text(applied.read_text(encoding="utf-8") + "\n-- edited\n", encoding="utf-8")
    (files / "06-new.sql").write_text("CREATE TABLE day24_probe (id int);", encoding="utf-8")
    with pytest.raises(SchemaError, match=r"02-incidents.sql: applied as .*Nothing was applied"):
        migrate(scratch, directory=files, say=_quiet)
    assert _query(scratch, "SELECT to_regclass('day24_probe')") == [(None,)]


@pytest.mark.integration
def test_a_file_that_fails_leaves_no_ledger_row_and_no_half_applied_file(
    scratch: str, tmp_path: Path
):
    files = tmp_path / "init"
    files.mkdir()
    (files / "01-ok.sql").write_text("CREATE TABLE day24_ok (id int);", encoding="utf-8")
    (files / "02-bad.sql").write_text(
        "CREATE TABLE day24_half (id int);\nSELECT no_such_function();", encoding="utf-8"
    )
    import psycopg

    with pytest.raises(psycopg.errors.UndefinedFunction):
        migrate(scratch, directory=files, say=_quiet)
    assert _query(scratch, f"SELECT filename FROM {LEDGER}") == [("01-ok.sql",)]  # noqa: S608
    assert _query(scratch, "SELECT to_regclass('day24_half')") == [(None,)]


@pytest.mark.integration
def test_baseline_records_a_database_init_built_and_refuses_the_rest(scratch: str):
    with pytest.raises(SchemaError, match="tables .* do not exist"):
        baseline(scratch, say=_quiet)
    # Build it the way `init/` does - every file, no ledger - then baseline it.
    import psycopg

    with psycopg.connect(scratch, autocommit=True) as conn:
        for file in schema_files():
            conn.execute(file.sql)  # type: ignore[arg-type]
    recorded = baseline(scratch, say=_quiet)
    assert recorded == [file.name for file in schema_files()]
    assert {step.action for step in migrate(scratch, say=_quiet)} == {"skip"}
    with pytest.raises(SchemaError, match="already has rows"):
        baseline(scratch, say=_quiet)


@pytest.mark.integration
def test_the_command_line_says_what_it_did(scratch: str, capsys: pytest.CaptureFixture[str]):
    assert db_main(["status", "--database-url", scratch]) == 0
    assert capsys.readouterr().out.strip().endswith(f"{len(schema_files())} file(s) to apply")
    assert db_main(["migrate", "--database-url", scratch]) == 0
    assert capsys.readouterr().out.strip().endswith(f"applied {len(schema_files())} file(s)")
    assert db_main(["migrate", "--database-url", scratch]) == 0
    assert capsys.readouterr().out.strip().endswith("up to date; nothing applied")


def test_an_unreachable_database_is_exit_2(capsys: pytest.CaptureFixture[str]):
    url = "postgresql://nobody:nothing@127.0.0.1:1/none?connect_timeout=1"
    assert db_main(["status", "--database-url", url]) == 2
    assert capsys.readouterr().err.startswith("cannot reach the database:")
