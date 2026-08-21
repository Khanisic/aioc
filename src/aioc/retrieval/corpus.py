"""Ingestion and hybrid search over the incident corpus (Day 8, Platform Layer).

The corpus (`docker/postgres/init/02-incidents.sql`, seeded by `03-seed-incidents.sql`) is
the document store the Docs agent answers from. Each incident row renders to one document -
title, window, services, and the postmortem facts (failure mode, root cause, resolution).
Those are recorded history of *closed* incidents, which is exactly what a "how did we fix
this before" query needs; the guarded ground truth is the *live* chaos signals
(`chaos_knob_value`, the `chaos-*` namespace - see `aioc.tools.policy`), which never appear
in the corpus and so cannot leak through retrieval.

Documents are deliberately one chunk each: the rows are a few hundred words, far below any
useful chunk size, so `SourceRef.chunk_id` stays null and chunking machinery would be
complexity with no retrieval benefit at this corpus size.

Hybrid search is two halves fused with Reciprocal Rank Fusion:

- **lexical** - pg_trgm similarity over title and summary (`incidents_summary_trgm_idx`,
  indexed since Day 5). Always available; needs nothing but the seeded corpus.
- **vector** - pgvector cosine over `incident_embeddings`, filtered by `model` (vectors
  from different models are not comparable). Available once `scripts/ingest_embeddings.py`
  has run with a `VOYAGE_API_KEY`.

RRF rather than score mixing because trgm similarity and cosine similarity live on
incomparable scales; ranks are the only thing they share. The layer degrades honestly: no
embedder, no embeddings, or a provider failure produce a lexical-only result whose
``degraded`` field says why, so the Docs agent can surface reduced coverage instead of
hiding it.

Ids: incidents are `inc_*`; the document ids the contract wants are `doc_*` (CONTRACTS.md
sec 4.2). The mapping `inc_0003 -> doc_0003` is internal to this module - consumers treat
both as opaque.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg

from aioc.tools.incident.store import dsn

from .embeddings import Embedder, EmbeddingError

# Standard RRF constant; rank contributions are 1/(RRF_K + rank).
RRF_K = 60

_INCIDENT_COLUMNS = """
    id, title, summary, started_at, ended_at, affected_services,
    true_severity, true_failure_mode, true_failure_mode_detail,
    true_root_cause, resolution
"""


@dataclass(slots=True, frozen=True)
class IncidentRow:
    """One corpus row: recorded truth about a closed (or ongoing) incident."""

    id: str
    title: str
    summary: str
    started_at: datetime
    ended_at: datetime | None
    affected_services: list[str]
    severity: str
    failure_mode: str
    failure_mode_detail: str | None
    root_cause: str
    resolution: str


@dataclass(slots=True, frozen=True)
class RetrievedDoc:
    """One retrieval hit, ready to be rendered into the Docs agent's prompt."""

    doc_id: str
    incident_id: str
    title: str
    text: str
    relevance: float  # 0-1; the better of the two halves' similarity scores
    uri: str
    lexical_score: float | None
    vector_score: float | None


@dataclass(slots=True, frozen=True)
class RetrievalResult:
    query: str
    docs: list[RetrievedDoc]
    documents_searched: int
    corpus_snapshot: str | None  # ingestion run id, e.g. 'ingest_2026-08-20'; None if lexical
    mode: Literal["hybrid", "lexical"]
    degraded: str | None  # why the vector half was unavailable, when it was


@dataclass(slots=True, frozen=True)
class IngestReport:
    model: str
    total: int
    embedded: int  # new or stale rows that were (re-)embedded this run
    unchanged: int


def doc_id_for(incident_id: str) -> str:
    """`inc_0003` -> `doc_0003`. Internal derivation; both ids stay opaque to consumers."""
    return "doc_" + incident_id.removeprefix("inc_")


def doc_uri_for(incident_id: str) -> str:
    return f"corpus://incidents/{incident_id}"


def render_document(row: IncidentRow) -> str:
    """The exact text that is embedded and that the Docs agent reads.

    One rendering shared by ingestion and retrieval on purpose: the vectors index the same
    words the model quotes from, and `input_sha256` (over this text) detects staleness.
    """

    def ts(value: datetime | None) -> str:
        if value is None:
            return "ongoing"
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    failure_mode = row.failure_mode
    if row.failure_mode_detail:
        failure_mode = f"{failure_mode} ({row.failure_mode_detail})"

    return (
        f"{row.title}\n"
        f"Incident {row.id}, severity {row.severity}, "
        f"{ts(row.started_at)} to {ts(row.ended_at)}.\n"
        f"Affected services: {', '.join(row.affected_services) or 'none recorded'}.\n"
        f"Failure mode: {failure_mode}.\n"
        f"Summary: {row.summary}\n"
        f"Root cause: {row.root_cause}\n"
        f"Resolution: {row.resolution}"
    )


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stale_incident_ids(rendered: dict[str, str], existing: dict[str, str]) -> list[str]:
    """Which incidents need (re-)embedding: new rows, plus rows whose rendered text changed.

    ``rendered`` maps incident id -> current sha256; ``existing`` maps incident id -> the
    sha256 stored alongside its vector. Pure so the idempotence logic is testable offline.
    """
    return sorted(inc_id for inc_id, digest in rendered.items() if existing.get(inc_id) != digest)


