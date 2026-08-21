"""Docs agent (Day 8): answers from the retrieved corpus only, citing every claim.

Follows `incident.py`'s structured pattern exactly - a single forced structured-output tool
whose JSON Schema is generated from the frozen models and annotated with the contract's
cross-field rules - with one addition: **retrieval happens before the model call**, in this
process, and the retrieved documents are rendered into the prompt alongside the
coordinator's explicit context. The model never fetches anything itself; what it can cite
is exactly what retrieval returned, which is what makes "answers from retrieved documents
only" (CONTRACTS.md sec 4.2) enforceable rather than aspirational.

Enforced in code, not just prompted:

- **Constrained to retrieved documents.** Every `document_id` the model cites - in claim
  sources and in evidence `source_ref`s - must be one retrieval actually returned; anything
  else raises `DocsAgentError`. A hallucinated source is the one failure a Docs agent must
  not have.
- **Quotes are verbatim.** A claim source's `quote` and an evidence `excerpt` must appear
  in the retrieved text (whitespace-normalised); a paraphrase raises.
- **Coverage accounting is stamped, not asked for.** `documents_searched`,
  `documents_retrieved`, `documents_cited`, and `corpus_snapshot` are facts this process
  measured, so the model is asked only for the analytic half of `Coverage` (the
  sub-question decomposition) and the runtime fills the rest - the same reasoning that
  stopped asking the planner model for `round` (war story #7). Likewise every document
  evidence entry gets the real retrieval `ToolCallRef` id stamped into `tool_call_id`.
- **Unanswered sub-questions need a `Gap`.** Validated on `DocsAgentResponse` itself
  (contracts layer), with the gap's `blocks_field` naming `findings.coverage.unanswered`.

The retrieval call is recorded honestly in `tool_calls` as `search_corpus` on
`aioc-docs` - the same shape the contract's sec 8 worked example shows - with measured
duration and an estimated token count. A retrieval *failure* propagates as an exception:
the executor already turns a failed invocation into a resolvable `Gap`, and fabricating a
degraded response here would duplicate that honesty less well.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from anthropic.types import ToolUseBlock
from pydantic import Field

from aioc.contracts import (
    Assessment,
    Claim,
    Coverage,
    DocsAgentResponse,
    DocsFindings,
    Evidence,
    Gap,
    ResponseStatus,
    SourceType,
    StrictModel,
    ToolCallRef,
)
from aioc.llm import LLMClient, ToolResult, ToolSpec, Usage
from aioc.retrieval import CorpusSearcher, RetrievalResult, default_embedder

from ._annotate import ROOT, apply_guidance
from .incident import _CONFIDENCE_BANDS

AGENT_NAME = "docs"

EMIT_TOOL_NAME = "emit_docs_report"

DEFAULT_TOP_K = 5

_GROUND_RULES = f"""\
You are the Docs agent of AIOC, an AI operations center. You are an expert technical
librarian for a site-reliability organisation: you answer questions about procedures,
past incidents, root causes, and resolutions from the documentation corpus.

Ground rules:

1. Answer ONLY from the retrieved documents provided in the message. You inherit nothing
   and you know nothing beyond those documents - your own general knowledge is exactly what
   you must not answer from. If the documents do not contain the answer, say so.
2. Cite a document for every claim, quoting the supporting passage VERBATIM - never
   paraphrase inside a quote. A claim with no citable document is unsupported and must be
   marked as such; unsupported claims must not shape your answer.
3. Estimate confidence for every conclusion as a number from 0.0 to 1.0, calibrated
   against these bands:

{_CONFIDENCE_BANDS}

   Below 0.25 means you must not state the conclusion at all - record it as a gap instead.
4. Distinguish "the corpus says nothing about this" from "retrieval returned nothing
   usable". They are different findings and both are worth reporting.
