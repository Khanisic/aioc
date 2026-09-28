"""Day 18 - Docs provenance: the claim -> source chain resolved to evidence and tool calls,
and every unanswered sub-question paired with the gap that reports it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import confidence_report
import pytest

from aioc.contracts import CoordinatorResponse, DocsAgentResponse, Gap
from aioc.coordinator import Band, coverage_gaps, digest, provenance
from aioc.coordinator.provenance import UNANSWERED_FIELD
from tests.test_contract import _worked_example
from tests.test_handoff import _example_responses


def _docs(**over: Any) -> DocsAgentResponse:
    payload = _example_responses()["docs"].model_dump(mode="json")
    payload.update(over)
    return DocsAgentResponse.model_validate(payload)


def _gap(gap_id: str, blocks: str | None, **over: Any) -> Gap:
    base: dict[str, Any] = {
        "id": gap_id,
        "description": f"{gap_id} description",
        "kind": "missing_data",
        "kind_detail": None,
        "blocks_field": blocks,
        "suggested_agent": None,
        "suggested_query": None,
        "resolvable": False,
    }
    base.update(over)
    return Gap.model_validate(base)


# ------------------------------------------------------------------- the worked example


def test_the_worked_examples_claim_traces_to_its_evidence_and_the_retrieval_call():
    p = provenance(_docs())

    assert p.invocation_id == "inv_a2"
    claim_1, claim_2 = p.claims
    assert claim_1.supported and claim_1.band is Band.SINGLE_SOURCE and claim_1.backs_answer
    (source,) = claim_1.sources
    assert source.document_id == "doc_012" and source.chunk_id == "doc_012#7"
    assert source.evidence_ids == ("ev_4",) and source.tool_call_ids == ("tc_3",)
    assert source.quote is not None and source.quote.startswith("Production requires")
    assert not claim_2.supported and claim_2.sources == () and not claim_2.backs_answer
    assert claim_2.band is Band.SPECULATION
    assert p.answer_evidence == ("ev_4",) and p.answer_documents == ("doc_012",)
    assert p.supported == (claim_1,) and p.unsupported == (claim_2,)


def test_the_worked_examples_unanswered_question_is_paired_with_its_gap():
    cov = provenance(_docs()).coverage
    assert cov.sub_questions == tuple(_docs().findings.coverage.sub_questions)
    assert len(cov.answered) == 1 and cov.answered_ratio == 0.5
    (gap,) = cov.unanswered
    assert gap.sub_question.startswith("Is there a documented post-deploy")
    assert gap.gap_id == "gap_3" and gap.resolvable is False and gap.suggested_agent is None
    assert cov.documents_cited == 1 and cov.cited_documents == ("doc_012",)
    assert cov.corpus_snapshot == "ingest_2026-07-20"


# ----------------------------------------------------------------- the source chain


def test_a_chunk_match_is_preferred_and_a_document_match_is_the_fallback():
    payload = _docs().model_dump(mode="json")
    # Two evidence entries for the same document: one at the claim's chunk, one at another.
    payload["evidence"].append(
        {**payload["evidence"][0], "id": "ev_9", "source_ref": "doc_012#2", "tool_call_id": "tc_9"}
    )
    p = provenance(DocsAgentResponse.model_validate(payload))
    (source,) = p.claims[0].sources
    assert source.evidence_ids == ("ev_4",)  # the chunk match, not both entries

    payload["findings"]["claims"][0]["sources"][0]["chunk_id"] = None
    p = provenance(DocsAgentResponse.model_validate(payload))
    (source,) = p.claims[0].sources
    assert source.evidence_ids == ("ev_4", "ev_9")  # document-level: every entry
    assert source.tool_call_ids == ("tc_3", "tc_9")


def test_a_source_no_evidence_entry_names_is_shown_with_none_rather_than_a_guess():
    payload = _docs().model_dump(mode="json")
    payload["evidence"][0]["source_ref"] = "doc_099#1"  # the answer now cites another doc
    p = provenance(DocsAgentResponse.model_validate(payload))
    (source,) = p.claims[0].sources
    assert source.evidence_ids == () and source.tool_call_ids == ()
    assert not p.claims[0].backs_answer and p.answer_documents == ("doc_099",)


def test_non_document_evidence_never_joins_the_chain():
    payload = _docs().model_dump(mode="json")
    payload["evidence"].append(
        {
            **payload["evidence"][0],
            "id": "ev_m",
            "source_type": "metric",
            "source_ref": "doc_012#7",  # a metric that happens to carry a doc-shaped ref
        }
    )
    p = provenance(DocsAgentResponse.model_validate(payload))
    assert p.claims[0].sources[0].evidence_ids == ("ev_4",)


# ------------------------------------------------------------------ coverage gaps


def test_an_indexed_gap_wins_and_the_rest_are_assigned_in_order():
    questions = ["q0", "q1", "q2"]
    gaps = [
        _gap("gap_b", UNANSWERED_FIELD),
        _gap("gap_1", f"{UNANSWERED_FIELD}[1]", resolvable=True),
        _gap("gap_a", UNANSWERED_FIELD),
        _gap("gap_other", "findings.answer.value"),  # not a coverage gap
    ]
    matched = coverage_gaps(questions, gaps)
    assert [(g.sub_question, g.gap_id) for g in matched] == [
        ("q0", "gap_b"),
        ("q1", "gap_1"),
        ("q2", "gap_a"),
    ]
    assert matched[1].resolvable is True and matched[0].resolvable is False


def test_a_question_no_gap_reports_gets_none_not_somebody_elses_gap():
    matched = coverage_gaps(["q0", "q1"], [_gap("gap_0", f"{UNANSWERED_FIELD}[0]")])
    assert matched[0].gap_id == "gap_0"
    assert matched[1].gap_id is None and matched[1].description is None
    assert matched[1].resolvable is None and matched[1].suggested_query is None


def test_nothing_unanswered_means_no_coverage_gaps():
    assert coverage_gaps([], [_gap("gap_x", UNANSWERED_FIELD)]) == ()


def test_a_resolvable_coverage_gap_carries_its_suggestion_for_the_loop():
    gap = _gap(
        "gap_r",
        UNANSWERED_FIELD,
        resolvable=True,
        suggested_agent="github",
        suggested_query="Which PR changed the pool ceiling?",
    )
    (matched,) = coverage_gaps(["q"], [gap])
    assert matched.suggested_agent is not None and matched.suggested_agent.value == "github"
    assert matched.suggested_query == "Which PR changed the pool ceiling?"


# ----------------------------------------------------------------------- rendering


def test_the_rendering_shows_the_chain_and_the_coverage_gap():
    lines = provenance(_docs()).render()
    text = "\n".join(lines)
    assert lines[0].startswith(
        "docs inv_a2: 1 supported claim(s), 1 unsupported; answer cites ev_4 -> doc_012"
    )
    assert "claim_1 [supported @0.88 single_source, backs the answer]" in text
    assert "<- doc_012#7 (checkout-api runbook) via ev_4 from tc_3" in text
    assert "claim_2 [UNSUPPORTED @0.10 speculation]" in text
    assert (
        "coverage: 1/2 sub-question(s) answered (50%); searched 18, retrieved 4, cited 1 (doc_012)"
        in text
    )
    assert (
        "UNANSWERED: Is there a documented post-deploy latency regression procedure?"
        "  <- gap_3 [unresolvable]"
    ) in text
    assert json.dumps(provenance(_docs()).to_dict())


def test_the_docs_digest_pairs_each_unanswered_question_with_its_gap():
    text = digest(_docs())
    assert "coverage: 1/2 sub-questions answered" in text
    assert "unanswered (1):" in text
    assert "gap=gap_3 unresolvable" in text


# ------------------------------------------------------------------------ the script


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    (run / "response.json").write_text(
        CoordinatorResponse.model_validate(_worked_example()).model_dump_json(),
        encoding="utf-8",
    )
    return run


def test_the_report_script_profiles_a_recorded_response_and_traces_its_docs(tmp_path: Path, capsys):
    assert confidence_report.main(["--run", str(_run_dir(tmp_path))]) == 0
    out = capsys.readouterr().out
    assert "request req_8f21:" in out
    assert "findings.root_cause: @0.68 inferred" in out
    assert "docs inv_a2: 1 supported claim(s)" in out
    assert "1 response(s), 8 judgement(s)" in out
    assert "coverage: 1/2 sub-question(s) answered across 1 docs report(s)" in out


def test_the_report_script_emits_json(tmp_path: Path, capsys):
    assert confidence_report.main(["--run", str(_run_dir(tmp_path)), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    (run,) = data["runs"]
    assert run["profile"]["request_id"] == "req_8f21"
    assert run["docs"][0]["coverage"]["unanswered"][0]["gap_id"] == "gap_3"
    assert data["totals"]["judgements"] == 8  # 3 incident, 3 docs, answer, intent


def test_the_report_script_skips_what_is_not_a_coordinator_response(tmp_path: Path, capsys):
    run = tmp_path / "old"
    run.mkdir()
    (run / "response.json").write_text("{}", encoding="utf-8")
    assert confidence_report.main(["--run", str(run)]) == 0
    assert "skipped old" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--json", None])
def test_the_report_script_handles_no_runs(tmp_path: Path, capsys, flag: str | None):
    argv = ["--results", str(tmp_path)] + ([flag] if flag else [])
    assert confidence_report.main(argv) == 0