def rrf_fuse(
    lexical: list[tuple[str, float]],
    vector: list[tuple[str, float]],
    *,
    k: int,
) -> list[tuple[str, float]]:
    """Fuse two ranked `(id, similarity)` lists into the top-``k`` by Reciprocal Rank Fusion.

    Returns `(id, relevance)` where relevance is the better of the two similarity scores
    (both live in [0, 1]) - RRF decides the *order*, similarity is what gets *reported*,
    because an RRF sum is meaningless outside this function. Ties keep lexicographic id
    order for determinism. Pure so the fusion math is testable offline.
    """
    rrf: dict[str, float] = {}
    best: dict[str, float] = {}
    for ranked in (lexical, vector):
        for rank, (item_id, score) in enumerate(ranked):
            rrf[item_id] = rrf.get(item_id, 0.0) + 1.0 / (RRF_K + rank + 1)
            best[item_id] = max(best.get(item_id, 0.0), min(max(score, 0.0), 1.0))
    ordered = sorted(rrf.items(), key=lambda pair: (-pair[1], pair[0]))
    return [(item_id, best[item_id]) for item_id, _ in ordered[:k]]


def _vector_literal(vector: list[float]) -> str:
    """Render a vector as a pgvector text literal (cast with ``::vector`` in SQL).

    A text literal instead of `pgvector.psycopg.register_vector` so callers can hand this
    module any plain connection - including one mid-transaction in a test - without adapter
    registration as a hidden prerequisite.
    """
    return "[" + ",".join(f"{x:.8g}" for x in vector) + "]"


def _row_to_incident(raw: tuple[Any, ...]) -> IncidentRow:
    return IncidentRow(
        id=raw[0],
        title=raw[1],
        summary=raw[2],
        started_at=raw[3],
        ended_at=raw[4],
        affected_services=list(raw[5] or []),
        severity=raw[6],
        failure_mode=raw[7],
        failure_mode_detail=raw[8],
        root_cause=raw[9],
        resolution=raw[10],
    )


def _embeddings_table_exists(conn: psycopg.Connection[Any]) -> bool:
    # to_regclass never raises, so probing cannot abort a caller's open transaction.
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('incident_embeddings')")
        row = cur.fetchone()
    return bool(row and row[0] is not None)