5. Never repeat configuration VALUES (connection strings, tokens, passwords) even if they
   appear in a document. Refer to configuration by key name only."""


# ------------------------------------------------------------------- structured output


class ReportedCoverage(StrictModel):
    """The analytic half of the contract's `Coverage` - the sub-question decomposition.

    The counting half (`documents_searched` / `documents_retrieved` / `documents_cited` /
    `corpus_snapshot`) is measured by the runtime and stamped after validation; asking the
    model to echo numbers this process already knows is a field it can only get wrong.
    """

    sub_questions: list[str] = Field(default_factory=list)
    answered: list[str] = Field(default_factory=list)
    unanswered: list[str] = Field(default_factory=list)


class ReportedFindings(StrictModel):
    """`DocsFindings` minus the stamped coverage counters."""

    answer: Assessment[str]
    claims: list[Claim] = Field(default_factory=list)
    coverage: ReportedCoverage


class DocsReport(StrictModel):
    """The payload the model owns - the `DocsAgentResponse` envelope minus the plumbing the
    caller fills in (ids, timestamps, `tool_calls`, and the measured coverage counters)."""

    status: ResponseStatus
    status_detail: str | None = None
    summary: str
    findings: ReportedFindings
    evidence: list[Evidence] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)


_TOP_LEVEL_DESCRIPTION = """\
The complete documentation report. Every property below is a top-level argument of this
tool - pass them directly; do not nest them inside a wrapper object.

Rules validated after you answer - a violation rejects the whole report:
1. Every `document_id` you cite must be one of the retrieved documents. Never invent one.
2. Every `quote` and evidence `excerpt` must appear verbatim in the retrieved text.
3. A claim with no sources must have `supported: false`, and unsupported claims must not
   shape `answer.value`.
4. Every sub-question you list as `unanswered` requires a gap whose `blocks_field` is
   `findings.coverage.unanswered`.
5. Any `*_detail` field must be null unless its partner field is exactly `other`."""

_FIELD_GUIDANCE: dict[str, dict[str, str]] = {
    ROOT: {
        "status": (
            "`complete` only when every sub-question is answered and `answer.value` is "
            "non-null. `partial` when some sub-questions are unanswered. "
            "`insufficient_evidence` when the documents establish almost nothing."
        ),
        "status_detail": (
            "Null unless `status` is `other`. Do not describe a `partial` status here."
        ),
        "summary": "One or two sentences: what the documentation does and does not answer.",
        "evidence": (
            "One entry per distinct passage you rely on, each with a verbatim `excerpt` "
            "from a retrieved document. Every evidence id cited by `answer` must appear "
            "here. Cite nothing you cannot quote."
        ),
        "gaps": (
            "What the corpus could not establish. Required for every unanswered "
            "sub-question - set that gap's `blocks_field` to `findings.coverage.unanswered`."
        ),
        "overall_confidence": "Your confidence in the report as a whole, on the band table.",
    },
    "ReportedFindings": {
        "answer": (
            "The synthesized answer, built from SUPPORTED claims only. Null value if the "
            "documents do not answer the question."
        ),
        "claims": (
            "One atomic assertion each - no conjunctions. Split a compound statement into "
            "separate claims so each can carry its own sources."
        ),
        "coverage": "Decompose the query into sub-questions and classify each.",
    },
    "ReportedCoverage": {
        "sub_questions": "The query decomposed into the distinct questions it contains.",
        "answered": "The subset of `sub_questions` the documents answer.",
        "unanswered": (
            "The subset the documents do not answer. Each entry here requires a matching "
            "gap. `answered` and `unanswered` together must exactly cover `sub_questions`."
        ),
    },
    "Assessment_str_": {
        "value": "Free text synthesized from supported claims, or null below 0.25 confidence.",
        "confidence": "Calibrated to the band table in the system prompt.",
        "evidence": "Ids of the evidence entries supporting this, all present in `evidence`.",
        "reasoning": "One line: how the cited documents lead to this answer.",
        "detail": "Always null here. This value is free text, so it has no `other` member.",
    },
    "Claim": {
        "id": "Opaque id starting `claim_`, unique within this report.",
        "statement": "One assertion. No conjunctions.",
        "supported": (
            "True only when `sources` is non-empty. An unsupported claim is reported so "
            "the gap is visible, not so it can be used in the answer."
        ),
        "sources": "The retrieved documents that state this, each with a verbatim quote.",
        "confidence": "Calibrated to the band table in the system prompt.",
    },
    "SourceRef": {
        "document_id": "The `id` of a retrieved document, exactly as given. Never invent one.",
        "title": "The document's title, exactly as given.",
        "chunk_id": "Always null - corpus documents are single-chunk.",
        "uri": "The document's uri, exactly as given.",
        "quote": "The supporting passage, VERBATIM from the document. Never paraphrase.",
        "relevance": "The document's retrieval relevance, exactly as given.",
    },
    "Evidence": {
        "id": "Opaque id starting `ev_`, referenced by `answer.evidence`.",
        "source_type": "`document` for anything quoted from a retrieved document.",
        "source_type_detail": "Null unless `source_type` is exactly `other`.",
        "source_ref": "The id of the document the excerpt comes from.",
        "excerpt": "Quoted verbatim from the retrieved document. Never paraphrase or invent.",
        "uri": "The document's uri, exactly as given.",
        "tool_call_id": "Leave null - the runtime records the real retrieval call id.",
    },
    "Gap": {
        "kind_detail": "Null unless `kind` is exactly `other`.",
        "blocks_field": (
            "`findings.coverage.unanswered` for a gap explaining an unanswered "
            "sub-question; `findings.answer.value` when the whole answer is blocked."
        ),
        "resolvable": (
            "True only if another agent or more documents could close this gap. False "
            "stops the coordinator's refinement loop, so set it honestly."
        ),
        "suggested_query": "The question to ask next, when `resolvable` is true.",
    },
}


def _apply_guidance(schema: dict[str, Any]) -> dict[str, Any]:
    return apply_guidance(
        schema,
        name="docs emit",
        description=_TOP_LEVEL_DESCRIPTION,
        guidance=_FIELD_GUIDANCE,
    )


# Generated once at import from the frozen models - never hand-written, so it cannot drift.
_EMIT_SCHEMA: dict[str, Any] = _apply_guidance(DocsReport.model_json_schema())

DOCS_STRUCTURED_SYSTEM_PROMPT = f"""\
{_GROUND_RULES}

