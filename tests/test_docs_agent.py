"""Unit tests for the Docs agent (Day 8).

Same pattern as the incident suite: the Anthropic client is a scripted fake injected
through `LLMClient` and retrieval is a recording fake behind the `CorpusRetriever` seam, so
everything here is deterministic, offline, and key-free. Under test is the agent's
plumbing - explicit context passing, document rendering, the grounding checks, the stamped
coverage accounting - not the model's judgment (the Day 19 eval harness owns that).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from anthropic.types import TextBlock, ToolUseBlock
from pydantic import ValidationError

from aioc.agents import (
    DOCS_EMIT_TOOL_NAME,
    DOCS_STRUCTURED_SYSTEM_PROMPT,
    DocsAgent,
    DocsAgentError,
)
from aioc.agents.docs import _EMIT_SCHEMA, DEFAULT_TOP_K, _apply_guidance
from aioc.contracts import AgentName, DocsAgentResponse, ResponseStatus
from aioc.llm import LLMClient, LLMSettings, Usage
from aioc.retrieval import RetrievalResult, RetrievedDoc

# --------------------------------------------------------------------------- fakes


class _FakeMessages:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake client ran out of scripted responses")
        return self._responses.pop(0)


class _FakeAnthropic:
    def __init__(self, responses: list[Any]) -> None:
        self.messages = _FakeMessages(responses)


class _FakeRetriever:
    def __init__(self, result: RetrievalResult) -> None:
        self._result = result
        self.calls: list[tuple[str, int]] = []

    def search(self, query: str, *, k: int = DEFAULT_TOP_K) -> RetrievalResult:
        self.calls.append((query, k))
        return self._result


_DOC_1_TEXT = (
    "payments-api resident memory climbed to OOM kill\n"
    "Incident inc_0001, severity sev2, 2026-01-14T12:05:00Z to 2026-01-14T14:40:00Z.\n"
    "Summary: RSS grew steadily until the container was OOM-killed.\n"
    "Root cause: An in-memory idempotency cache had no eviction policy.\n"
    "Resolution: Added an LRU bound of 10000 entries and a 15-minute TTL to the cache."
)

_DOC_2_TEXT = (
    "inventory-api connection pool exhausted under catalogue sync\n"
    "Incident inc_0003, severity sev3, 2026-02-03T02:10:00Z to 2026-02-03T03:25:00Z.\n"
    "Summary: a nightly sync held every pooled connection.\n"
    "Resolution: gave the sync its own small pool."
)

_QUOTE = "Added an LRU bound of 10000 entries and a 15-minute TTL to the cache."

_SUB_Q_ANSWERED = "How was the payments-api memory leak fixed?"
_SUB_Q_UNANSWERED = "Is there a standing memory alert threshold?"


def _doc(doc_id: str, incident_id: str, title: str, text: str) -> RetrievedDoc:
    return RetrievedDoc(
        doc_id=doc_id,
        incident_id=incident_id,
        title=title,
        text=text,
        relevance=0.9,
        uri=f"corpus://incidents/{incident_id}",
        lexical_score=0.4,
        vector_score=0.9,
    )


def _retrieval(
    docs: list[RetrievedDoc] | None = None,
    *,
    mode: str = "hybrid",
    snapshot: str | None = "ingest_2026-08-20",
    degraded: str | None = None,
) -> RetrievalResult:
    if docs is None:
        docs = [
            _doc("doc_0001", "inc_0001", "payments-api OOM kill", _DOC_1_TEXT),
            _doc("doc_0003", "inc_0003", "inventory-api pool exhaustion", _DOC_2_TEXT),
        ]
    return RetrievalResult(
        query="q",
        docs=docs,
        documents_searched=18,
        corpus_snapshot=snapshot,
        mode=mode,  # type: ignore[arg-type]
        degraded=degraded,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    """A contract-valid DocsReport payload grounded in the fake retrieval above."""
    payload: dict[str, Any] = {
        "status": "partial",
        "status_detail": None,
        "summary": "The corpus documents the memory-leak fix; no alert threshold is recorded.",
        "findings": {
            "answer": {
                "value": "The leak was fixed by bounding the idempotency cache with an LRU "
                "limit and a TTL.",
                "confidence": 0.85,
                "evidence": ["ev_1"],
                "reasoning": "Stated directly in the incident's resolution.",
                "detail": None,
            },
            "claims": [
                {
                    "id": "claim_1",
                    "statement": "The payments-api memory leak was resolved by bounding "
                    "the idempotency cache.",
                    "supported": True,
                    "sources": [
                        {
                            "document_id": "doc_0001",
                            "title": "payments-api OOM kill",
                            "chunk_id": None,
                            "uri": "corpus://incidents/inc_0001",
                            "quote": _QUOTE,
                            "relevance": 0.9,
                        }
                    ],
                    "confidence": 0.85,
                },
                {
                    "id": "claim_2",
                    "statement": "A standing memory alert threshold exists.",
                    "supported": False,
                    "sources": [],
                    "confidence": 0.1,
                },
            ],
            "coverage": {
                "sub_questions": [_SUB_Q_ANSWERED, _SUB_Q_UNANSWERED],
                "answered": [_SUB_Q_ANSWERED],
                "unanswered": [_SUB_Q_UNANSWERED],
            },
        },
        "evidence": [
            {
                "id": "ev_1",
                "source_type": "document",
                "source_type_detail": None,
                "source_ref": "doc_0001",
                "excerpt": _QUOTE,
                "observed_at": None,
                "uri": "corpus://incidents/inc_0001",
                "tool_call_id": None,
            }
        ],
        "gaps": [
            {
                "id": "gap_1",
                "description": "No document records a standing memory alert threshold.",
                "kind": "missing_data",
                "kind_detail": None,
                "blocks_field": "findings.coverage.unanswered",
                "suggested_agent": None,
                "suggested_query": None,
                "resolvable": False,
            }
        ],
        "overall_confidence": 0.8,
    }
    payload.update(overrides)
    return payload


def _tool_message(
    payload: dict[str, Any],
    *,
    stop_reason: str = "tool_use",
    in_tokens: int = 900,
    out_tokens: int = 400,
) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            ToolUseBlock(type="tool_use", id="toolu_1", name=DOCS_EMIT_TOOL_NAME, input=payload)
        ],
        model="claude-sonnet-5",
        usage=SimpleNamespace(input_tokens=in_tokens, output_tokens=out_tokens),
    )


def _agent(
    responses: list[Any],
    retrieval: RetrievalResult | None = None,
) -> tuple[DocsAgent, _FakeMessages, _FakeRetriever]:
    fake = _FakeAnthropic(responses)
    settings = LLMSettings(model="claude-sonnet-5", max_tokens=4096)
    client = LLMClient(settings, client=fake)  # type: ignore[arg-type]
    retriever = _FakeRetriever(retrieval if retrieval is not None else _retrieval())
    return DocsAgent(client, retriever), fake.messages, retriever


_CONTEXT = "Investigating repeat memory pressure on payments-api after last week's incident."
_QUERY = "How did we fix the payments-api memory leak, and is there an alert threshold?"


# ------------------------------------------------------------------------ happy path


def test_answer_returns_a_validated_docs_response_with_stamped_accounting():
    agent, _, retriever = _agent([_tool_message(_payload())])
    usage = Usage()
    resp = agent.answer(
        _QUERY, context=_CONTEXT, request_id="req_1", invocation_id="inv_1", usage=usage
    )

    assert isinstance(resp, DocsAgentResponse)
    assert resp.agent is AgentName.DOCS
    assert (resp.request_id, resp.invocation_id) == ("req_1", "inv_1")
    assert resp.status is ResponseStatus.PARTIAL

    # The coverage counters are measured by the runtime, never asked of the model.
    cov = resp.findings.coverage
    assert cov.documents_searched == 18
    assert cov.documents_retrieved == 2
    assert cov.documents_cited == 1  # claim_1 cites doc_0001; claim_2 cites nothing
    assert cov.corpus_snapshot == "ingest_2026-08-20"

    # The retrieval call is recorded honestly and stamped into document evidence.
    assert len(resp.tool_calls) == 1
    ref = resp.tool_calls[0]
    assert (ref.tool_name, ref.server, ref.ok) == ("search_corpus", "aioc-docs", True)
    assert resp.evidence[0].tool_call_id == ref.id

    # Cost accounting threads through the shared accumulator.
    assert (usage.input_tokens, usage.output_tokens) == (900, 400)

    # The retriever saw the query once, at the default depth.
    assert retriever.calls == [(_QUERY, DEFAULT_TOP_K)]


def test_prompt_carries_context_documents_and_query_explicitly():
    agent, messages, _ = _agent([_tool_message(_payload())])
    agent.answer(_QUERY, context=_CONTEXT, request_id="req_1", invocation_id="inv_1")

    call = messages.calls[0]
    prompt = call["messages"][0]["content"]
    assert f"<context>\n{_CONTEXT}\n</context>" in prompt
    assert '<document id="doc_0001"' in prompt
    assert '<document id="doc_0003"' in prompt
    assert _DOC_1_TEXT in prompt
    assert 'retrieved="2" searched="18" mode="hybrid"' in prompt
    assert prompt.endswith(f"Documentation query: {_QUERY}")

    system = call["system"]
    assert system == DOCS_STRUCTURED_SYSTEM_PROMPT
    assert "ONLY from the retrieved documents" in system
    assert "0.90-1.00" in system  # the CONTRACTS.md sec 2.1 band table, verbatim
    assert call["tool_choice"] == {"type": "tool", "name": DOCS_EMIT_TOOL_NAME}


def test_degraded_retrieval_is_surfaced_to_the_model():
    agent, messages, _ = _agent(
        [_tool_message(_payload())],
        _retrieval(mode="lexical", snapshot=None, degraded="no embedder configured"),
    )
    resp = agent.answer(_QUERY, context=_CONTEXT)
    prompt = messages.calls[0]["messages"][0]["content"]
    assert "(retrieval note: no embedder configured)" in prompt
    assert resp.findings.coverage.corpus_snapshot is None


def test_empty_retrieval_supports_an_insufficient_evidence_report():
    payload = _payload(
        status="insufficient_evidence",
        summary="No document matches the query.",
        evidence=[],
        findings={
            "answer": {
                "value": None,
                "confidence": 0.0,
                "evidence": [],
                "reasoning": "Nothing was retrieved.",
                "detail": None,
            },
            "claims": [],
            "coverage": {
                "sub_questions": [_SUB_Q_ANSWERED],
                "answered": [],
                "unanswered": [_SUB_Q_ANSWERED],
            },
        },
        gaps=[
            {
                "id": "gap_1",
                "description": "Retrieval returned no documents for this query.",
                "kind": "missing_data",
                "kind_detail": None,
                "blocks_field": "findings.answer.value",
                "suggested_agent": None,
                "suggested_query": None,
                "resolvable": False,
            },
            {
                "id": "gap_2",
                "description": "The sub-question could not be answered from the corpus.",
                "kind": "missing_data",
                "kind_detail": None,
                "blocks_field": "findings.coverage.unanswered",
                "suggested_agent": None,
                "suggested_query": None,
                "resolvable": False,
            },
        ],
        overall_confidence=0.1,
    )
    agent, messages, _ = _agent([_tool_message(payload)], _retrieval(docs=[], snapshot=None))
    resp = agent.answer(_QUERY, context=_CONTEXT)
    assert "(no documents matched the query)" in messages.calls[0]["messages"][0]["content"]
    assert resp.status is ResponseStatus.INSUFFICIENT_EVIDENCE
    assert resp.findings.coverage.documents_retrieved == 0
    assert resp.findings.answer.value is None


# ------------------------------------------------------------------ explicit context


def test_empty_context_is_rejected():
    agent, _, _ = _agent([])
    with pytest.raises(ValueError, match="context must be non-empty"):
        agent.answer(_QUERY, context="   ")


def test_empty_query_is_rejected():
    agent, _, _ = _agent([])
    with pytest.raises(ValueError, match="query must be non-empty"):
        agent.answer("", context=_CONTEXT)


# ---------------------------------------------------------------------- grounding


def test_citing_a_document_retrieval_never_returned_is_rejected():
    payload = _payload()
    payload["findings"]["claims"][0]["sources"][0]["document_id"] = "doc_9999"
    agent, _, _ = _agent([_tool_message(payload)])
    with pytest.raises(DocsAgentError, match="doc_9999"):
        agent.answer(_QUERY, context=_CONTEXT)


def test_evidence_referencing_an_unretrieved_document_is_rejected():
    payload = _payload()
    payload["evidence"][0]["source_ref"] = "doc_9999#2"
    agent, _, _ = _agent([_tool_message(payload)])
    with pytest.raises(DocsAgentError, match="doc_9999"):
        agent.answer(_QUERY, context=_CONTEXT)


def test_a_paraphrased_quote_is_rejected():
    payload = _payload()
    payload["findings"]["claims"][0]["sources"][0]["quote"] = (
        "They fixed it by limiting the cache size."
    )
    agent, _, _ = _agent([_tool_message(payload)])
    with pytest.raises(DocsAgentError, match="verbatim"):
        agent.answer(_QUERY, context=_CONTEXT)


def test_a_paraphrased_evidence_excerpt_is_rejected():
    payload = _payload()
    payload["evidence"][0]["excerpt"] = "The cache was bounded."
    agent, _, _ = _agent([_tool_message(payload)])
    with pytest.raises(DocsAgentError, match="verbatim"):
        agent.answer(_QUERY, context=_CONTEXT)


def test_a_quote_with_different_whitespace_still_counts_as_verbatim():
    payload = _payload()
    payload["findings"]["claims"][0]["sources"][0]["quote"] = _QUOTE.replace(" ", "  ", 3)
    agent, _, _ = _agent([_tool_message(payload)])
    resp = agent.answer(_QUERY, context=_CONTEXT)
    assert resp.findings.claims[0].sources[0].quote is not None


def test_a_chunk_suffixed_evidence_source_ref_is_accepted():
    payload = _payload()
    payload["evidence"][0]["source_ref"] = "doc_0001#0"
    agent, _, _ = _agent([_tool_message(payload)])
    resp = agent.answer(_QUERY, context=_CONTEXT)
    assert resp.evidence[0].source_ref == "doc_0001#0"


# ----------------------------------------------------------------- contract invariants


def test_unanswered_sub_question_without_a_matching_gap_is_rejected():
    payload = _payload(gaps=[])
    agent, _, _ = _agent([_tool_message(payload)])
    with pytest.raises(ValidationError, match="findings.coverage.unanswered"):
        agent.answer(_QUERY, context=_CONTEXT)


def test_a_sourceless_supported_claim_is_rejected():
    payload = _payload()
    payload["findings"]["claims"][1]["supported"] = True
    agent, _, _ = _agent([_tool_message(payload)])
    with pytest.raises(ValidationError, match="supported"):
        agent.answer(_QUERY, context=_CONTEXT)


# ------------------------------------------------------------------- failure modes


def test_truncated_output_is_reported_as_truncation_not_schema_failure():
    agent, _, _ = _agent([_tool_message(_payload(), stop_reason="max_tokens")])
    with pytest.raises(DocsAgentError, match="max_tokens"):
        agent.answer(_QUERY, context=_CONTEXT)


def test_a_prose_only_response_is_a_clear_error():
    message = SimpleNamespace(
        stop_reason="end_turn",
        content=[TextBlock(type="text", text="here is my answer in prose")],
        model="claude-sonnet-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=10),
    )
    agent, _, _ = _agent([message])
    with pytest.raises(DocsAgentError, match="did not call"):
        agent.answer(_QUERY, context=_CONTEXT)


# ------------------------------------------------------------------ schema annotation


def test_emit_schema_carries_the_grounding_guidance():
    claim_props = _EMIT_SCHEMA["$defs"]["Claim"]["properties"]
    assert "sources" in claim_props["supported"]["description"]
    source_props = _EMIT_SCHEMA["$defs"]["SourceRef"]["properties"]
    assert "VERBATIM" in source_props["quote"]["description"]
    assert "retrieved" in source_props["document_id"]["description"]
    # The stamped counters are absent from what the model is asked for.
    coverage_props = _EMIT_SCHEMA["$defs"]["ReportedCoverage"]["properties"]
    assert "documents_searched" not in coverage_props
    assert "corpus_snapshot" not in coverage_props


def test_guidance_drift_fails_loudly():
    # Guidance targeting a field the schema no longer has must raise, not silently drop.
    with pytest.raises(RuntimeError, match="docs emit schema guidance"):
        _apply_guidance({"properties": {}, "$defs": {}})
