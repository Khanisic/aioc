"""`DocsFindings` and its sub-types (CONTRACTS.md sec 4.2).

Carries the Domain 5 provenance shore-up: claim -> source mapping and coverage-gap
reporting.
"""

from pydantic import Field, model_validator

from ._common import StrictModel
from .primitives import Assessment


class SourceRef(StrictModel):
    document_id: str
    title: str
    chunk_id: str | None = None
    uri: str | None = None
    quote: str | None = None  # verbatim, never paraphrased
    relevance: float | None = None  # retrieval score, 0-1


class Claim(StrictModel):
    """One atomic assertion, individually sourced.

    Invariant: a claim with no sources cannot be ``supported``. Unsupported claims are
    reported so the gap is visible, not so they can be used - the envelope validator keeps
    them out of ``answer.value``.
    """

    id: str
    statement: str
    supported: bool
    sources: list[SourceRef] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check(self) -> "Claim":
        if not self.sources and self.supported:
            raise ValueError(
                "a claim with no sources must have supported=false (CONTRACTS.md sec 4.2)"
            )
        return self


class Coverage(StrictModel):
    """Coverage-gap reporting. ``answered`` and ``unanswered`` partition ``sub_questions``."""

    sub_questions: list[str] = Field(default_factory=list)
    answered: list[str] = Field(default_factory=list)
    unanswered: list[str] = Field(default_factory=list)
    documents_searched: int
    documents_retrieved: int
    documents_cited: int
    corpus_snapshot: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "Coverage":
        sub, ans, un = set(self.sub_questions), set(self.answered), set(self.unanswered)
        if ans & un:
            raise ValueError("answered and unanswered must be disjoint (CONTRACTS.md sec 4.2)")
        if ans | un != sub:
            raise ValueError(
                "answered union unanswered must equal sub_questions (CONTRACTS.md sec 4.2)"
            )
        return self


class DocsFindings(StrictModel):
    answer: Assessment[str]  # synthesized from supported claims only
    claims: list[Claim] = Field(default_factory=list)
    coverage: Coverage