Report your findings by calling the `{EMIT_TOOL_NAME}` tool exactly once. Do not write any
prose outside the tool call. Fill its fields as follows:

- Decompose the query into `coverage.sub_questions` and classify every one as `answered`
  or `unanswered`. Every unanswered sub-question REQUIRES a gap whose `blocks_field` is
  `findings.coverage.unanswered`.
- State what the documents establish as `claims` - one atomic assertion each, every one
  citing the retrieved documents that state it, with the supporting passage quoted
  VERBATIM. A claim you cannot source gets `supported: false` and an empty `sources` list.
- Synthesize `answer` from the SUPPORTED claims only; give a `confidence` in [0, 1] using
  the bands above and cite the evidence ids that back it. If the documents do not answer
  the question, set `answer.value` to null and record a gap naming
  `findings.answer.value`.
- Every evidence entry carries a verbatim `excerpt`, `source_type` `document`, and the
  document id as `source_ref`. Leave `tool_call_id` null; the runtime fills it.
- Set `status` to `complete` only when everything is answered; `partial` when some
  sub-questions are not; `insufficient_evidence` when the documents establish almost
  nothing (that is a finding, not a failure - report it plainly).
- For any enum, when no member fits, use `other` and put the specifics in that field's
  `detail`; leave `detail` null otherwise.

