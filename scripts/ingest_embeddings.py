"""Embed the incident corpus for hybrid search (Day 8). Opt-in; costs Voyage API tokens.

    uv run python scripts/ingest_embeddings.py            # embed new/stale rows only
    uv run python scripts/ingest_embeddings.py --dry-run  # report what would embed, free

Costs one Voyage embeddings call for however many rows are new or stale (the whole 18-row
corpus is a single batch, roughly two thousand tokens - fractions of a cent). Re-running
against an unchanged corpus embeds nothing: ingestion hashes the rendered document text
and skips rows whose stored hash matches, so this script is safe to run casually.

Needs `VOYAGE_API_KEY` in the shell or `.env`, the Docker stack up, and the
`incident_embeddings` table (docker/postgres/init/04-embeddings.sql). If the table is
missing - a stack initialised before Day 8 - the script applies that file itself: it is
additive-only (CREATE TABLE IF NOT EXISTS) and written to be applied standalone.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg  # noqa: E402
from runlog import RunRecorder  # noqa: E402 - needs the sys.path insert above

from aioc.retrieval import CorpusSearcher, EmbeddingSettings, default_embedder  # noqa: E402
from aioc.retrieval.corpus import (  # noqa: E402
    _row_to_incident,
    render_document,
    stale_incident_ids,
    text_sha256,
)
from aioc.tools.incident.store import dsn  # noqa: E402

_EMBEDDINGS_SQL = (
    Path(__file__).resolve().parents[1] / "docker" / "postgres" / "init" / "04-embeddings.sql"
)


def _ensure_table(conn: psycopg.Connection) -> bool:
    """Apply 04-embeddings.sql when the table predates this database. Returns True if applied."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('incident_embeddings')")
        row = cur.fetchone()
        if row and row[0] is not None:
            return False
        cur.execute(_EMBEDDINGS_SQL.read_text(encoding="utf-8"))
    conn.commit()
    return True


def _dry_run(conn: psycopg.Connection, model: str) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, summary, started_at, ended_at, affected_services, "
            "true_severity, true_failure_mode, true_failure_mode_detail, "
            "true_root_cause, resolution FROM incidents"
        )
        digests = {
            row.id: text_sha256(render_document(row))
            for row in (_row_to_incident(raw) for raw in cur.fetchall())
        }
        cur.execute(
            "SELECT incident_id, input_sha256 FROM incident_embeddings WHERE model = %s",
            (model,),
        )
        existing = dict(cur.fetchall())
    stale = stale_incident_ids(digests, existing)
    return len(stale), len(digests)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be embedded without calling the provider",
    )
    args = parser.parse_args(argv)

    settings = EmbeddingSettings()
    print(f"embedding model: {settings.model} ({settings.dim} dims)")

    try:
        conn = psycopg.connect(dsn(), connect_timeout=5)
    except psycopg.OperationalError as exc:
        print(f"corpus unreachable: {str(exc).splitlines()[0]}", file=sys.stderr)
        print("is the stack up? docker compose up -d --wait", file=sys.stderr)
        return 2

    with conn:
        if _ensure_table(conn):
            print("applied docker/postgres/init/04-embeddings.sql (table was missing)")

        if args.dry_run:
            stale, total = _dry_run(conn, settings.model)
            print(f"dry run: {stale} of {total} rows would be embedded; no call made")
            return 0

        embedder = default_embedder(settings)
        if embedder is None:
            print("VOYAGE_API_KEY is not set (shell or .env).", file=sys.stderr)
            print("Lexical-only retrieval still works without it.", file=sys.stderr)
            return 2

        start = time.monotonic()
        with RunRecorder(
            kind="ingest",
            name="embed-corpus",
            command="ingest_embeddings.py",
            metadata={"model": embedder.model, "dim": embedder.dim},
        ) as run:
            try:
                report = CorpusSearcher(embedder).ingest(conn=conn)
                conn.commit()
            except Exception as exc:
                run.event(
                    "ingest",
                    outcome="failed",
                    duration_ms=(time.monotonic() - start) * 1000,
                    type="embedding_call",
                    message=f"{type(exc).__name__}: {exc}",
                )
                print(f"  FAIL  {type(exc).__name__}: {exc}", file=sys.stderr)
                return 1
            run.event(
                "ingest",
                outcome="passed",
                duration_ms=(time.monotonic() - start) * 1000,
                type="embedding_call",
                data={
                    "model": report.model,
                    "total": report.total,
                    "embedded": report.embedded,
                    "unchanged": report.unchanged,
                },
                message=f"{report.embedded} embedded, {report.unchanged} unchanged",
            )

        print(f"  total {report.total}  embedded {report.embedded}  unchanged {report.unchanged}")
        print(f"records: {run.dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
