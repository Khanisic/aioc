"""Retrieval layer over the incident corpus (Day 8): embeddings, ingestion, hybrid search.

The Docs agent consumes this through the `CorpusSearcher.search` seam; ingestion is driven
by `scripts/ingest_embeddings.py`. Everything degrades to lexical-only when no embedding
provider is configured - the offline suite never needs a key.
"""

from __future__ import annotations

from .corpus import (
    CorpusSearcher,
    IngestReport,
    RetrievalResult,
    RetrievedDoc,
    doc_id_for,
    render_document,
)
from .embeddings import (
    Embedder,
    EmbeddingError,
    EmbeddingSettings,
    VoyageEmbedder,
    default_embedder,
)

__all__ = [
    "CorpusSearcher",
    "Embedder",
    "EmbeddingError",
    "EmbeddingSettings",
    "IngestReport",
    "RetrievalResult",
    "RetrievedDoc",
    "VoyageEmbedder",
    "default_embedder",
    "doc_id_for",
    "render_document",
]