Set `overall_confidence` to your confidence in the report as a whole."""


class DocsAgentError(RuntimeError):
    """The model did not return usable structured output, or its output cited sources it
    was never given. A malformed-but-present payload raises pydantic's ``ValidationError``
    instead."""


def _emit_never_runs(_args: dict[str, Any]) -> ToolResult:
    raise DocsAgentError(f"{EMIT_TOOL_NAME} is a structured-output tool; it is never executed")


_EMIT_TOOL = ToolSpec(
    name=EMIT_TOOL_NAME,
    description=(
        "Emit the documentation report as structured data. Call this exactly once; it is "
        "the only way to answer. Claims cite retrieved documents with verbatim quotes; the "
        "answer is synthesized from supported claims only; unanswered sub-questions carry "
        "matching gaps."
    ),
    input_schema=_EMIT_SCHEMA,
    handler=_emit_never_runs,
)


class CorpusRetriever(Protocol):
    """What the agent needs from retrieval - `aioc.retrieval.CorpusSearcher` satisfies it,
    and tests inject deterministic fakes through the same seam."""

    def search(self, query: str, *, k: int = DEFAULT_TOP_K) -> RetrievalResult: ...


def _new_id(prefix: str) -> str:
    """A local opaque id for standalone runs. The coordinator supplies real ids."""
    return f"{prefix}_{uuid4().hex[:8]}"


def _normalise(text: str) -> str:
    return " ".join(text.split())


class DocsAgent:
    """Docs agent: retrieval-grounded, schema-validated documentation answers (Day 8)."""

    name = AGENT_NAME

    def __init__(
        self,
        client: LLMClient | None = None,
        retriever: CorpusRetriever | None = None,
    ) -> None:
        self._client = client or LLMClient()
        self._retriever = retriever or CorpusSearcher(default_embedder())

    def answer(
        self,
        query: str,
        *,
        context: str,
        request_id: str | None = None,
        invocation_id: str | None = None,
        usage: Usage | None = None,
        top_k: int = DEFAULT_TOP_K,
    ) -> DocsAgentResponse:
        """Answer one documentation query as a schema-validated `DocsAgentResponse`.

        ``context`` is the coordinator's explicit context block (``context_passed``,
        CONTRACTS.md sec 5) and must be non-empty - same rule as every agent. Retrieval
        runs first and its documents are rendered into the prompt; the model is then forced
        through ``emit_docs_report``; and the assembled response is validated against the
        full contract envelope plus this module's grounding checks.

        Raises `DocsAgentError` for missing/ungrounded output and pydantic's
        ``ValidationError`` for a payload that violates the contract. Re-requesting with
        the error attached is the Day 17 validation-retry loop - deliberately not built
        yet, so a bad payload surfaces loudly rather than being silently patched.
        """
        if not query.strip():
            raise ValueError("query must be non-empty")
        if not context.strip():
            raise ValueError(
                "context must be non-empty - the Docs agent inherits nothing; "
                "pass everything it needs explicitly (CONTRACTS.md sec 5, context_passed)"
            )

        started_at = datetime.now(UTC)
        t0 = monotonic()
        result = self._retriever.search(query.strip(), k=top_k)
        search_ref = ToolCallRef(
            id=_new_id("tc"),
            tool_name="search_corpus",
            server="aioc-docs",
            started_at=started_at,
            duration_ms=int((monotonic() - t0) * 1000),
            ok=True,
            error_class=None,
            # Rough chars/4 estimate; the exact number is a Langfuse (Day 9) concern.
            tokens_returned=sum(len(d.text) for d in result.docs) // 4,
            truncated=False,
        )

        prompt = self._prompt(query.strip(), context.strip(), result)
        resp = self._client.complete(
            messages=[{"role": "user", "content": prompt}],
            system=DOCS_STRUCTURED_SYSTEM_PROMPT,
            tools=[_EMIT_TOOL],
            tool_choice={"type": "tool", "name": EMIT_TOOL_NAME},
        )
        if usage is not None:
            usage.input_tokens += resp.usage.input_tokens
            usage.output_tokens += resp.usage.output_tokens
        # Same truncation-before-validation order as the Incident agent: a report cut off at
        # max_tokens parses as "missing required field" and misdirects the debugging.
        if resp.stop_reason == "max_tokens":
            raise DocsAgentError(
                f"{EMIT_TOOL_NAME} output was truncated at the max_tokens limit "
                f"({resp.usage.output_tokens} output tokens); the report is incomplete. "
                "Raise AIOC_MAX_TOKENS or narrow the query."
            )

        payload = _extract_tool_input(resp, EMIT_TOOL_NAME)
        report = DocsReport.model_validate(payload)
        _check_grounding(report, result)

        findings = DocsFindings(
            answer=report.findings.answer,
            claims=report.findings.claims,
            coverage=Coverage(
                sub_questions=report.findings.coverage.sub_questions,
                answered=report.findings.coverage.answered,
                unanswered=report.findings.coverage.unanswered,
                documents_searched=result.documents_searched,
                documents_retrieved=len(result.docs),
                documents_cited=_documents_cited(report.findings.claims),
                corpus_snapshot=result.corpus_snapshot,
            ),
        )
        evidence = [
            e.model_copy(update={"tool_call_id": search_ref.id})
            if e.source_type is SourceType.DOCUMENT
            else e
            for e in report.evidence
        ]
        return DocsAgentResponse(
            request_id=request_id or _new_id("req"),
            invocation_id=invocation_id or _new_id("inv"),
            status=report.status,
            status_detail=report.status_detail,
            summary=report.summary,
            findings=findings,
            evidence=evidence,
            gaps=report.gaps,
            overall_confidence=report.overall_confidence,
            tool_calls=[search_ref],
            generated_at=datetime.now(UTC),
        )

    @staticmethod
    def _prompt(query: str, context: str, result: RetrievalResult) -> str:
        header = (
            f'<documents retrieved="{len(result.docs)}" '
            f'searched="{result.documents_searched}" mode="{result.mode}">'
        )
        lines = [header]
        if result.degraded:
            lines.append(f"(retrieval note: {result.degraded})")
        if not result.docs:
            lines.append("(no documents matched the query)")
        for doc in result.docs:
            lines.append(
                f'<document id="{doc.doc_id}" title="{doc.title}" uri="{doc.uri}" '
                f'relevance="{doc.relevance:.2f}">'
            )
            lines.append(doc.text)
            lines.append("</document>")
        lines.append("</documents>")
        documents_block = "\n".join(lines)
        return (
            f"<context>\n{context}\n</context>\n\n{documents_block}\n\nDocumentation query: {query}"
        )


def _documents_cited(claims: list[Claim]) -> int:
    return len({source.document_id for claim in claims for source in claim.sources})


def _doc_ref(ref: str) -> str:
    """`doc_012#7` -> `doc_012`. Evidence `source_ref` may carry a chunk suffix (the sec 8
    worked example does); grounding is checked against the document either way."""
    return ref.split("#", 1)[0]


def _check_grounding(report: DocsReport, result: RetrievalResult) -> None:
    """The two checks that make "retrieved documents only" a property, not a hope."""
    retrieved = {doc.doc_id: _normalise(doc.text) for doc in result.docs}

    cited: dict[str, str] = {}  # id -> where it was cited, for the error message
    for claim in report.findings.claims:
        for source in claim.sources:
            cited.setdefault(source.document_id, f"claim {claim.id}")
    for entry in report.evidence:
        if entry.source_type is SourceType.DOCUMENT:
            cited.setdefault(_doc_ref(entry.source_ref), f"evidence {entry.id}")
    unknown = {doc_id: at for doc_id, at in cited.items() if doc_id not in retrieved}
    if unknown:
        listed = ", ".join(f"{doc_id} (at {at})" for doc_id, at in sorted(unknown.items()))
        raise DocsAgentError(
            f"the report cites document(s) retrieval never returned: {listed} - "
            "a source outside the retrieved set is a hallucination by definition"
        )

    for claim in report.findings.claims:
        for source in claim.sources:
            if source.quote is not None and _normalise(source.quote) not in retrieved.get(
                source.document_id, ""
            ):
                raise DocsAgentError(
                    f"claim {claim.id} quotes {source.document_id} with text that does not "
                    "appear verbatim in that document - quotes must never be paraphrased"
                )
    for entry in report.evidence:
        if entry.source_type is SourceType.DOCUMENT:
            if _normalise(entry.excerpt) not in retrieved[_doc_ref(entry.source_ref)]:
                raise DocsAgentError(
                    f"evidence {entry.id} excerpt does not appear verbatim in "
                    f"{entry.source_ref} - excerpts must never be paraphrased"
                )


def _extract_tool_input(resp: Any, tool_name: str) -> dict[str, Any]:
    """Pull the forced tool call's input object out of the response, or fail clearly."""
    for block in resp.content:
        if isinstance(block, ToolUseBlock) and block.name == tool_name:
            if not isinstance(block.input, dict):
                raise DocsAgentError(f"{tool_name} input was not a JSON object")
            return dict(block.input)
    raise DocsAgentError(
        f"model did not call {tool_name} (stop_reason={resp.stop_reason!r}); no structured output"
    )
