"""`DocsFindings` and its sub-types (CONTRACTS.md sec 4.2).

Carries the Domain 5 provenance shore-up: claim -> source mapping and coverage-gap
reporting.
"""

import re

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
    reported so the gap is visible, not so they can be used - `DocsFindings` keeps them out
    of ``answer.value``.
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


def _normalised(text: str) -> str:
    """Case-folded, whitespace-collapsed, trailing punctuation dropped - so a statement
    reused in the answer with a changed capital or a lost full stop is still recognised."""
    return re.sub(r"\s+", " ", text).strip().rstrip(".;:!").casefold()


class DocsFindings(StrictModel):
    answer: Assessment[str]  # synthesized from supported claims only
    claims: list[Claim] = Field(default_factory=list)
    coverage: Coverage

    @model_validator(mode="after")
    def _check(self) -> "DocsFindings":
        # Sec 4.2: supported == false claims must not appear in answer.value. Checked the
        # one way that can be checked mechanically - the claim's statement, normalised,
        # inside the normalised answer. A paraphrase of an unsupported claim is beyond any
        # validator; this catches the claim that was reported unsupported and then used
        # word for word anyway.
        if self.answer.value is None:
            return self
        answer = _normalised(self.answer.value)
        for claim in self.claims:
            statement = _normalised(claim.statement)
            if not claim.supported and statement and statement in answer:
                raise ValueError(
                    f"unsupported claim {claim.id} appears in answer.value; an unsupported "
                    "claim is reported so the gap is visible, not so it can be used "
                    "(CONTRACTS.md sec 4.2)"
                )
        return self
