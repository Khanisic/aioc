"""Docs provenance (Day 18): every claim traced to its sources, and every unanswered
sub-question traced to the gap that reports it.

CONTRACTS.md sec 4.2 says `DocsFindings` "carries the Day 18 Domain 5 shore-up: claim ->
source mapping and coverage-gap reporting", and the shapes have carried it since Day 1:
each `Claim` lists its `SourceRef`s with a verbatim quote, `Coverage` partitions the
question into answered and unanswered, and (since Day 16) every unanswered entry must have
its own `Gap`. The Docs agent (Day 8) grounds all of it in code - an uncited document or a
paraphrased quote is refused before the report exists. What was missing is the reading:
the chain from a claim to the retrieval that produced its source, and from an unanswered
sub-question to the gap the coordinator will act on, resolved once and rendered where a
person or a downstream agent looks.

**The chain is resolved, not restated.** A claim's `SourceRef` names a document (and
usually a chunk); the response's `Evidence` entries of type ``document`` name the same
documents in ``source_ref`` and carry the ``tool_call_id`` of the retrieval that returned
them. `provenance` joins the two, so each `ClaimSource` says which evidence ids and which
tool call stand behind it - a chunk match when both sides name a chunk, the document
otherwise. A claim whose sources match no evidence entry is not an error (the Docs agent
cites documents in claims and metrics or context in evidence independently); it is shown
with no evidence ids, which is the honest reading.

**Coverage gaps are matched by index first, then in order.** A gap's ``blocks_field`` may
name the entry (``findings.coverage.unanswered[1]``) or the field; indexed gaps are taken
first, the rest are assigned to the remaining unanswered questions in report order. The
contract guarantees at least one gap per unanswered entry, so every `CoverageGap` has one
unless a report is malformed - and then ``gap_id`` is ``None`` rather than a guess.

Nothing here changes a frozen shape; the digest and the free report script are the readers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from aioc.contracts import AgentName, DocsAgentResponse, Evidence, Gap, SourceType

from .confidence import Band, band

UNANSWERED_FIELD = "findings.coverage.unanswered"
_INDEXED = re.compile(rf"^{re.escape(UNANSWERED_FIELD)}\[(\d+)\]$")


@dataclass(frozen=True, slots=True)
class ClaimSource:
    document_id: str
    title: str
    chunk_id: str | None
    uri: str | None
    quote: str | None
    relevance: float | None
    evidence_ids: tuple[str, ...]  # the response's evidence entries for this document/chunk
    tool_call_ids: tuple[str, ...]  # the retrieval calls those entries came from

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "chunk_id": self.chunk_id,
            "uri": self.uri,
            "quote": self.quote,
            "relevance": self.relevance,
            "evidence_ids": list(self.evidence_ids),
            "tool_call_ids": list(self.tool_call_ids),
        }


@dataclass(frozen=True, slots=True)
class ClaimProvenance:
    claim_id: str
    statement: str
    supported: bool
    confidence: float
    band: Band
    sources: tuple[ClaimSource, ...]
    backs_answer: bool  # one of its documents is cited by answer.evidence

    @property
    def document_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(s.document_id for s in self.sources))

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "statement": self.statement,
            "supported": self.supported,
            "confidence": self.confidence,
            "band": self.band.value,
            "backs_answer": self.backs_answer,
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass(frozen=True, slots=True)
class CoverageGap:
    sub_question: str
    gap_id: str | None
    description: str | None
    resolvable: bool | None
    suggested_agent: AgentName | None
    suggested_query: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub_question": self.sub_question,
            "gap_id": self.gap_id,
            "description": self.description,
            "resolvable": self.resolvable,
            "suggested_agent": self.suggested_agent.value if self.suggested_agent else None,
            "suggested_query": self.suggested_query,
        }


@dataclass(frozen=True, slots=True)
class CoverageReport:
    sub_questions: tuple[str, ...]
    answered: tuple[str, ...]
    unanswered: tuple[CoverageGap, ...]
    documents_searched: int
    documents_retrieved: int
    documents_cited: int
    cited_documents: tuple[str, ...]  # the ids behind documents_cited
    corpus_snapshot: str | None

    @property
    def answered_ratio(self) -> float | None:
        return len(self.answered) / len(self.sub_questions) if self.sub_questions else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub_questions": list(self.sub_questions),
            "answered": list(self.answered),
            "unanswered": [g.to_dict() for g in self.unanswered],
            "documents_searched": self.documents_searched,
            "documents_retrieved": self.documents_retrieved,
            "documents_cited": self.documents_cited,
            "cited_documents": list(self.cited_documents),
            "corpus_snapshot": self.corpus_snapshot,
        }


@dataclass(frozen=True, slots=True)
class DocsProvenance:
    request_id: str
    invocation_id: str
    answer_evidence: tuple[str, ...]
    answer_documents: tuple[str, ...]  # documents those evidence ids resolve to
    claims: tuple[ClaimProvenance, ...]
    coverage: CoverageReport

    @property
    def supported(self) -> tuple[ClaimProvenance, ...]:
        return tuple(c for c in self.claims if c.supported)

    @property
    def unsupported(self) -> tuple[ClaimProvenance, ...]:
        return tuple(c for c in self.claims if not c.supported)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "invocation_id": self.invocation_id,
            "answer_evidence": list(self.answer_evidence),
            "answer_documents": list(self.answer_documents),
            "claims": [c.to_dict() for c in self.claims],
            "coverage": self.coverage.to_dict(),
        }

    def render(self) -> list[str]:
        cov = self.coverage
        lines = [
            f"docs {self.invocation_id}: {len(self.supported)} supported claim(s), "
            f"{len(self.unsupported)} unsupported; answer cites "
            f"{', '.join(self.answer_evidence) or 'nothing'}"
            + (f" -> {', '.join(self.answer_documents)}" if self.answer_documents else "")
        ]
        for c in self.claims:
            mark = "supported" if c.supported else "UNSUPPORTED"
            backs = ", backs the answer" if c.backs_answer else ""
            lines.append(
                f"  {c.claim_id} [{mark} @{c.confidence:.2f} {c.band.value}{backs}]: {c.statement}"
            )
            for s in c.sources:
                where = s.chunk_id or s.document_id
                via = (
                    f" via {', '.join(s.evidence_ids)}"
                    if s.evidence_ids
                    else " (no evidence entry)"
                )
                calls = f" from {', '.join(s.tool_call_ids)}" if s.tool_call_ids else ""
                quote = f' "{s.quote}"' if s.quote else ""
                lines.append(f"    <- {where} ({s.title}){via}{calls}{quote}")
        ratio = f" ({cov.answered_ratio:.0%})" if cov.answered_ratio is not None else ""
        lines.append(
            f"  coverage: {len(cov.answered)}/{len(cov.sub_questions)} sub-question(s) "
            f"answered{ratio}; searched {cov.documents_searched}, retrieved "
            f"{cov.documents_retrieved}, cited {cov.documents_cited}"
            + (f" ({', '.join(cov.cited_documents)})" if cov.cited_documents else "")
            + (f"; snapshot {cov.corpus_snapshot}" if cov.corpus_snapshot else "")
        )
        for q in cov.answered:
            lines.append(f"    answered: {q}")
        for g in cov.unanswered:
            if g.gap_id is None:
                lines.append(f"    UNANSWERED: {g.sub_question}  <- no gap reports it")
                continue
            flag = "resolvable" if g.resolvable else "unresolvable"
            hint = (
                f' -> {g.suggested_agent.value}: "{g.suggested_query}"' if g.suggested_agent else ""
            )
            lines.append(f"    UNANSWERED: {g.sub_question}  <- {g.gap_id} [{flag}]{hint}")
        return lines


# -------------------------------------------------------------------------- the reading


def provenance(response: DocsAgentResponse) -> DocsProvenance:
    f = response.findings
    by_doc, by_chunk = _document_evidence(response.evidence)
    answer_docs = tuple(
        dict.fromkeys(
            _doc_of(e.source_ref)
            for e in response.evidence
            if e.id in set(f.answer.evidence) and e.source_type is SourceType.DOCUMENT
        )
    )
    claims = tuple(_claim(c, by_doc, by_chunk, set(answer_docs)) for c in f.claims)
    cited = tuple(dict.fromkeys(d for c in claims for d in c.document_ids))
    return DocsProvenance(
        request_id=response.request_id,
        invocation_id=response.invocation_id,
        answer_evidence=tuple(f.answer.evidence),
        answer_documents=answer_docs,
        claims=claims,
        coverage=CoverageReport(
            sub_questions=tuple(f.coverage.sub_questions),
            answered=tuple(f.coverage.answered),
            unanswered=coverage_gaps(f.coverage.unanswered, response.gaps),
            documents_searched=f.coverage.documents_searched,
            documents_retrieved=f.coverage.documents_retrieved,
            documents_cited=f.coverage.documents_cited,
            cited_documents=cited,
            corpus_snapshot=f.coverage.corpus_snapshot,
        ),
    )


def coverage_gaps(unanswered: list[str], gaps: list[Gap]) -> tuple[CoverageGap, ...]:
    """Each unanswered sub-question with the gap that reports it: an indexed
    ``blocks_field`` wins, the rest are assigned in order, and a question no gap reports
    gets ``None`` rather than somebody else's gap."""
    indexed: dict[int, Gap] = {}
    unindexed: list[Gap] = []
    for g in gaps:
        if g.blocks_field is None:
            continue
        m = _INDEXED.match(g.blocks_field)
        if m:
            indexed.setdefault(int(m.group(1)), g)
        elif g.blocks_field == UNANSWERED_FIELD:
            unindexed.append(g)
    out: list[CoverageGap] = []
    for i, question in enumerate(unanswered):
        gap = indexed.get(i)
        if gap is None and unindexed:
            gap = unindexed.pop(0)
        out.append(
            CoverageGap(
                sub_question=question,
                gap_id=gap.id if gap else None,
                description=gap.description if gap else None,
                resolvable=gap.resolvable if gap else None,
                suggested_agent=gap.suggested_agent if gap else None,
                suggested_query=gap.suggested_query if gap else None,
            )
        )
    return tuple(out)