class CorpusSearcher:
    """Hybrid search over the incident corpus.

    ``embedder`` may be ``None`` (lexical-only - the no-key default). ``conn`` on `search`
    and `ingest` lets tests run inside a transaction they roll back; when omitted, a
    connection is opened per call from the shared store settings (`DATABASE_URL` /
    `POSTGRES_*`, `.env`-aware - the port-collision override rides along).
    """

    def __init__(self, embedder: Embedder | None = None) -> None:
        self._embedder = embedder

    # ------------------------------------------------------------------------- ingestion

    def ingest(self, *, conn: psycopg.Connection[Any] | None = None) -> IngestReport:
        """Embed every new or stale corpus document. Idempotent: unchanged rows are skipped.

        Requires an embedder; ingestion without one is a request that cannot mean anything.
        """
        if self._embedder is None:
            raise EmbeddingError("ingest requires an embedder (set VOYAGE_API_KEY)")
        if conn is not None:
            return self._ingest(conn, self._embedder)
        with psycopg.connect(dsn()) as owned:
            report = self._ingest(owned, self._embedder)
            owned.commit()
            return report

    def _ingest(self, conn: psycopg.Connection[Any], embedder: Embedder) -> IngestReport:
        if not _embeddings_table_exists(conn):
            raise RuntimeError(
                "the incident_embeddings table does not exist - init/ only runs on a fresh "
                "volume, so apply docker/postgres/init/04-embeddings.sql to this database "
                "(see the file header for the psql one-liner) or run make db-reset"
            )
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_INCIDENT_COLUMNS} FROM incidents")  # noqa: S608
            rows = [_row_to_incident(r) for r in cur.fetchall()]
            cur.execute(
                "SELECT incident_id, input_sha256 FROM incident_embeddings WHERE model = %s",
                (embedder.model,),
            )
            existing = {inc_id: digest for inc_id, digest in cur.fetchall()}

        texts = {row.id: render_document(row) for row in rows}
        digests = {inc_id: text_sha256(text) for inc_id, text in texts.items()}
        stale = stale_incident_ids(digests, existing)

        if stale:
            vectors = embedder.embed([texts[i] for i in stale], input_type="document")
            with conn.cursor() as cur:
                for inc_id, vector in zip(stale, vectors, strict=True):
                    cur.execute(
                        """
                        INSERT INTO incident_embeddings
                            (incident_id, model, embedding, input_sha256)
                        VALUES (%s, %s, %s::vector, %s)
                        ON CONFLICT (incident_id, model) DO UPDATE
                            SET embedding = EXCLUDED.embedding,
                                input_sha256 = EXCLUDED.input_sha256,
                                created_at = now()
                        """,
                        (inc_id, embedder.model, _vector_literal(vector), digests[inc_id]),
                    )

        return IngestReport(
            model=embedder.model,
            total=len(rows),
            embedded=len(stale),
            unchanged=len(rows) - len(stale),
        )

    # ---------------------------------------------------------------------------- search

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        conn: psycopg.Connection[Any] | None = None,
    ) -> RetrievalResult:
        if not query.strip():
            raise ValueError("query must be non-empty")
        if conn is not None:
            return self._search(conn, query, k)
        with psycopg.connect(dsn()) as owned:
            return self._search(owned, query, k)

    def _search(self, conn: psycopg.Connection[Any], query: str, k: int) -> RetrievalResult:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM incidents")
            counted = cur.fetchone()
            documents_searched = int(counted[0]) if counted else 0

        lexical = self._lexical_half(conn, query, k)
        vector, snapshot, degraded = self._vector_half(conn, query, k)

        fused = rrf_fuse(lexical, vector, k=k)
        docs = self._fetch_docs(conn, fused, lexical=dict(lexical), vector=dict(vector))
        return RetrievalResult(
            query=query,
            docs=docs,
            documents_searched=documents_searched,
            corpus_snapshot=snapshot,
            mode="hybrid" if vector else "lexical",
            degraded=degraded,
        )

    def _lexical_half(
        self, conn: psycopg.Connection[Any], query: str, k: int
    ) -> list[tuple[str, float]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, greatest(similarity(title, %(q)s), similarity(summary, %(q)s))
                    AS score
                FROM incidents
                WHERE greatest(similarity(title, %(q)s), similarity(summary, %(q)s)) > 0
                ORDER BY score DESC, id
                LIMIT %(k)s
                """,
                {"q": query, "k": k},
            )
            return [(row[0], float(row[1])) for row in cur.fetchall()]

    def _vector_half(
        self, conn: psycopg.Connection[Any], query: str, k: int
    ) -> tuple[list[tuple[str, float]], str | None, str | None]:
        """The vector half, or an empty half with the honest reason it is unavailable."""
        if self._embedder is None:
            return [], None, "no embedder configured (VOYAGE_API_KEY unset); lexical only"
        if not _embeddings_table_exists(conn):
            return [], None, "incident_embeddings table missing (04-embeddings.sql not applied)"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 'ingest_' || to_char(max(created_at) AT TIME ZONE 'UTC', "
                "'YYYY-MM-DD'), count(*) FROM incident_embeddings WHERE model = %s",
                (self._embedder.model,),
            )
            fetched = cur.fetchone()
        if fetched is None or int(fetched[1]) == 0:
            return (
                [],
                None,
                f"no embeddings for model {self._embedder.model!r} "
                "(run scripts/ingest_embeddings.py); lexical only",
            )
        snapshot = str(fetched[0])
        try:
            [query_vector] = self._embedder.embed([query], input_type="query")
        except EmbeddingError as exc:
            return [], None, f"query embedding failed ({exc}); lexical only"
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT incident_id, 1 - (embedding <=> %(v)s::vector) AS score
                FROM incident_embeddings
                WHERE model = %(m)s
                ORDER BY embedding <=> %(v)s::vector, incident_id
                LIMIT %(k)s
                """,
                {"v": _vector_literal(query_vector), "m": self._embedder.model, "k": k},
            )
            return [(row[0], float(row[1])) for row in cur.fetchall()], snapshot, None

    def _fetch_docs(
        self,
        conn: psycopg.Connection[Any],
        fused: list[tuple[str, float]],
        *,
        lexical: dict[str, float],
        vector: dict[str, float],
    ) -> list[RetrievedDoc]:
        if not fused:
            return []
        ids = [inc_id for inc_id, _ in fused]
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {_INCIDENT_COLUMNS} FROM incidents WHERE id = ANY(%s)",  # noqa: S608
                (ids,),
            )
            by_id = {raw[0]: _row_to_incident(raw) for raw in cur.fetchall()}
        docs: list[RetrievedDoc] = []
        for inc_id, relevance in fused:
            row = by_id[inc_id]
            docs.append(
                RetrievedDoc(
                    doc_id=doc_id_for(inc_id),
                    incident_id=inc_id,
                    title=row.title,
                    text=render_document(row),
                    relevance=round(relevance, 4),
                    uri=doc_uri_for(inc_id),
                    lexical_score=lexical.get(inc_id),
                    vector_score=vector.get(inc_id),
                )
            )
        return docs
