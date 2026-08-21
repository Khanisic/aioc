"""Day 8 retrieval layer: rendering, idempotence logic, fusion math, and hybrid search.

Same split as the tool suites: everything pure - document rendering, stale detection, RRF
fusion, the Voyage response parsing - is tested offline and unmarked; the handful that
query the seeded corpus are marked `integration`. The integration tests that *write*
(ingestion) run inside a transaction that is rolled back, so the shared database is left
exactly as found and no test ever needs the destructive `make db-reset`.

No test here needs a VOYAGE_API_KEY: the fake embedder is deterministic, and the Voyage
client is exercised through an `httpx.MockTransport`. That keeps the standing rule - the
offline suite makes zero paid API calls of any kind.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from aioc.retrieval import (
    CorpusSearcher,
    EmbeddingError,
    EmbeddingSettings,
    VoyageEmbedder,
    default_embedder,
    doc_id_for,
    render_document,
)
from aioc.retrieval.corpus import (
    RRF_K,
    IncidentRow,
    _vector_literal,
    rrf_fuse,
    stale_incident_ids,
    text_sha256,
)
from aioc.retrieval.embeddings import InputType, _parse_embeddings

# --------------------------------------------------------------------------- fakes


class FakeEmbedder:
    """Deterministic 1024-dim embedder: the vector encodes which marker words appear, so
    similarity is exact-by-construction and tests can predict rankings."""

    MARKERS = ("memory", "pool", "latency", "deploy")

    def __init__(self, model: str = "fake-embedder") -> None:
        self.model = model
        self.dim = 1024
        self.calls: list[tuple[list[str], str]] = []

    def embed(self, texts: list[str], *, input_type: InputType) -> list[list[float]]:
        self.calls.append((list(texts), input_type))
        out: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dim
            lowered = text.lower()
            for i, marker in enumerate(self.MARKERS):
                if marker in lowered:
                    vector[i] = 1.0
            if not any(vector):
                vector[-1] = 1.0  # never the zero vector; cosine needs a direction
            out.append(vector)
        return out


def _row(
    incident_id: str = "inc_9001",
    *,
    title: str = "payments-api resident memory climbed",
    ended: bool = True,
) -> IncidentRow:
    return IncidentRow(
        id=incident_id,
        title=title,
        summary="RSS grew steadily until the container was OOM-killed.",
        started_at=datetime(2026, 1, 14, 12, 5, tzinfo=UTC),
        ended_at=datetime(2026, 1, 14, 14, 40, tzinfo=UTC) if ended else None,
        affected_services=["payments-api", "checkout-api"],
        severity="sev2",
        failure_mode="resource_exhaustion",
        failure_mode_detail=None,
        root_cause="An in-memory cache had no eviction policy.",
        resolution="Added an LRU bound and a TTL to the cache.",
    )


# ----------------------------------------------------------------- document rendering


def test_render_document_carries_the_postmortem_facts():
    text = render_document(_row())
    assert "payments-api resident memory climbed" in text
    assert "Incident inc_9001, severity sev2" in text
    assert "2026-01-14T12:05:00Z to 2026-01-14T14:40:00Z" in text
    assert "payments-api, checkout-api" in text
    assert "Failure mode: resource_exhaustion." in text
    assert "Root cause: An in-memory cache had no eviction policy." in text
    assert "Resolution: Added an LRU bound and a TTL to the cache." in text


def test_render_document_marks_an_ongoing_incident():
    assert "to ongoing" in render_document(_row(ended=False))


def test_render_document_expands_an_other_failure_mode_detail():
    row = replace(_row(), failure_mode="other", failure_mode_detail="certificate expiry")
    assert "Failure mode: other (certificate expiry)." in render_document(row)


def test_doc_id_mapping_is_prefix_swap():
    assert doc_id_for("inc_0003") == "doc_0003"


# ------------------------------------------------------------------- ingest idempotence


def test_stale_detection_picks_new_and_changed_rows_only():
    rendered = {"inc_1": "aaa", "inc_2": "bbb", "inc_3": "ccc"}
    existing = {"inc_1": "aaa", "inc_2": "STALE"}
    assert stale_incident_ids(rendered, existing) == ["inc_2", "inc_3"]


def test_identical_corpus_is_fully_unchanged():
    rendered = {"inc_1": text_sha256("same"), "inc_2": text_sha256("also")}
    assert stale_incident_ids(rendered, dict(rendered)) == []


# --------------------------------------------------------------------------- RRF fusion


def test_rrf_prefers_a_doc_ranked_by_both_halves():
    lexical = [("inc_a", 0.4), ("inc_b", 0.9)]
    vector = [("inc_a", 0.8), ("inc_c", 0.7)]
    fused = rrf_fuse(lexical, vector, k=3)
    ids = [i for i, _ in fused]
    # inc_a appears in both halves, so its RRF sum beats either single-half doc.
    assert ids[0] == "inc_a"
    assert set(ids) == {"inc_a", "inc_b", "inc_c"}


def test_rrf_reports_the_better_similarity_not_the_rrf_sum():
    fused = dict(rrf_fuse([("inc_a", 0.4)], [("inc_a", 0.8)], k=1))
    assert fused["inc_a"] == 0.8  # not 2/(RRF_K+1)


def test_rrf_ties_break_deterministically_by_id():
    fused = rrf_fuse([("inc_b", 0.5)], [("inc_a", 0.5)], k=2)
    assert [i for i, _ in fused] == ["inc_a", "inc_b"]


def test_rrf_truncates_to_k():
    lexical = [(f"inc_{i}", 0.5) for i in range(10)]
    assert len(rrf_fuse(lexical, [], k=3)) == 3


def test_rrf_clamps_reported_relevance_into_unit_range():
    fused = dict(rrf_fuse([("inc_a", 1.7)], [], k=1))
    assert fused["inc_a"] == 1.0


def test_rrf_constant_is_the_standard_sixty():
    assert RRF_K == 60


# ------------------------------------------------------------------ vector literal


def test_vector_literal_is_pgvector_syntax():
    assert _vector_literal([0.0, 1.0, -0.25]) == "[0,1,-0.25]"


# ------------------------------------------------------------------ voyage client


def _voyage_response(vectors: list[list[float]]) -> dict[str, Any]:
    return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]}


def _settings(key: str | None = "test-key", dim: int = 3) -> EmbeddingSettings:
    # Explicit values so the developer's real .env can never leak into a test.
    return EmbeddingSettings(
        voyage_api_key=key,  # type: ignore[arg-type]
        model="voyage-3.5",
        dim=dim,
    )


def test_voyage_embedder_posts_model_input_and_input_type():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["json"] = json.loads(request.content)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_voyage_response([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = VoyageEmbedder(_settings(), client=client)
    vectors = embedder.embed(["doc one", "doc two"], input_type="document")

    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert seen["json"] == {
        "model": "voyage-3.5",
        "input": ["doc one", "doc two"],
        "input_type": "document",
    }
    assert seen["auth"] == "Bearer test-key"


def test_voyage_embedder_without_a_key_raises_and_names_the_fallback():
    with pytest.raises(EmbeddingError, match="VOYAGE_API_KEY"):
        VoyageEmbedder(_settings(key=None)).embed(["x"], input_type="query")


def test_voyage_embedder_rejects_a_wrong_dimension_loudly():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_voyage_response([[1.0, 2.0]]))  # 2-dim, not 3

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(EmbeddingError, match="2-dimensional vector where 3"):
        VoyageEmbedder(_settings(), client=client).embed(["x"], input_type="document")


def test_voyage_embedder_rejects_a_short_response_loudly():
    with pytest.raises(EmbeddingError, match="1 embeddings for 2"):
        _parse_embeddings(_voyage_response([[1.0, 0.0, 0.0]]), expected=2, dim=3)


def test_voyage_embedder_reorders_by_index():
    body = {
        "data": [
            {"index": 1, "embedding": [0.0, 1.0, 0.0]},
            {"index": 0, "embedding": [1.0, 0.0, 0.0]},
        ]
    }
    assert _parse_embeddings(body, expected=2, dim=3) == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


def test_voyage_http_failure_becomes_embedding_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": "rate limited"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(EmbeddingError, match="request failed"):
        VoyageEmbedder(_settings(), client=client).embed(["x"], input_type="query")


def test_settings_env_file_is_the_repo_root_dotenv():
    # Regression: this path was copied from tools/incident/store.py, which sits one
    # directory deeper, so it silently pointed one level above the repo - and a key the
    # user had actually set read back as "not set". Pin it to where pyproject.toml lives.
    env_file = EmbeddingSettings.model_config["env_file"]
    assert isinstance(env_file, Path)
    assert env_file.name == ".env"
    assert (env_file.parent / "pyproject.toml").is_file()


def test_default_embedder_is_none_without_a_key():
    assert default_embedder(_settings(key=None)) is None


def test_default_embedder_exists_with_a_key():
    embedder = default_embedder(_settings())
    assert embedder is not None and embedder.model == "voyage-3.5"


def test_empty_input_embeds_to_empty_without_a_network_call():
    assert VoyageEmbedder(_settings(key=None)).embed([], input_type="document") == []


def test_search_rejects_an_empty_query():
    with pytest.raises(ValueError, match="query must be non-empty"):
        CorpusSearcher().search("   ")


# ------------------------------------------------------------------------- integration


@pytest.fixture(autouse=True)
def _require_reachable_store(request: pytest.FixtureRequest) -> None:
    """Skip integration tests when the corpus is unreachable - same diagnosis as the
    timeline suite, which is where the port-collision story is written down."""
    if "integration" not in request.keywords:
        return

    import psycopg

    from aioc.tools.incident.store import dsn

    try:
        with psycopg.connect(dsn(), connect_timeout=3):
            return
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"incident corpus unreachable ({str(exc).splitlines()[0]}); see "
            "tests/test_timeline_tool.py for the port-collision diagnosis"
        )


@pytest.fixture
def rollback_conn():
    """A connection whose transaction is always rolled back - ingestion tests write through
    it and the shared database is left exactly as found."""
    import psycopg

    from aioc.tools.incident.store import dsn

    with psycopg.connect(dsn()) as conn:
        _ensure_embeddings_table(conn)
        yield conn
        conn.rollback()


def _ensure_embeddings_table(conn: Any) -> None:
    """Apply 04-embeddings.sql inside the (rolled-back) transaction when the live database
    predates Day 8 - init/ only runs on a fresh volume, and this keeps the test
    self-sufficient either way."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('incident_embeddings')")
        row = cur.fetchone()
        if row and row[0] is not None:
            return
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[1] / "docker" / "postgres" / "init" / "04-embeddings.sql"
    ).read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)


