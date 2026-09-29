"""Day 19 eval harness: the answer key, the cases, the scoring, the runner, the report.

Offline, like the rest of the suite. The model's judgement is what a live run measures
(`scripts/run_evals.py`); what is tested here is everything the measurement stands on:

- the seed is read correctly, and the file reads the same as the table (``integration``);
- no case shows an agent its own answer, and a case file that would is refused;
- scoring says what it claims - a wrong answer, an abstention, and an invented detail are
  three different results, each with a test that produces exactly that one;
- the runner scores the shipped agents, attributes tokens to the case that spent them,
  and gives the same scores whether the requests went realtime or through a batch.

The agents themselves are used to build the responses being scored (against a scripted
client), so every response in this file is one the contract and the agents accept.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from anthropic.types import ToolUseBlock

from aioc.agents import DOCS_EMIT_TOOL_NAME, EMIT_TOOL_NAME, DocsAgent, IncidentAgent
from aioc.contracts import DocsAgentResponse, FailureMode, IncidentAgentResponse, ToolCallRef
from aioc.coordinator.confidence import Band
from aioc.evals import (
    DEFAULT_SET,
    SEED_PATH,
    CaseError,
    EvalAborted,
    EvalItem,
    ItemScore,
    RecordingRetriever,
    SeedError,
    SeedIncident,
    Task,
    calibrate,
    check_leaks,
    cost_view,
    load_cases,
    load_seed,
    parse_seed,
    render_markdown,
    run_batch,
    run_realtime,
    score_failure,
    score_item,
    seed_by_id,
    sentences,
    summarise,
    to_record,
    tool_success,
)
from aioc.evals.cases import SignalSelection
from aioc.llm import DeferredClient, LLMClient, LLMSettings, Usage, price
from aioc.retrieval import RetrievalResult, RetrievedDoc, doc_id_for
from tests.test_batch import _batcher, _FakeBatches, _succeeded

CASES = load_cases()
ITEMS = {item.key: item for item in CASES.items}
SEED = seed_by_id()

_SETTINGS = LLMSettings(
    model="claude-sonnet-5", max_tokens=2048, prompt_caching=True, max_validation_retries=2
)


# ===================================================================== building responses


def _first_symptom(item: EvalItem) -> str:
    lines = item.context.splitlines()
    return lines[lines.index("Reported symptoms:") + 1].removeprefix("- ")


def _first_event(item: EvalItem) -> tuple[str, str]:
    """(timestamp, service) of the first event line the agent was shown."""
    line = next(ln for ln in item.context.splitlines() if re.match(r"- 20\d\d-", ln))
    stamp, service = line.removeprefix("- ").split(" ")[:2]
    return stamp, service


def _assessment(value: Any, confidence: float, *, detail: str | None = None) -> dict[str, Any]:
    return {
        "value": value,
        "confidence": confidence,
        "evidence": ["ev_1"] if value is not None else [],
        "reasoning": "From the reported symptoms.",
        "detail": detail,
    }


def _diagnosis(
    item: EvalItem,
    *,
    mode: str | None = None,
    severity: str | None = None,
    mode_confidence: float = 0.8,
    severity_confidence: float = 0.75,
    services: list[str] | None = None,
    excerpt: str | None = None,
    timeline_at: str | None = None,
    impact: dict[str, Any] | None = None,
    similar: list[str] | None = None,
    answer_truth: bool = True,
) -> dict[str, Any]:
    """An `emit_incident_report` payload about ``item``. With no overrides it is the
    right answer, grounded line by line in the context the item shows."""
    assert item.incident is not None
    truth = item.incident
    if mode is None and answer_truth:
        mode = truth.true_failure_mode
    if severity is None and answer_truth:
        severity = truth.true_severity
    stamp, service = _first_event(item)
    gaps = [_gap("findings.root_cause.value", "gap_1")]
    if mode is None:
        gaps.append(_gap("findings.failure_mode.value", "gap_2"))
    if severity is None:
        gaps.append(_gap("findings.severity.value", "gap_3"))
    shown: dict[str, Any] = {
        "error_rate_before": None,
        "error_rate_after": None,
        "p50_latency_ms_before": None,
        "p50_latency_ms_after": None,
        "p99_latency_ms_before": None,
        "p99_latency_ms_after": None,
        "requests_affected": None,
        "duration_seconds": None,
    }
    shown.update(impact or {})
    return {
        "status": "partial",
        "status_detail": None,
        "summary": "A diagnosis from the signals given.",
        "findings": {
            "incident_window": {"start": timeline_at or stamp, "end": None},
            "affected_services": (
                services if services is not None else list(truth.affected_services)[:1]
            ),
            "severity": _assessment(severity, severity_confidence),
            "failure_mode": _assessment(
                mode, mode_confidence, detail="No named mode fits." if mode == "other" else None
            ),
            "root_cause": _assessment(None, 0.2),
            "contributing_factors": [],
            "timeline": [
                {
                    "id": "evt_a",
                    "at": timeline_at or stamp,
                    "service": service,
                    "kind": "alert",
                    "kind_detail": None,
                    "description": "The first signal.",
                    "severity": None,
                    "evidence_id": "ev_1",
                }
            ],
            "impact": shown,
            "recommended_actions": [],
            "similar_incidents": similar or [],
        },
        "evidence": [
            {
                "id": "ev_1",
                "source_type": "metric",
                "source_type_detail": None,
                "source_ref": "context",
                "excerpt": excerpt if excerpt is not None else _first_symptom(item),
                "observed_at": None,
                "uri": None,
                "tool_call_id": None,
            }
        ],
        "gaps": gaps,
        "overall_confidence": 0.6,
    }


def _gap(field: str, gap_id: str) -> dict[str, Any]:
    return {
        "id": gap_id,
        "description": "The signals do not establish this.",
        "kind": "missing_data",
        "kind_detail": None,
        "blocks_field": field,
        "suggested_agent": None,
        "suggested_query": None,
        "resolvable": False,
    }


def _document(incident: SeedIncident) -> RetrievedDoc:
    text = (
        f"{incident.title}\nIncident {incident.id}.\nSummary: {incident.summary}\n"
        f"Root cause: {incident.true_root_cause}\nResolution: {incident.resolution}"
    )
    return RetrievedDoc(
        doc_id=incident.document_id,
        incident_id=incident.id,
        title=incident.title,
        text=text,
        relevance=0.9,
        uri=f"corpus://incidents/{incident.id}",
        lexical_score=0.5,
        vector_score=0.9,
    )


def _recall(
    cite: SeedIncident | None,
    *,
    answer: str | None = "It has happened before and the fix is recorded.",
    question: str = "Has this happened before?",
) -> dict[str, Any]:
    """An `emit_docs_report` payload. ``cite`` is the post-mortem it stands on; with none
    it is the honest empty answer - a null value, the question unanswered, and its gap."""
    if cite is None:
        return {
            "status": "insufficient_evidence",
            "status_detail": None,
            "summary": "The retrieved post-mortems record no such incident.",
            "findings": {
                "answer": _docs_answer(None),
                "claims": [],
                "coverage": {"sub_questions": [question], "answered": [], "unanswered": [question]},
            },
            "evidence": [],
            "gaps": [
                _gap("findings.answer.value", "gap_1"),
                _gap("findings.coverage.unanswered[0]", "gap_2"),
            ],
            "overall_confidence": 0.2,
        }
    return {
        "status": "complete",
        "status_detail": None,
        "summary": "The corpus records a precedent.",
        "findings": {
            "answer": _docs_answer(answer),
            "claims": [
                {
                    "id": "claim_1",
                    "statement": "A precedent is recorded with its resolution.",
                    "supported": True,
                    "sources": [
                        {
                            "document_id": cite.document_id,
                            "title": cite.title,
                            "chunk_id": None,
                            "uri": None,
                            "quote": cite.resolution,
                            "relevance": 0.9,
                        }
                    ],
                    "confidence": 0.85,
                }
            ],
            "coverage": {"sub_questions": [question], "answered": [question], "unanswered": []},
        },
        "evidence": [
            {
                "id": "ev_1",
                "source_type": "document",
                "source_type_detail": None,
                "source_ref": cite.document_id,
                "excerpt": cite.resolution,
                "observed_at": None,
                "uri": None,
                "tool_call_id": None,
            }
        ],
        "gaps": [],
        "overall_confidence": 0.8,
    }


def _docs_answer(value: str | None) -> dict[str, Any]:
    return {
        "value": value,
        "confidence": 0.85 if value is not None else 0.1,
        "evidence": ["ev_1"] if value is not None else [],
        "reasoning": "From the cited post-mortem." if value else "Nothing retrieved answers it.",
        "detail": None,
    }


def _message(tool: str, payload: dict[str, Any], *, cached: int = 4_000) -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="tool_use",
        model="claude-sonnet-5",
        content=[ToolUseBlock(type="tool_use", id="toolu_1", name=tool, input=payload)],
        usage=SimpleNamespace(
            input_tokens=200,
            output_tokens=900,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=cached,
        ),
    )


class _Scripted:
    """An Anthropic client that replies with the given messages, in order."""

    def __init__(self, replies: list[Any]) -> None:
        self._replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._replies.pop(0)


class _Retriever:
    """Returns the documents of the given incidents, whatever is asked."""

    def __init__(self, *incident_ids: str) -> None:
        self._docs = [_document(SEED[incident_id]) for incident_id in incident_ids]
        self.calls: list[str] = []

    def search(self, query: str, *, k: int = 5) -> RetrievalResult:
        self.calls.append(query)
        return RetrievalResult(
            query=query,
            docs=list(self._docs),
            documents_searched=18,
            corpus_snapshot="ingest_test",
            mode="hybrid",
            degraded=None,
        )


def _diagnosed(item: EvalItem, **overrides: Any) -> IncidentAgentResponse:
    client = LLMClient(
        _SETTINGS,
        client=_Scripted([_message(EMIT_TOOL_NAME, _diagnosis(item, **overrides))]),  # type: ignore[arg-type]
    )
    return IncidentAgent(client, max_validation_retries=0).diagnose(
        item.query, context=item.context
    )


def _recalled(
    item: EvalItem, cite: str | None, *, retrieved: tuple[str, ...], **overrides: Any
) -> DocsAgentResponse:
    payload = _recall(None if cite is None else SEED[cite], **overrides)
    client = LLMClient(
        _SETTINGS,
        client=_Scripted([_message(DOCS_EMIT_TOOL_NAME, payload)]),  # type: ignore[arg-type]
    )
    return DocsAgent(client, _Retriever(*retrieved), max_validation_retries=0).answer(
        item.query, context=item.context
    )


# ================================================================================ the seed


def test_the_seed_parses_into_every_incident_and_every_event():
    seed = load_seed()
    assert len(seed) == 18
    assert sum(len(incident.events) for incident in seed) == 65
    assert len({incident.id for incident in seed}) == 18


def test_a_row_is_read_field_by_field():
    incident = SEED["inc_0001"]
    assert incident.title == "payments-api resident memory climbed to OOM kill"
    assert incident.summary.startswith("payments-api RSS grew steadily from 180MB")
    assert incident.started_at.isoformat() == "2026-01-14T12:05:00+00:00"
    assert incident.ended_at is not None and incident.ended_at.hour == 14
    assert incident.affected_services == ("payments-api", "checkout-api")
    assert (incident.true_severity, incident.true_failure_mode) == ("sev2", "resource_exhaustion")
    assert incident.true_failure_mode_detail is None
    assert "idempotency cache" in incident.true_root_cause
    assert incident.resolution.startswith("Added an LRU bound")
    assert incident.impact["error_rate_after"] == 0.074
    assert incident.impact["p99_latency_ms_after"] == 2100
    assert incident.impact["requests_affected"] == 48200
    assert [event.id for event in incident.events] == [
        "evt_0001_1",
        "evt_0001_2",
        "evt_0001_3",
        "evt_0001_4",
    ]
    assert incident.events[2].kind == "restart" and incident.events[2].severity == "sev2"


def test_an_unmeasured_metric_stays_null():
    impact = SEED["inc_0007"].impact
    assert impact["error_rate_before"] is None and impact["p99_latency_ms_after"] is None
    assert impact["requests_affected"] == 340


def test_an_other_failure_mode_carries_its_detail():
    incident = SEED["inc_0013"]
    assert incident.true_failure_mode == "other"
    assert incident.true_failure_mode_detail is not None
    assert "TLS client certificate" in incident.true_failure_mode_detail
    event = SEED["inc_0003"].events[0]
    assert (event.kind, event.severity) == ("other", None)
    assert event.kind_detail is not None and event.kind_detail.startswith("Scheduled batch job")


def test_events_are_oldest_first_and_belong_to_their_incident():
    for incident in load_seed():
        stamps = [event.at for event in incident.events]
        assert stamps == sorted(stamps), incident.id
        assert {event.incident_id for event in incident.events} == {incident.id}


def test_the_answer_key_covers_every_failure_mode():
    seeded = {incident.true_failure_mode for incident in load_seed()}
    assert seeded == {mode.value for mode in FailureMode}


def test_document_ids_are_the_retrieval_layers():
    for incident in load_seed():
        assert incident.document_id == doc_id_for(incident.id)


_TINY = """
-- a comment, with an 'apostrophe' and a (parenthesis
INSERT INTO incidents (
    id, title, summary, started_at, ended_at, affected_services,
    true_severity, true_failure_mode, true_failure_mode_detail,
    true_root_cause, resolution,
    error_rate_before, error_rate_after,
    p50_latency_ms_before, p50_latency_ms_after,
    p99_latency_ms_before, p99_latency_ms_after,
    requests_affected
) VALUES
('inc_t1', 'a title -- not a comment', 'It''s slow. Then it stopped.',
 '2026-01-01T00:00:00Z', NULL, '{}',
 'sev3', 'other', 'a detail',
 'cause', 'fix',
 0.5, NULL, 1, 2, NULL, NULL, 7)
ON CONFLICT (id) DO NOTHING;

INSERT INTO incident_timeline_events
    (id, incident_id, at, service, description, kind, kind_detail, severity) VALUES
('evt_t1_2','inc_t1','2026-01-01T00:09:00Z','svc','second','alert',NULL,'sev3'), -- trailing
('evt_t1_1','inc_t1','2026-01-01T00:01:00Z','svc','first','deploy',NULL,NULL)
ON CONFLICT (id) DO NOTHING;
"""


def test_a_comment_is_not_data_and_a_string_is_not_a_comment():
    (incident,) = parse_seed(_TINY)
    assert incident.title == "a title -- not a comment"
    assert incident.summary == "It's slow. Then it stopped."
    assert incident.ended_at is None
    assert incident.affected_services == ()
    assert incident.impact["error_rate_before"] == 0.5
    assert incident.impact["error_rate_after"] is None
    assert [event.id for event in incident.events] == ["evt_t1_1", "evt_t1_2"]


@pytest.mark.parametrize(
    ("broken", "complaint"),
    [
        pytest.param(
            _TINY.replace("NULL, NULL, 7)", "NULL, 7)"),
            "17 values for 18 columns",
            id="a-value-missing",
        ),
        pytest.param(
            _TINY[: _TINY.index("'first'") + 3], "unterminated string", id="cut-mid-string"
        ),
        pytest.param(_TINY[: _TINY.index("'first'")], "unterminated value list", id="cut-mid-row"),
        pytest.param(
            _TINY.replace("0.5, NULL, 1", "now(), NULL, 1"), "unexpected value", id="a-function"
        ),
        pytest.param(
            _TINY.replace("'evt_t1_1','inc_t1'", "'evt_t1_1','inc_gone'"),
            "not seeded",
            id="an-orphan-event",
        ),
        pytest.param(
            _TINY.replace("'2026-01-01T00:00:00Z', NULL", "'2026-01-01 00:00', NULL"),
            "explicit Z",
            id="a-local-timestamp",
        ),
        pytest.param(
            _TINY.replace("INSERT INTO incidents", "INSERT INTO incident_log"),
            "no `INSERT INTO",
            id="no-incidents-statement",
        ),
    ],
)
def test_a_seed_that_does_not_read_cleanly_is_an_error_not_a_guess(broken: str, complaint: str):
    with pytest.raises(SeedError, match=complaint):
        parse_seed(broken)


@pytest.fixture(autouse=True)
def _require_reachable_store(request: pytest.FixtureRequest) -> None:
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


@pytest.mark.integration
def test_the_file_reads_the_same_as_the_table():
    # The Docs agent retrieves from the table and the eval scores against the file: if
    # they differ, the answer key is about a corpus nobody is being asked about.
    import psycopg

    from aioc.tools.incident.store import dsn

    with psycopg.connect(dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, summary, true_severity, true_failure_mode, "
            "true_failure_mode_detail, true_root_cause, resolution, affected_services, "
            "error_rate_after, requests_affected FROM incidents WHERE source = 'synthetic'"
        )
        rows = {row[0]: row for row in cur.fetchall()}
        cur.execute("SELECT id, incident_id, description, kind FROM incident_timeline_events")
        events = {row[0]: row for row in cur.fetchall()}

    seed = load_seed()
    assert set(rows) == {incident.id for incident in seed}
    for incident in seed:
        row = rows[incident.id]
        assert row[1:8] == (
            incident.title,
            incident.summary,
            incident.true_severity,
            incident.true_failure_mode,
            incident.true_failure_mode_detail,
            incident.true_root_cause,
            incident.resolution,
        )
        assert tuple(row[8]) == incident.affected_services
        assert row[9] == incident.impact["error_rate_after"]
        assert row[10] == incident.impact["requests_affected"]
        for event in incident.events:
            assert events[event.id][1:] == (incident.id, event.description, event.kind)
    assert len(events) == sum(len(incident.events) for incident in seed)


# =============================================================================== the cases


def test_the_committed_set_loads_with_both_tasks_for_every_incident():
    assert CASES.path == DEFAULT_SET
    assert (CASES.name, CASES.version) == ("seeded-incidents", 1)
    diagnoses = CASES.select(tasks={Task.DIAGNOSE})
    recalls = CASES.select(tasks={Task.RECALL})
    assert (len(diagnoses), len(recalls)) == (18, 20)
    assert {item.incident.id for item in diagnoses if item.incident} == set(SEED)
    assert [item.key for item in recalls if item.incident is None] == [
        "case_19:recall",
        "case_20:recall",
    ]
    assert {item.agent for item in diagnoses} == {"incident"}
    assert {item.agent for item in recalls} == {"docs"}


def test_the_set_is_identified_by_the_hash_of_its_file():
    import hashlib

    assert CASES.sha256 == hashlib.sha256(DEFAULT_SET.read_bytes()).hexdigest()


def test_every_line_an_agent_is_shown_is_verbatim_from_the_seed():
    for item in CASES.select(tasks={Task.DIAGNOSE}):
        assert item.incident is not None
        incident = item.incident
        recorded = set(sentences(incident.summary))
        events = {event.id: event for event in incident.events}
        lines = item.context.splitlines()
        symptoms = lines[lines.index("Reported symptoms:") + 1 : lines.index("", 3)]
        for line in symptoms:
            assert line.removeprefix("- ") in recorded, (item.key, line)
        for line in lines[lines.index("Events, oldest first:") + 1 :]:
            match = re.fullmatch(r"- (\S+) (\S+) \[(.+?)\] (.*) \((evt_\w+)\)", line)
            assert match is not None, (item.key, line)
            event = events[match.group(5)]
            assert match.group(4) == event.description
            assert match.group(2) == event.service
            assert match.group(1) == event.at.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_no_diagnose_context_carries_its_own_answer():
    # Checked here independently of `check_leaks`, so the guard and its test cannot
    # share a blind spot.
    for item in CASES.select(tasks={Task.DIAGNOSE}):
        assert item.incident is not None
        incident, shown = item.incident, item.context.casefold()
        for withheld in (incident.title, incident.true_root_cause, incident.resolution):
            assert withheld.casefold() not in shown, item.key
        assert incident.id not in shown, item.key
        if incident.true_failure_mode != "other":
            assert incident.true_failure_mode not in shown, item.key
        assert not re.search(r"\bsev[1-4]\b", shown), item.key
        # The last recorded event is the fix in every seeded incident.
        assert incident.events[-1].description.casefold() not in shown, item.key


def test_the_question_is_the_same_for_every_diagnosis():
    # A per-case question is a place for a hint to hide.
    assert len({item.query for item in CASES.select(tasks={Task.DIAGNOSE})}) == 1


def test_an_unmeasured_metric_is_shown_as_unmeasured():
    context = ITEMS["case_06:diagnose"].context
    assert "- error rate: not measured -> not measured" in context
    assert "- requests affected: 340" in context


def test_selection_by_task_and_by_case():
    assert [i.key for i in CASES.select(cases={"case_04"})] == [
        "case_04:diagnose",
        "case_04:recall",
    ]
    assert [i.key for i in CASES.select(tasks={Task.RECALL}, cases={"case_04", "case_19"})] == [
        "case_04:recall",
        "case_19:recall",
    ]


_ALL = SignalSelection(summary_sentences=[0, 1], events=["evt_0001_1"])


@pytest.mark.parametrize(
    ("context", "what"),
    [
        (
            "An in-memory idempotency cache for payment intents had no eviction policy, so every "
            "request retained its entry for the process lifetime.",
            "the root cause",
        ),
        (
            "note: Added an LRU bound of 10000 entries and a 15-minute TTL to the idempotency "
            "cache; set a container memory limit so the failure mode is a fast restart rather "
            "than host pressure.",
            "the resolution",
        ),
        ("payments-api resident memory climbed to OOM kill", "the incident title"),
        ("see inc_0001 for detail", "the incident id"),
        ("this looks like resource_exhaustion", "the failure mode"),
        ("Container  OOM-killed and restarted\nby the runtime", "withheld event evt_0001_3"),
        ("the alert was tagged SEV2", "names a severity level"),
    ],
)
def test_a_context_that_carries_an_answer_is_refused(context: str, what: str):
    with pytest.raises(CaseError, match=re.escape(what)):
        check_leaks(context, SEED["inc_0001"], _ALL)


def test_a_withheld_summary_sentence_is_a_leak_and_a_selected_one_is_not():
    incident = SEED["inc_0010"]
    first = sentences(incident.summary)[0]
    with pytest.raises(CaseError, match="withheld summary sentence 0"):
        check_leaks(first, incident, SignalSelection(summary_sentences=[1], events=[]))
    check_leaks(first, incident, SignalSelection(summary_sentences=[0, 1], events=[]))


def test_the_word_other_is_not_a_leaked_failure_mode():
    incident = SEED["inc_0018"]
    check_leaks(
        "Instances of the same services on other hosts were unaffected.",
        incident,
        SignalSelection(summary_sentences=[0, 1], events=[e.id for e in incident.events]),
    )
    with pytest.raises(CaseError, match="the failure mode detail"):
        check_leaks(
            incident.true_failure_mode_detail or "",
            incident,
            SignalSelection(summary_sentences=[0, 1], events=[e.id for e in incident.events]),
        )


def _case_file(tmp_path: Path, change: Any) -> Path:
    """The committed file with one change applied."""
    data = json.loads(DEFAULT_SET.read_text(encoding="utf-8"))
    change(data)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _first(data: dict[str, Any]) -> dict[str, Any]:
    return data["cases"][0]


@pytest.mark.parametrize(
    ("change", "complaint"),
    [
        (lambda d: _first(d).update(incident_id="inc_9999"), "is not a seeded incident"),
        (lambda d: _first(d)["signals"]["events"].append("evt_0002_1"), "are not events of"),
        (lambda d: _first(d)["signals"].update(summary_sentences=[0, 7]), "no summary sentence"),
        (lambda d: _first(d).update(id="case_02"), "duplicate case ids"),
        (lambda d: _first(d).update(incident_id="inc_0003"), "used by more than one case"),
        (lambda d: (_first(d).pop("signals"), _first(d).pop("recall_query")), "no task"),
        (lambda d: d["cases"][-1].pop("recall_query"), "needs one"),
        (
            lambda d: d["cases"][-1].update(signals={"summary_sentences": [0], "events": []}),
            "signals need an incident",
        ),
        (
            lambda d: _first(d).update(signals={"summary_sentences": [], "events": []}),
            "no signals cannot be answered",
        ),
        (lambda d: _first(d).update(expected_mode="resource_exhaustion"), "Extra inputs"),
        (lambda d: d["cases"].__delitem__(slice(16, 18)), "too few diagnose cases"),
    ],
)
def test_a_case_file_that_would_make_a_score_meaningless_is_refused(
    tmp_path: Path, change: Any, complaint: str
):
    with pytest.raises(CaseError, match=complaint):
        load_cases(_case_file(tmp_path, change))


def test_the_guard_covers_what_is_withheld_and_curation_is_the_files_diff(tmp_path: Path):
    # Selecting the event that records the fix is a choice the guard cannot refuse: a
    # line that is shown is, by definition, not withheld. What keeps it out is that the
    # selection is a committed file, reviewed as a diff - and the test above this section
    # that holds the committed set to "the last event is never shown".
    def select_the_fix(data: dict[str, Any]) -> None:
        _first(data)["signals"]["events"] = ["evt_0001_4"]

    loaded = load_cases(_case_file(tmp_path, select_the_fix))
    shown = next(item for item in loaded.items if item.key == "case_01:diagnose")
    assert "Idempotency cache bound and TTL deployed" in shown.context


def test_the_answer_key_is_not_in_the_case_file():
    # One answer key: the seed. A case file that restated it could disagree with it.
    text = DEFAULT_SET.read_text(encoding="utf-8")
    for mode in FailureMode:
        if mode is not FailureMode.OTHER:
            assert mode.value not in text
    assert not re.search(r"\bsev[1-4]\b", text)


# ================================================================================ scoring


def test_a_right_diagnosis_is_correct_on_both_judgements():
    item = ITEMS["case_01:diagnose"]
    score = score_item(item, _diagnosed(item))
    assert score.correct is True and score.answered is True and score.error is None
    mode, severity = score.judgements
    assert (mode.field, mode.expected, mode.actual, mode.correct) == (
        "failure_mode",
        "resource_exhaustion",
        "resource_exhaustion",
        True,
    )
    assert (mode.confidence, mode.band) == (0.8, Band.SINGLE_SOURCE)
    assert (severity.expected, severity.actual, severity.correct) == ("sev2", "sev2", True)
    assert score.severity_within_one is True
    assert score.grounding.ungrounded == ()
    assert (score.incident_id, score.status, score.overall_confidence) == (
        "inc_0001",
        "partial",
        0.6,
    )


def test_a_wrong_diagnosis_is_wrong_and_not_an_abstention():
    item = ITEMS["case_07:diagnose"]  # truth: code_regression
    score = score_item(item, _diagnosed(item, mode="downstream_latency"))
    mode = score.judgements[0]
    assert score.correct is False
    assert (mode.actual, mode.correct, mode.abstained) == ("downstream_latency", False, False)


def test_an_abstention_is_not_correct_and_is_not_a_wrong_answer():
    item = ITEMS["case_08:diagnose"]
    score = score_item(
        item, _diagnosed(item, answer_truth=False, mode_confidence=0.2, severity="sev4")
    )
    mode = score.judgements[0]
    assert score.correct is False
    assert (mode.actual, mode.abstained, mode.band) == (None, True, Band.SPECULATION)


def test_the_other_mode_is_a_right_answer_when_it_is_the_truth():
    item = ITEMS["case_17:diagnose"]
    assert score_item(item, _diagnosed(item)).correct is True
    assert score_item(item, _diagnosed(item, mode="bad_config_deploy")).correct is False


@pytest.mark.parametrize(
    ("stated", "exact", "within_one"),
    [("sev2", True, True), ("sev1", False, True), ("sev3", False, True), ("sev4", False, False)],
)
def test_severity_is_scored_exactly_and_within_one_level(
    stated: str, exact: bool, within_one: bool
):
    item = ITEMS["case_01:diagnose"]  # truth: sev2
    score = score_item(item, _diagnosed(item, severity=stated))
    assert score.judgements[1].correct is exact
    assert score.severity_within_one is within_one
    assert score.correct is True  # the headline is the failure mode


def test_a_severity_off_the_scale_has_no_distance():
    item = ITEMS["case_01:diagnose"]
    payload = _diagnosis(item, severity="other")
    payload["findings"]["severity"]["detail"] = "Not a customer-facing incident."
    client = LLMClient(_SETTINGS, client=_Scripted([_message(EMIT_TOOL_NAME, payload)]))  # type: ignore[arg-type]
    response = IncidentAgent(client, max_validation_retries=0).diagnose(
        item.query, context=item.context
    )
    score = score_item(item, response)
    assert score.judgements[1].correct is False and score.severity_within_one is None


def test_services_are_scored_as_precision_and_recall():
    item = ITEMS["case_03:diagnose"]  # truth: postgres + the three services
    score = score_item(item, _diagnosed(item, services=["postgres", "checkout-api", "redis"]))
    assert score.services_precision == pytest.approx(2 / 3)
    assert score.services_recall == pytest.approx(2 / 4)
    none_named = score_item(item, _diagnosed(item, services=[]))
    assert none_named.services_precision is None and none_named.services_recall == 0.0


def test_a_grounded_diagnosis_has_nothing_ungrounded():
    item = ITEMS["case_01:diagnose"]
    shown = {"error_rate_before": 0.001, "error_rate_after": 0.074, "requests_affected": 48200}
    score = score_item(item, _diagnosed(item, impact=shown))
    assert score.grounding.ungrounded == ()
    # one excerpt, one service, one timeline event, three impact numbers
    assert score.grounding.checked == 6


def test_a_reflowed_excerpt_is_still_verbatim():
    item = ITEMS["case_01:diagnose"]
    reflowed = _first_symptom(item).replace(" over two hours", "\n  over two hours")
    assert score_item(item, _diagnosed(item, excerpt=reflowed)).grounding.ungrounded == ()


@pytest.mark.parametrize(
    ("overrides", "complaint"),
    [
        ({"excerpt": "Memory grew by roughly eight times."}, "evidence ev_1: excerpt is not in"),
        ({"services": ["payments-api", "billing-api"]}, "affected service 'billing-api'"),
        ({"timeline_at": "2026-01-14T09:00:00Z"}, "no signal at 2026-01-14T09:00:00Z"),
        ({"similar": ["inc_0042"]}, "similar incident 'inc_0042'"),
        ({"impact": {"error_rate_after": 0.08}}, "impact.error_rate_after: stated 0.08"),
    ],
)
def test_each_kind_of_invented_detail_is_found(overrides: dict[str, Any], complaint: str):
    item = ITEMS["case_01:diagnose"]
    score = score_item(item, _diagnosed(item, **overrides))
    assert len(score.grounding.ungrounded) == 1
    assert complaint in score.grounding.ungrounded[0]
    assert score.correct is True  # invented detail and a wrong answer are separate results


def test_a_number_for_a_metric_that_was_not_measured_is_invented():
    item = ITEMS["case_06:diagnose"]  # every rate and latency is unmeasured in the seed
    score = score_item(item, _diagnosed(item, impact={"p99_latency_ms_after": 900}))
    assert score.grounding.ungrounded == (
        "impact.p99_latency_ms_after: stated 900, the context shows not measured",
    )


def test_a_duration_is_derived_not_invented():
    item = ITEMS["case_01:diagnose"]
    score = score_item(item, _diagnosed(item, impact={"duration_seconds": 7620}))
    assert score.grounding.ungrounded == ()


def test_a_recall_that_cites_the_incidents_own_post_mortem_is_correct():
    item = ITEMS["case_01:recall"]
    response = _recalled(item, "inc_0001", retrieved=("inc_0001", "inc_0003"))
    score = score_item(item, response, retrieved=("doc_0001", "doc_0003"))
    assert score.correct is True
    assert score.cited_documents == ("doc_0001",)
    assert (score.retrieved_expected, score.supported_claims) == (True, 1)
    assert score.invented_precedent is None
    assert (score.tool_calls, score.tool_calls_ok) == (1, 1)
    assert score.grounding.checked == 1 and score.grounding.ungrounded == ()


def test_a_recall_that_cites_another_incident_is_the_agents_miss():
    item = ITEMS["case_01:recall"]
    response = _recalled(item, "inc_0003", retrieved=("inc_0001", "inc_0003"))
    score = score_item(item, response, retrieved=("doc_0001", "doc_0003"))
    assert score.correct is False
    assert score.retrieved_expected is True  # it was there to be cited


def test_a_recall_retrieval_never_offered_the_document_is_retrievals_miss():
    item = ITEMS["case_01:recall"]
    response = _recalled(item, "inc_0003", retrieved=("inc_0003",))
    score = score_item(item, response, retrieved=("doc_0003",))
    assert (score.correct, score.retrieved_expected) == (False, False)
    assert score_item(item, response).retrieved_expected is None  # not recorded: not known


def test_an_unsupported_claim_cites_nothing():
    item = ITEMS["case_01:recall"]
    response = _recalled(item, None, retrieved=("inc_0001",))
    score = score_item(item, response, retrieved=("doc_0001",))
    assert score.cited_documents == () and score.correct is False


def test_a_probe_is_right_when_it_declines_to_answer():
    item = ITEMS["case_19:recall"]
    score = score_item(item, _recalled(item, None, retrieved=("inc_0009",)))
    assert item.incident is None and score.incident_id is None
    assert (score.correct, score.invented_precedent) == (True, False)
    assert score.retrieved_expected is None


def test_a_probe_answered_from_a_post_mortem_is_an_invented_precedent():
    item = ITEMS["case_19:recall"]
    response = _recalled(
        item, "inc_0009", retrieved=("inc_0009",), answer="DNS failed during settlement."
    )
    score = score_item(item, response)
    assert (score.correct, score.invented_precedent) == (False, True)


def test_a_response_of_the_wrong_kind_is_a_type_error():
    diagnosis = _diagnosed(ITEMS["case_01:diagnose"])
    with pytest.raises(TypeError, match="expected a docs response"):
        score_item(ITEMS["case_01:recall"], diagnosis)


def test_an_agent_that_raised_is_a_result_with_its_reason():
    error = ValueError("the report was refused\nagain")
    error.add_note("validation-retry loop: 3 attempt(s)")
    score = score_failure(ITEMS["case_01:diagnose"], error)
    assert (score.answered, score.correct) == (False, None)
    assert score.error == (
        "ValueError: the report was refused again | validation-retry loop: 3 attempt(s)"
    )


def _scores() -> list[ItemScore]:
    """Six diagnoses with known results, two recalls, two probes, one failure."""
    diagnoses = [
        ("case_01:diagnose", {"mode_confidence": 0.92}),  # right, top band
        ("case_02:diagnose", {"mode_confidence": 0.75}),  # right
        ("case_05:diagnose", {"mode": "bad_config_deploy", "mode_confidence": 0.75}),  # wrong
        ("case_07:diagnose", {"mode": "downstream_latency", "mode_confidence": 0.55}),  # wrong
        ("case_08:diagnose", {"answer_truth": False, "mode_confidence": 0.2, "severity": "sev2"}),
        ("case_17:diagnose", {"services": ["payments-api", "billing-api"]}),  # right, invented
    ]
    scores = [score_item(ITEMS[key], _diagnosed(ITEMS[key], **kw)) for key, kw in diagnoses]
    hit = ITEMS["case_01:recall"]
    scores.append(
        score_item(
            hit, _recalled(hit, "inc_0001", retrieved=("inc_0001",)), retrieved=("doc_0001",)
        )
    )
    miss = ITEMS["case_02:recall"]
    scores.append(
        score_item(
            miss, _recalled(miss, "inc_0001", retrieved=("inc_0001",)), retrieved=("doc_0001",)
        )
    )
    declined = ITEMS["case_19:recall"]
    scores.append(score_item(declined, _recalled(declined, None, retrieved=("inc_0009",))))
    invented = ITEMS["case_20:recall"]
    scores.append(score_item(invented, _recalled(invented, "inc_0009", retrieved=("inc_0009",))))
    scores.append(score_failure(ITEMS["case_03:diagnose"], RuntimeError("refused")))
    return scores


def test_the_summary_counts_what_it_says_it_counts():
    summary = summarise(_scores())
    assert summary.items == 11
    assert summary.answered.render() == "10/11 = 91%"
    # accuracy: three of six diagnoses right; the abstention is one of the three not right
    assert (summary.failure_mode.hits, summary.failure_mode.total) == (3, 6)
    assert summary.failure_mode_abstained == 1
    assert {mode: (r.hits, r.total) for mode, r in summary.failure_mode_by_mode.items()} == {
        "code_regression": (0, 3),
        "other": (1, 1),
        "resource_exhaustion": (2, 2),
    }
    assert (summary.recall_cited.hits, summary.recall_cited.total) == (1, 2)
    assert (summary.recall_retrieved.hits, summary.recall_retrieved.total) == (1, 2)
    assert (summary.probes_abstained.hits, summary.probes_abstained.total) == (1, 2)
    # hallucination: one invented service among the diagnoses, one invented precedent
    assert summary.ungrounded.hits == 1
    assert (summary.items_with_ungrounded.hits, summary.items_with_ungrounded.total) == (1, 6)
    assert (summary.invented_precedents.hits, summary.invented_precedents.total) == (1, 2)
    # tools: the four recalls each made one retrieval call
    assert (summary.tool_success.hits, summary.tool_success.total) == (4, 4)


def test_the_failure_is_in_the_denominator_of_nothing_it_did_not_answer():
    only_failure = summarise([score_failure(ITEMS["case_03:diagnose"], RuntimeError("x"))])
    assert only_failure.answered.render() == "0/1 = 0%"
    assert only_failure.failure_mode.value is None
    assert only_failure.failure_mode.render() == "n/a (0)"
    assert only_failure.tool_success.value is None
    assert only_failure.services_precision is None


def test_calibration_is_accuracy_per_contract_band():
    judgements = [j for score in _scores() for j in score.judgements]
    rows = {row.band: row for row in calibrate(judgements)}
    # failure mode: 0.92 right | 0.75 right, 0.75 wrong, 0.80 right | 0.55 wrong | abstained
    # severity: six stated at 0.75, all the truth except case_08's sev2 against sev4
    assert (rows[Band.TWO_SOURCES].judgements, rows[Band.TWO_SOURCES].correct) == (1, 1)
    assert (rows[Band.SINGLE_SOURCE].judgements, rows[Band.SINGLE_SOURCE].correct) == (9, 7)
    assert (rows[Band.INFERRED].judgements, rows[Band.INFERRED].correct) == (1, 0)
    assert rows[Band.HYPOTHESIS].judgements == 0 and rows[Band.HYPOTHESIS].accuracy is None
    # An abstention states no conclusion, so it has no confidence to calibrate.
    assert rows[Band.SPECULATION].judgements == 0
    assert rows[Band.SINGLE_SOURCE].accuracy == pytest.approx(7 / 9)
    assert rows[Band.SINGLE_SOURCE].mean_confidence == pytest.approx(
        (0.75 * 2 + 0.8 + 0.75 * 6) / 9
    )


def test_tool_success_counts_a_failed_call():
    item = ITEMS["case_01:recall"]
    ok = _recalled(item, "inc_0001", retrieved=("inc_0001",))
    failed = ok.model_copy(
        update={
            "tool_calls": [
                ToolCallRef.model_validate(
                    {
                        **ok.tool_calls[0].model_dump(),
                        "ok": False,
                        "error_class": "transient",
                    }
                )
            ]
        }
    )
    rate = tool_success([ok, failed, _diagnosed(ITEMS["case_01:diagnose"])])
    assert (rate.hits, rate.total) == (1, 2)
    assert tool_success([]).value is None


# ================================================================================= runner


def _item_of(params: dict[str, Any]) -> EvalItem:
    """Which item a request is for, read off the prompt the agent built."""
    prompt = params["messages"][0]["content"]
    tool = params["tool_choice"]["name"]
    task = Task.DIAGNOSE if tool == EMIT_TOOL_NAME else Task.RECALL
    found = [
        item
        for item in CASES.items
        if item.task is task and item.context in prompt and prompt.endswith(item.query)
    ]
    if task is Task.DIAGNOSE:
        (item,) = found
        return item
    assert found, prompt[-120:]
    return found[0]


# What the scripted model gets wrong. Everything else it answers with the truth.
_WRONG_MODE = {"case_07:diagnose": "downstream_latency"}
_WRONG_DOC = {"case_02:recall": "inc_0001"}


def _answer(params: dict[str, Any]) -> SimpleNamespace:
    item = _item_of(params)
    if item.task is Task.DIAGNOSE:
        return _message(EMIT_TOOL_NAME, _diagnosis(item, mode=_WRONG_MODE.get(item.key)))
    if item.incident is None:
        return _message(DOCS_EMIT_TOOL_NAME, _recall(None))
    cite = SEED[_WRONG_DOC.get(item.key, item.incident.id)]
    return _message(DOCS_EMIT_TOOL_NAME, _recall(cite))


class _Model:
    """An Anthropic client that answers each request for the item it is about."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return _answer(kwargs)


class _Corpus:
    """Retrieval that finds the incident a recall question belongs to, plus inc_0001."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._by_query = {item.query: item.incident for item in CASES.select(tasks={Task.RECALL})}

    def search(self, query: str, *, k: int = 5) -> RetrievalResult:
        self.calls.append(query)
        incident = self._by_query[query]
        wanted = {"inc_0001"} | ({incident.id} if incident is not None else set())
        return RetrievalResult(
            query=query,
            docs=[_document(SEED[incident_id]) for incident_id in sorted(wanted)],
            documents_searched=18,
            corpus_snapshot="ingest_test",
            mode="hybrid",
            degraded=None,
        )


_SUBSET = ("case_01", "case_02", "case_07", "case_19")


def _subset() -> list[EvalItem]:
    return list(CASES.select(cases=set(_SUBSET)))


def test_the_runner_scores_the_shipped_agents_item_by_item():
    model, corpus = _Model(), _Corpus()
    said: list[str] = []
    run = run_realtime(
        _subset(),
        LLMClient(_SETTINGS, client=model),  # type: ignore[arg-type]
        corpus,
        progress=said.append,
    )
    assert [r.item.key for r in run.results] == [item.key for item in _subset()]
    assert (run.mode, run.model, run.prompt_caching, run.cache_ttl) == (
        "realtime",
        "claude-sonnet-5",
        True,
        "5m",
    )
    correct = {score.key: score.correct for score in run.scores}
    assert correct == {
        "case_01:diagnose": True,
        "case_01:recall": True,
        "case_02:diagnose": True,
        "case_02:recall": False,
        "case_07:diagnose": False,
        "case_07:recall": True,
        "case_19:recall": True,
    }
    assert len(model.calls) == 7  # one model call an item, no retries
    assert len(said) == 7 and "case_07:diagnose" in said[4] and "wrong" in said[4]
    # Responses are kept, and named for their case.
    first = run.results[0].response
    assert isinstance(first, IncidentAgentResponse)
    assert (first.request_id, first.invocation_id) == ("req_case_01", "inv_case_01_diagnose")


def test_each_agent_is_handed_its_context_explicitly_and_nothing_else():
    model = _Model()
    run_realtime(_subset()[:2], LLMClient(_SETTINGS, client=model), _Corpus())  # type: ignore[arg-type]
    diagnose, recall = model.calls
    item = ITEMS["case_01:diagnose"]
    assert diagnose["messages"][0]["content"] == (
        f"<context>\n{item.context}\n</context>\n\nOperational query: {item.query}"
    )
    assert diagnose["tool_choice"] == {"type": "tool", "name": EMIT_TOOL_NAME}
    # The answer key stays out of the prompt: nothing but the item reaches the agent.
    assert item.incident is not None
    assert item.incident.true_root_cause not in json.dumps(diagnose, default=str)
    assert recall["tool_choice"] == {"type": "tool", "name": DOCS_EMIT_TOOL_NAME}
    assert ITEMS["case_01:recall"].context in recall["messages"][0]["content"]


def test_tokens_and_retries_are_attributed_to_the_item_that_spent_them():
    run = run_realtime(_subset(), LLMClient(_SETTINGS, client=_Model()), _Corpus())  # type: ignore[arg-type]
    for score in run.scores:
        assert (score.usage.input_tokens, score.usage.output_tokens) == (4_200, 900)
        assert score.usage.cache_read_tokens == 4_000
        assert (score.retry.attempts, score.retry.outcome) == (1, "accepted")
        assert score.seconds is not None and score.seconds >= 0
    total = summarise(run.scores).usage
    assert (total.input_tokens, total.cache_read_tokens) == (7 * 4_200, 7 * 4_000)


def test_retrieval_is_recorded_so_a_miss_can_be_blamed_correctly():
    corpus = _Corpus()
    run = run_realtime(_subset(), LLMClient(_SETTINGS, client=_Model()), corpus)  # type: ignore[arg-type]
    by_key = {result.item.key: result for result in run.results}
    miss = by_key["case_02:recall"]
    assert miss.retrieved == ("doc_0001", "doc_0003")
    assert miss.score.retrieved_expected is True and miss.score.correct is False
    assert by_key["case_01:diagnose"].retrieved is None
    assert len(corpus.calls) == 4  # one search for each recall


def test_a_refused_report_is_retried_and_the_retry_is_counted():
    item = ITEMS["case_01:diagnose"]
    broken = _diagnosis(item)
    broken["findings"]["severity"]["detail"] = "not allowed here"
    fake = _Scripted([_message(EMIT_TOOL_NAME, broken), _message(EMIT_TOOL_NAME, _diagnosis(item))])
    run = run_realtime([item], LLMClient(_SETTINGS, client=fake))  # type: ignore[arg-type]
    (score,) = run.scores
    assert score.correct is True
    assert (score.retry.attempts, score.retry.rejections, score.retry.outcome) == (
        2,
        ("format",),
        "recovered",
    )
    assert score.usage.output_tokens == 1_800  # both attempts cost
    summary = summarise(run.scores)
    assert summary.retries == {
        "accepted_first_try": 0,
        "recovered": 1,
        "exhausted": 0,
        "retry_calls": 1,
    }


def test_an_agent_that_gives_up_is_a_scored_item_and_the_run_goes_on():
    first, second = ITEMS["case_01:diagnose"], ITEMS["case_02:diagnose"]
    broken = _diagnosis(first)
    broken["findings"]["severity"]["detail"] = "not allowed here"
    fake = _Scripted(
        [
            _message(EMIT_TOOL_NAME, broken),
            _message(EMIT_TOOL_NAME, copy.deepcopy(broken)),
            _message(EMIT_TOOL_NAME, _diagnosis(second)),
        ]
    )
    run = run_realtime([first, second], LLMClient(_SETTINGS, client=fake))  # type: ignore[arg-type]
    failed, fine = run.scores
    assert (failed.answered, failed.correct) == (False, None)
    assert failed.error is not None and "identical" in failed.error
    assert (failed.retry.outcome, failed.retry.rejections) == ("exhausted", ("format", "format"))
    assert failed.usage.output_tokens == 1_800  # a refused report still cost
    assert fine.correct is True
    assert run.results[0].response is None


def test_a_recall_without_a_retriever_is_a_failed_item_that_says_so():
    run = run_realtime([ITEMS["case_01:recall"]], LLMClient(_SETTINGS, client=_Model()))  # type: ignore[arg-type]
    assert run.scores[0].answered is False
    assert "needs a retriever" in (run.scores[0].error or "")


def test_retrieval_is_asked_once_for_each_question():
    inner = _Corpus()
    recording = RecordingRetriever(inner)
    query = ITEMS["case_01:recall"].query
    assert recording.retrieved(query) is None  # not searched yet: not known
    first = recording.search(query, k=5)
    assert recording.search(query, k=5) is first
    assert inner.calls == [query]
    assert recording.retrieved(query) == ("doc_0001",)
    assert recording.degraded() == []


def test_degraded_retrieval_is_reported_with_the_run():
    class _Lexical(_Corpus):
        def search(self, query: str, *, k: int = 5) -> RetrievalResult:
            result = super().search(query, k=k)
            return RetrievalResult(
                query=result.query,
                docs=result.docs,
                documents_searched=18,
                corpus_snapshot=None,
                mode="lexical",
                degraded="no embedding provider configured",
            )

    run = run_realtime(
        [ITEMS["case_01:recall"]],
        LLMClient(_SETTINGS, client=_Model()),  # type: ignore[arg-type]
        _Lexical(),
    )
    assert run.retrieval_degraded == ["no embedding provider configured"]


# ---------------------------------------------------------------------- the batch path


def _batch_api(answer: Any = None) -> _FakeBatches:
    return _FakeBatches(lambda cid, params: _succeeded(cid, (answer or _answer)(params)))


def test_a_batch_gives_the_scores_realtime_gives():
    realtime = run_realtime(_subset(), LLMClient(_SETTINGS, client=_Model()), _Corpus())  # type: ignore[arg-type]

    batches = _batch_api()
    batcher, _ = _batcher(batches)
    said: list[str] = []
    corpus = _Corpus()
    batch = run_batch(_subset(), DeferredClient(_SETTINGS), batcher, corpus, progress=said.append)

    assert batch.mode == "batch"
    assert [s.key for s in batch.scores] == [s.key for s in realtime.scores]
    assert [s.correct for s in batch.scores] == [s.correct for s in realtime.scores]
    assert [s.usage.as_record() for s in batch.scores] == [
        s.usage.as_record() for s in realtime.scores
    ]
    # One batch carried all seven requests, and each is the request realtime would send.
    assert len(batches.created) == 1 and len(batches.created[0]) == 7
    assert [(b.requests, b.succeeded, b.failed) for b in batch.batches] == [(7, 7, 0)]
    assert any("7 request(s) submitted as one batch" in line for line in said)
    # Replayed once per pass, searched once per question.
    assert len(corpus.calls) == 4
    # A batched item's own clock measures the replay, so it is not reported as latency.
    assert all(score.seconds is None for score in batch.scores)


def test_a_retry_in_a_batch_is_a_second_batch_of_one():
    refused: set[str] = set()

    def answer(params: dict[str, Any]) -> SimpleNamespace:
        item = _item_of(params)
        if item.key == "case_02:diagnose" and len(params["messages"]) == 1:
            refused.add(item.key)
            broken = _diagnosis(item)
            broken["findings"]["severity"]["detail"] = "not allowed here"
            return _message(EMIT_TOOL_NAME, broken)
        return _answer(params)

    batches = _batch_api(answer)
    batcher, _ = _batcher(batches)
    run = run_batch(_subset(), DeferredClient(_SETTINGS), batcher, _Corpus())

    assert refused == {"case_02:diagnose"}
    assert [len(requests) for requests in batches.created] == [7, 1]
    by_key = {score.key: score for score in run.scores}
    retried = by_key["case_02:diagnose"]
    assert retried.correct is True
    assert (retried.retry.attempts, retried.retry.outcome) == (2, "recovered")
    assert retried.usage.output_tokens == 1_800
    assert by_key["case_01:diagnose"].usage.output_tokens == 900
    assert len(run.batches) == 2


def test_a_request_the_batch_failed_is_a_failed_item():
    def answer(cid: str, params: dict[str, Any]) -> Any:
        if _item_of(params).key == "case_07:diagnose":
            error = SimpleNamespace(error=SimpleNamespace(type="api_error", message="overloaded"))
            return SimpleNamespace(
                custom_id=cid, result=SimpleNamespace(type="errored", error=error)
            )
        return _succeeded(cid, _answer(params))

    batches = _FakeBatches(answer)
    batcher, _ = _batcher(batches)
    run = run_batch(_subset(), DeferredClient(_SETTINGS), batcher, _Corpus())
    by_key = {score.key: score for score in run.scores}
    assert by_key["case_07:diagnose"].answered is False
    assert "errored: api_error: overloaded" in (by_key["case_07:diagnose"].error or "")
    assert sum(1 for score in run.scores if score.answered) == 6
    assert [(b.succeeded, b.failed) for b in run.batches] == [(6, 1)]


# ------------------------------------------------------- a refused credential stops it


def _refused() -> anthropic.AuthenticationError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.AuthenticationError(
        "Error code: 401 - API key is invalid.",
        response=httpx.Response(401, request=request),
        body=None,
    )


class _Revoked(_Model):
    """Answers ``good`` requests, then refuses the credential on every one after."""

    def __init__(self, good: int) -> None:
        super().__init__()
        self._good = good

    def _create(self, **kwargs: Any) -> Any:
        if len(self.calls) >= self._good:
            self.calls.append(kwargs)
            raise _refused()
        return super()._create(**kwargs)


def test_a_refused_credential_stops_the_run_instead_of_failing_every_item():
    # The first live run of this harness recorded five failed items for one revoked key.
    model = _Revoked(good=2)
    client = LLMClient(_SETTINGS, client=model)  # type: ignore[arg-type]
    with pytest.raises(EvalAborted) as caught:
        run_realtime(_subset(), client, _Corpus())
    assert caught.value.completed == 2
    assert "case_02:diagnose" in str(caught.value)
    assert "AuthenticationError" in str(caught.value)
    assert "HANDOFF.md sec 7 item 10" in str(caught.value)
    assert len(model.calls) == 3  # the refusal, and then nothing


def test_a_batch_that_cannot_be_submitted_stops_the_run():
    class _Locked(_FakeBatches):
        def create(self, *, requests: list[dict[str, Any]]) -> SimpleNamespace:
            raise _refused()

    batcher, _ = _batcher(_Locked())
    with pytest.raises(EvalAborted, match="batch submission 1") as caught:
        run_batch(_subset(), DeferredClient(_SETTINGS), batcher, _Corpus())
    assert caught.value.completed == 0


# ================================================================================= report


def test_the_record_is_json_and_names_the_set_it_was_run_on():
    run = run_realtime(_subset(), LLMClient(_SETTINGS, client=_Model()), _Corpus())  # type: ignore[arg-type]
    record = json.loads(json.dumps(to_record(run, CASES)))
    assert record["set"] == {"name": "seeded-incidents", "version": 1, "sha256": CASES.sha256}
    assert record["run"]["mode"] == "realtime" and record["run"]["batches"] == []
    assert len(record["items"]) == 7
    assert record["summary"]["accuracy"]["failure_mode"] == {
        "hits": 2,
        "total": 3,
        "rate": pytest.approx(2 / 3),
    }
    assert record["items"][0]["usage"] == {
        "in": 4_200,
        "out": 900,
        "cache_read": 4_000,
        "cache_write": 0,
    }


def test_the_same_tokens_are_priced_three_ways():
    run = run_realtime(_subset(), LLMClient(_SETTINGS, client=_Model()), _Corpus())  # type: ignore[arg-type]
    summary = summarise(run.scores)
    cost = cost_view(run, summary)
    flat = Usage(input_tokens=7 * 4_200, output_tokens=7 * 900)
    assert cost.realtime_uncached == pytest.approx(price("claude-sonnet-5", flat))
    assert cost.without_cache == cost.realtime_uncached  # realtime: the mode changes nothing
    assert cost.as_run == pytest.approx(price("claude-sonnet-5", summary.usage))
    assert cost.as_run < cost.realtime_uncached  # type: ignore[operator]
    assert cost.per_item == pytest.approx(cost.as_run / 7)  # type: ignore[operator]


def test_a_batch_is_priced_at_half_and_the_baseline_is_still_realtime():
    batcher, _ = _batcher(_batch_api())
    run = run_batch(_subset(), DeferredClient(_SETTINGS), batcher, _Corpus())
    cost = cost_view(run, summarise(run.scores))
    assert cost.without_cache == pytest.approx(cost.realtime_uncached / 2)  # type: ignore[operator]
    assert cost.as_run == pytest.approx(price("claude-sonnet-5", summarise(run.scores).usage) / 2)  # type: ignore[operator]


def test_an_unpriced_model_is_reported_unpriced():
    settings = _SETTINGS.model_copy(update={"model": "some-other-model"})
    run = run_realtime(_subset()[:1], LLMClient(settings, client=_Model()))  # type: ignore[arg-type]
    cost = cost_view(run, summarise(run.scores))
    assert cost.to_dict() == {
        "as_run_usd": None,
        "without_cache_usd": None,
        "realtime_uncached_usd": None,
        "per_item_usd": None,
    }
    assert "unpriced" in render_markdown(run, CASES)


def test_the_report_says_what_was_measured_and_shows_its_working():
    batcher, _ = _batcher(_batch_api())
    run = run_batch(_subset(), DeferredClient(_SETTINGS), batcher, _Corpus())
    report = render_markdown(run, CASES, heading="Eval run: test")
    assert report.startswith("# Eval run: test\n")
    for section in (
        "## Accuracy",
        "## Hallucination",
        "## Tool success",
        "## Calibration",
        "## Validation retries",
        "## Cost",
        "## Items",
    ):
        assert section in report
    assert "| Failure mode matches the recorded truth | 2/3 = 67% |" in report
    assert "| Recall cites the incident's own post-mortem | 2/3 = 67% |" in report
    assert "| No-precedent probes answered with no answer | 1/1 = 100% |" in report
    assert "| `case_07:diagnose` | code_regression | downstream_latency | 0.80 | no |" in report
    assert "| `case_19:recall` | no precedent | nothing cited |" in report
    assert "| Realtime, no cache |" in report and "| baseline |" in report
    assert "| `msgbatch_1` | 7 | 7 | 0 |" in report
    assert "Mode: batch, prompt caching on (5m TTL)." in report
    # House style: no em dash, anywhere.
    assert chr(0x2014) not in report


def test_the_report_lists_what_was_refused_and_what_was_invented():
    item, other = ITEMS["case_01:diagnose"], ITEMS["case_02:diagnose"]
    broken = _diagnosis(other)
    broken["findings"]["severity"]["detail"] = "not allowed here"
    fake = _Scripted(
        [
            _message(EMIT_TOOL_NAME, _diagnosis(item, services=["payments-api", "billing-api"])),
            _message(EMIT_TOOL_NAME, broken),
            _message(EMIT_TOOL_NAME, copy.deepcopy(broken)),
        ]
    )
    run = run_realtime([item, other], LLMClient(_SETTINGS, client=fake))  # type: ignore[arg-type]
    report = render_markdown(run, CASES)
    assert "## What was refused or ungrounded" in report
    assert "- `case_01:diagnose`: affected service 'billing-api' is not in the context" in report
    assert "- `case_02:diagnose` produced no response: ValidationError" in report
    assert "| `case_02:diagnose` | - | no response | - | failed |" in report


def test_an_empty_measurement_is_reported_as_not_measured():
    run = run_realtime(
        [ITEMS["case_01:diagnose"]],
        LLMClient(_SETTINGS, client=_Model()),  # type: ignore[arg-type]
    )
    report = render_markdown(run, CASES)
    assert "| Recall cites the incident's own post-mortem | n/a (0) |" in report
    assert "| Tool calls that returned ok | n/a (0) |" in report


# ================================================================================ the seed path


def test_the_seed_path_points_at_the_committed_file():
    assert SEED_PATH.name == "03-seed-incidents.sql"
    assert SEED_PATH.is_file()
