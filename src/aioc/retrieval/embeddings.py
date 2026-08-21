"""Embedding provider for the retrieval layer (Day 8, Platform Layer).

Anthropic has no embeddings endpoint, so the vector half of hybrid search rides on an
external provider. Voyage AI is the working assumption from the execution plan; the seam is
the `Embedder` protocol, so swapping providers (or injecting a deterministic fake in tests)
never touches ingestion or search.

The model choice is *the* Day 8 decision because the vector dimension is fixed at
CREATE TABLE time (`docker/postgres/init/04-embeddings.sql`). The default is `voyage-3.5`
at its native 1024 dimensions. `incident_embeddings.model` records which vectors are
comparable, so changing the model later means re-ingesting, never rewriting the corpus.

The whole layer degrades gracefully without a key: `default_embedder()` returns ``None``
when `VOYAGE_API_KEY` is unset, and `CorpusSearcher` falls back to lexical-only search.
Nothing offline ever needs the key - the same standing rule as the Claude key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"

InputType = Literal["document", "query"]


class EmbeddingSettings(BaseSettings):
    """Read from the process environment first and the repo `.env` second, the same
    precedence `aioc.llm.LLMSettings` and the tool stores use."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3].parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Tests construct these settings explicitly by field name; the aliases above are
        # for the environment only.
        populate_by_name=True,
    )

    voyage_api_key: SecretStr | None = Field(default=None, validation_alias="VOYAGE_API_KEY")
    model: str = Field(default="voyage-3.5", validation_alias="AIOC_EMBEDDING_MODEL")
    # Must match the vector(N) column in 04-embeddings.sql. voyage-3.5's native size.
    dim: int = Field(default=1024, validation_alias="AIOC_EMBEDDING_DIM")


class EmbeddingError(RuntimeError):
    """The provider could not produce embeddings (missing key, HTTP failure, bad shape)."""


class Embedder(Protocol):
    """One embedding provider, as ingestion and search see it.

    ``input_type`` matters: retrieval-tuned models embed corpus documents and user queries
    into deliberately different spaces, and mixing them up degrades recall silently.
    """

    @property
    def model(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str], *, input_type: InputType) -> list[list[float]]: ...


class VoyageEmbedder:
    """The Voyage AI embeddings endpoint behind the `Embedder` protocol.

    ``client`` is injectable for tests (an `httpx.Client` with a `MockTransport`), the same
    pattern `aioc.observability.prometheus` uses. One call per `embed` - the whole corpus is
    18 documents, far under Voyage's 128-text batch limit.
    """

    def __init__(
        self,
        settings: EmbeddingSettings | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings or EmbeddingSettings()
        self._client = client

    @property
    def model(self) -> str:
        return self._settings.model

    @property
    def dim(self) -> int:
        return self._settings.dim

    def embed(self, texts: list[str], *, input_type: InputType) -> list[list[float]]:
        if not texts:
            return []
        key = self._settings.voyage_api_key
        if key is None:
            raise EmbeddingError(
                "VOYAGE_API_KEY is not set (shell or .env); vector search is unavailable "
                "without it. Lexical-only retrieval still works."
            )
        payload = {"model": self.model, "input": texts, "input_type": input_type}
        headers = {"Authorization": f"Bearer {key.get_secret_value()}"}
        try:
            if self._client is not None:
                resp = self._client.post(VOYAGE_API_URL, json=payload, headers=headers)
            else:
                resp = httpx.post(VOYAGE_API_URL, json=payload, headers=headers, timeout=30.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Voyage embeddings request failed: {exc}") from exc
        return _parse_embeddings(resp.json(), expected=len(texts), dim=self.dim)


def _parse_embeddings(body: Any, *, expected: int, dim: int) -> list[list[float]]:
    """Validate the response shape loudly - a silently short or misordered list would
    corrupt the vector store in a way nothing downstream can detect."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or len(data) != expected:
        raise EmbeddingError(
            f"Voyage returned {len(data) if isinstance(data, list) else 'no'} embeddings "
            f"for {expected} inputs"
        )
    ordered: list[list[float]] = [[] for _ in range(expected)]
    for item in data:
        if not isinstance(item, dict) or "index" not in item or "embedding" not in item:
            raise EmbeddingError("Voyage response item missing index/embedding")
        vector = [float(x) for x in item["embedding"]]
        if len(vector) != dim:
            raise EmbeddingError(
                f"Voyage returned a {len(vector)}-dimensional vector where {dim} was "
                "expected - the model and the incident_embeddings column disagree"
            )
        ordered[int(item["index"])] = vector
    return ordered


def default_embedder(settings: EmbeddingSettings | None = None) -> Embedder | None:
    """The configured embedder, or ``None`` when no key is set (lexical-only mode)."""
    settings = settings or EmbeddingSettings()
    if settings.voyage_api_key is None:
        return None
    return VoyageEmbedder(settings)
