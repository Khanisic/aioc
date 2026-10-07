"""The database schema as an ordered list of files, applied once each (Day 24).

`docker/postgres/init/` runs only when the local Postgres volume is first initialised, and
never against a hosted database. Applying the files by hand works once and then cannot
tell what a database already has: `02-incidents.sql` is not idempotent on purpose (a
second run fails loudly instead of quietly skipping a changed table), so "run them all
again" is not an upgrade path.

This module is the upgrade path, and the migration-tool decision Day 24 had to make
(`docs/design-notes/deployment.md`): the init files are the migrations, in filename
order, and a ledger table records which have been applied and what each one's bytes
were. A run applies only the files the ledger lacks, each in its own transaction with
its ledger row, so a file is applied completely or not at all. A file whose bytes
changed after it was applied is refused rather than re-run: the change belongs in a new,
later-numbered file, which is the one rule a migration ledger asks of its authors.

Two ways a database can already hold the schema without a ledger, both handled:

- the local Docker volume, initialised by ``init/`` itself: ``baseline`` records every
  file as applied without running any, once, after checking the tables are there;
- nothing else - a hosted database starts empty and gets every file in order.

Platform Layer. Plain SQL through psycopg, no ORM and no new dependency: the files are
the schema, and Postgres runs them exactly as ``docker-entrypoint-initdb.d`` would.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import psycopg

# The schema's files, in the order Postgres's own entrypoint runs them.
INIT_DIR = Path(__file__).resolve().parents[3] / "docker" / "postgres" / "init"

LEDGER = "schema_migrations"
_LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS {LEDGER} (
    filename   text        PRIMARY KEY,
    sha256     text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now(),
    how        text        NOT NULL CHECK (how IN ('applied', 'baseline'))
)
"""

# What `baseline` checks before claiming a database holds the schema: one table a file
# creates, for every file that creates one. A database missing any of them is not the
# one `init/` built, and recording it as migrated would hide that.
_EVIDENCE: dict[str, str] = {
    "02-incidents.sql": "incidents",
    "04-embeddings.sql": "incident_embeddings",
    "05-hitl-audit.sql": "hitl_audit_log",
}

Action = Literal["apply", "skip", "refuse"]


class SchemaError(RuntimeError):
    """The database and the files disagree in a way no run should paper over."""


@dataclass(frozen=True, slots=True)
class SchemaFile:
    name: str
    path: Path
    sha256: str

    @classmethod
    def read(cls, path: Path) -> SchemaFile:
        return cls(path.name, path, hashlib.sha256(path.read_bytes()).hexdigest())

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class Step:
    """What a run does with one file, and why."""

    file: SchemaFile
    action: Action
    why: str


def schema_files(directory: Path = INIT_DIR) -> list[SchemaFile]:
    """Every ``*.sql`` file, in filename order - the order the entrypoint runs them."""
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise SchemaError(f"no schema files in {directory}")
    return [SchemaFile.read(path) for path in files]


def plan(files: Sequence[SchemaFile], ledger: dict[str, str]) -> list[Step]:
    """Pure: what a run would do, given the ledger's ``filename -> sha256``."""
    known = {file.name for file in files}
    vanished = sorted(set(ledger) - known)
    if vanished:
        raise SchemaError(
            f"the database records {vanished} as applied, and no such file exists here: "
            "a schema file was renamed or deleted after it was applied"
        )
    steps: list[Step] = []
    for file in files:
        recorded = ledger.get(file.name)
        if recorded is None:
            steps.append(Step(file, "apply", "not applied yet"))
        elif recorded == file.sha256:
            steps.append(Step(file, "skip", "applied, unchanged"))
        else:
            steps.append(
                Step(
                    file,
                    "refuse",
                    f"applied as {recorded[:12]}, and the file is now {file.sha256[:12]}: "
                    "a change to an applied file belongs in a new, later-numbered file",
                )
            )
    return steps


Connect = Callable[[], Any]


def _connect(dsn: str) -> Connect:
    def connect() -> Any:
        return psycopg.connect(dsn, autocommit=True)

    return connect


@contextmanager
def _session(connect: Connect) -> Iterator[Any]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def read_ledger(conn: Any, *, create: bool = True) -> dict[str, str]:
    """The ledger's ``filename -> sha256``. ``create=False`` reads a missing ledger as
    empty without creating it, so a dry run leaves the database exactly as it found it."""
    if create:
        conn.execute(_LEDGER_DDL)
    elif conn.execute("SELECT to_regclass(%s)", (LEDGER,)).fetchone()[0] is None:
        return {}
    rows = conn.execute(f"SELECT filename, sha256 FROM {LEDGER}").fetchall()  # noqa: S608
    return {str(name): str(sha) for name, sha in rows}


def migrate(
    dsn: str | None = None,
    *,
    directory: Path = INIT_DIR,
    dry_run: bool = False,
    connect: Connect | None = None,
    say: Callable[[str], None] = print,
) -> list[Step]:
    """Apply every file the ledger lacks, in order. Refuses before applying anything if
    any applied file changed, so a run never leaves a database half-upgraded past a
    file it could not reconcile."""
    connect = connect or _connect(_dsn(dsn))
    files = schema_files(directory)
    with _session(connect) as conn:
        steps = plan(files, read_ledger(conn, create=not dry_run))
        refused = [step for step in steps if step.action == "refuse"]
        for step in steps:
            say(f"  {step.action:<6} {step.file.name}  ({step.why})")
        if refused:
            raise SchemaError(
                "; ".join(f"{step.file.name}: {step.why}" for step in refused)
                + ". Nothing was applied."
            )
        if dry_run:
            return steps
        for step in steps:
            if step.action != "apply":
                continue
            # One transaction for the file and its ledger row: applied completely, and
            # recorded, or neither.
            with conn.transaction():
                conn.execute(step.file.sql)
                conn.execute(
                    f"INSERT INTO {LEDGER} (filename, sha256, how) VALUES (%s, %s, 'applied')",  # noqa: S608
                    (step.file.name, step.file.sha256),
                )
            say(f"  done   {step.file.name}")
    return steps


def baseline(
    dsn: str | None = None,
    *,
    directory: Path = INIT_DIR,
    connect: Connect | None = None,
    say: Callable[[str], None] = print,
) -> list[str]:
    """Record every file as applied without running it - for a database ``init/`` built,
    which has the schema and no ledger. Refused unless every table the files create is
    there, and refused on a database whose ledger already has rows."""
    connect = connect or _connect(_dsn(dsn))
    files = schema_files(directory)
    with _session(connect) as conn:
        if read_ledger(conn):
            raise SchemaError(
                f"{LEDGER} already has rows; baseline is for a database that has none - "
                "run migrate instead"
            )
        missing = [
            table
            for name, table in _EVIDENCE.items()
            if any(file.name == name for file in files)
            and conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None
        ]
        if missing:
            raise SchemaError(
                f"tables {missing} do not exist: this database does not hold the schema, "
                "so recording it as applied would be false - run migrate instead"
            )
        with conn.transaction():
            for file in files:
                conn.execute(
                    f"INSERT INTO {LEDGER} (filename, sha256, how) VALUES (%s, %s, 'baseline')",  # noqa: S608
                    (file.name, file.sha256),
                )
                say(f"  baseline {file.name}")
    return [file.name for file in files]


def _dsn(dsn: str | None) -> str:
    if dsn:
        return dsn
    from aioc.tools.incident.store import dsn as store_dsn  # the one DATABASE_URL reader

    return store_dsn()