# ------------------------------------------------------------------------------ helpers


def _doc_of(ref: str) -> str:
    """``doc_012#7`` -> ``doc_012``; the chunk suffix is optional in a source_ref."""
    return ref.split("#", 1)[0]


def _document_evidence(
    evidence: list[Evidence],
) -> tuple[dict[str, list[Evidence]], dict[str, list[Evidence]]]:
    by_doc: dict[str, list[Evidence]] = {}
    by_chunk: dict[str, list[Evidence]] = {}
    for e in evidence:
        if e.source_type is not SourceType.DOCUMENT:
            continue
        by_doc.setdefault(_doc_of(e.source_ref), []).append(e)
        if "#" in e.source_ref:
            by_chunk.setdefault(e.source_ref, []).append(e)
    return by_doc, by_chunk


def _claim(
    c: Any,
    by_doc: dict[str, list[Evidence]],
    by_chunk: dict[str, list[Evidence]],
    answer_docs: set[str],
) -> ClaimProvenance:
    sources: list[ClaimSource] = []
    for s in c.sources:
        entries = (by_chunk.get(s.chunk_id, []) if s.chunk_id else []) or by_doc.get(
            s.document_id, []
        )
        sources.append(
            ClaimSource(
                document_id=s.document_id,
                title=s.title,
                chunk_id=s.chunk_id,
                uri=s.uri,
                quote=s.quote,
                relevance=s.relevance,
                evidence_ids=tuple(e.id for e in entries),
                tool_call_ids=tuple(
                    dict.fromkeys(e.tool_call_id for e in entries if e.tool_call_id)
                ),
            )
        )
    docs = {s.document_id for s in sources}
    return ClaimProvenance(
        claim_id=c.id,
        statement=c.statement,
        supported=c.supported,
        confidence=c.confidence,
        band=band(c.confidence),
        sources=tuple(sources),
        backs_answer=bool(docs & answer_docs),
    )