@pytest.mark.integration
def test_lexical_search_finds_the_pool_exhaustion_incident(rollback_conn: Any) -> None:
    result = CorpusSearcher().search("connection pool exhausted", k=3, conn=rollback_conn)
    assert result.mode == "lexical"
    assert result.degraded is not None  # honest about the missing vector half
    assert result.documents_searched >= 18
    assert result.docs, "the seeded corpus has a pool-exhaustion incident to find"
    top = result.docs[0]
    assert "pool" in top.text.lower()
    assert top.doc_id.startswith("doc_")
    assert top.incident_id.startswith("inc_")
    assert 0.0 <= top.relevance <= 1.0


@pytest.mark.integration
def test_ingest_is_idempotent_and_enables_hybrid_search(rollback_conn: Any) -> None:
    embedder = FakeEmbedder()
    searcher = CorpusSearcher(embedder)

    first = searcher.ingest(conn=rollback_conn)
    assert first.total >= 18
    assert first.embedded == first.total and first.unchanged == 0

    second = searcher.ingest(conn=rollback_conn)
    assert second.embedded == 0 and second.unchanged == second.total

    result = searcher.search("memory leak OOM kill", k=4, conn=rollback_conn)
    assert result.mode == "hybrid"
    assert result.degraded is None
    assert result.corpus_snapshot is not None and result.corpus_snapshot.startswith("ingest_")
    assert any(d.vector_score is not None for d in result.docs)
    # The fake embedder keys on marker words, so a memory query surfaces a memory incident.
    assert any("memory" in d.text.lower() for d in result.docs)


@pytest.mark.integration
def test_hybrid_search_degrades_to_lexical_when_the_provider_fails(
    rollback_conn: Any,
) -> None:
    class FailingEmbedder(FakeEmbedder):
        def embed(self, texts: list[str], *, input_type: InputType) -> list[list[float]]:
            if input_type == "query":
                raise EmbeddingError("provider down")
            return super().embed(texts, input_type=input_type)

    searcher = CorpusSearcher(FailingEmbedder())
    searcher.ingest(conn=rollback_conn)
    result = searcher.search("connection pool exhausted", k=3, conn=rollback_conn)
    assert result.mode == "lexical"
    assert result.degraded is not None and "provider down" in result.degraded
    assert result.docs, "the lexical half still answers"
