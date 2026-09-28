"""Day 18 - field-level confidence.

The band table is pinned to the contract's text and to the prompt every agent carries;
the reading walks every judgement in a response (assessments anywhere in the findings,
plus the Docs agent's claims), and the two flags are the band table read literally.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from aioc.agents.incident import _CONFIDENCE_BANDS
from aioc.contracts import CoordinatorResponse, DocsAgentResponse, IncidentAgentResponse
from aioc.coordinator import (
    BANDS,
    Band,
    Flag,
    band,
    digest,
    profile,
    request_profile,
)
from aioc.coordinator.confidence import band_meaning, digest_line, render_bands
from tests.test_contract import _worked_example
from tests.test_handoff import _example_responses

_CONTRACT = Path(__file__).resolve().parents[1] / "docs" / "CONTRACTS.md"


# ----------------------------------------------------------------------------- the bands


def _contract_band_rows() -> list[tuple[float, str]]:
    """The sec 2.1 band table as (lower bound, meaning), from the contract text itself."""
    text = _CONTRACT.read_text(encoding="utf-8")
    start = text.index("**Confidence bands.**")
    end = text.index("**Invariants**", start)
    rows: list[tuple[float, str]] = []
    for line in text[start:end].splitlines():
        m = re.match(r"^\| `?(?:below `)?(\d\.\d\d)`?[^|]*\| (.+?) \|$", line)
        if m:
            rows.append((float(m.group(1)), m.group(2)))
    return rows


def test_the_band_table_is_the_contracts_table():
    rows = _contract_band_rows()
    assert len(rows) == 5
    # Bounds: the first four rows state their lower bound; the last is "below 0.25".
    assert [lower for lower, _ in rows[:4]] == [lower for lower, _, _ in BANDS[:4]]
    assert rows[4][0] == BANDS[3][0]  # "below 0.25" is the floor of the hypothesis band
    # Meanings, verbatim, for the four stated bands (the fifth is a rule, worded per prompt).
    for (_, contract_meaning), (_, name, meaning) in zip(rows[:4], BANDS[:4], strict=True):
        assert contract_meaning == meaning == band_meaning(name)


def test_the_band_table_is_what_every_agent_is_prompted_with():
    for _, _, meaning in BANDS:
        assert meaning in _CONFIDENCE_BANDS


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [
        (1.0, Band.TWO_SOURCES),
        (0.90, Band.TWO_SOURCES),
        (0.899, Band.SINGLE_SOURCE),
        (0.70, Band.SINGLE_SOURCE),
        (0.69, Band.INFERRED),
        (0.50, Band.INFERRED),
        (0.49, Band.HYPOTHESIS),
        (0.25, Band.HYPOTHESIS),
        (0.249, Band.SPECULATION),
        (0.0, Band.SPECULATION),
    ],
)
def test_band_boundaries_are_inclusive_at_the_lower_bound(confidence: float, expected: Band):
    assert band(confidence) is expected


def test_a_confidence_outside_the_unit_interval_is_refused():
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        band(1.5)


def test_render_bands_lists_every_band_with_its_bound():
    lines = render_bands()
    assert len(lines) == 5 and lines[0].startswith("0.90+ two_sources:")


# --------------------------------------------------------------------------- the reading


def test_every_assessment_in_the_worked_incident_report_is_read_with_its_path():
    incident = _example_responses()["incident"]
    assert isinstance(incident, IncidentAgentResponse)
    p = profile(incident)

    assert p.agent == "incident" and p.invocation_id == incident.invocation_id
    paths = [f.path for f in p.fields]
    assert paths == ["findings.severity", "findings.failure_mode", "findings.root_cause"]
    assert all(f.kind == "assessment" for f in p.fields)
    assert p.highest is not None and p.highest.path == "findings.severity"
    assert p.lowest is not None and p.lowest.path == "findings.root_cause"
    assert p.lowest.band is Band.INFERRED and p.highest.band is Band.SINGLE_SOURCE
    assert p.by_band[Band.SINGLE_SOURCE] == 2 and p.by_band[Band.INFERRED] == 1
    assert p.nulls == () and p.flags == () and p.flagged == ()
    assert p.overall_band is band(incident.overall_confidence)


def test_docs_claims_are_judgements_too_with_their_documents_as_evidence():
    docs = _example_responses()["docs"]
    assert isinstance(docs, DocsAgentResponse)
    p = profile(docs)

    by_path = {f.path: f for f in p.fields}
    assert by_path["findings.answer"].kind == "assessment"
    claim_1, claim_2 = by_path["findings.claims[0]"], by_path["findings.claims[1]"]
    assert claim_1.kind == "claim" and claim_1.evidence == ("doc_012",) and not claim_1.null
    assert claim_2.null  # unsupported: it states nothing a reader can rely on
    assert claim_2.band is Band.SPECULATION and claim_2.flags == ()
    assert len(p.nulls) == 1 and p.stated == (by_path["findings.answer"], claim_1)


def test_a_two_sources_band_with_one_citation_is_flagged():
    payload = _example_responses()["docs"].model_dump(mode="json")
    payload["findings"]["answer"]["confidence"] = 0.93  # one evidence id
    payload["findings"]["claims"][0]["confidence"] = 0.95  # one source document
    p = profile(DocsAgentResponse.model_validate(payload))

    flagged = {f.path: f.flags for f in p.flagged}
    assert flagged == {
        "findings.answer": (Flag.TWO_SOURCES_BAND_UNDER_CITED,),
        "findings.claims[0]": (Flag.TWO_SOURCES_BAND_UNDER_CITED,),
    }


def test_two_citations_satisfy_the_two_sources_band():
    payload = _example_responses()["incident"].model_dump(mode="json")
    payload["findings"]["failure_mode"]["confidence"] = 0.95  # cites ev_1 and ev_2
    p = profile(IncidentAgentResponse.model_validate(payload))
    assert p.flagged == ()


def test_a_null_value_is_never_flagged_for_citations():
    payload = _example_responses()["incident"].model_dump(mode="json")
    payload["findings"]["root_cause"] = {
        "value": None,
        "confidence": 0.95,  # confidence that it cannot be determined
        "evidence": [],
        "reasoning": "nothing in the window names a cause",
        "detail": None,
    }
    payload["gaps"].append(
        {
            "id": "gap_rc",
            "description": "no cause",
            "kind": "missing_data",
            "kind_detail": None,
            "blocks_field": "findings.root_cause.value",
            "suggested_agent": None,
            "suggested_query": None,
            "resolvable": False,
        }
    )
    p = profile(IncidentAgentResponse.model_validate(payload))
    root = next(f for f in p.fields if f.path == "findings.root_cause")
    assert root.null and root.flags == ()
    assert root not in p.stated and p.lowest is not None and p.lowest.path != root.path


def test_an_overall_above_every_stated_field_is_flagged_and_equal_is_not():
    payload = _example_responses()["incident"].model_dump(mode="json")
    payload["overall_confidence"] = 0.95  # fields top out at 0.84
    assert profile(IncidentAgentResponse.model_validate(payload)).flags == (
        Flag.OVERALL_ABOVE_EVERY_FIELD,
    )
    payload["overall_confidence"] = 0.84
    assert profile(IncidentAgentResponse.model_validate(payload)).flags == ()


def test_the_request_profile_adds_the_coordinators_two_judgements():
    resp = CoordinatorResponse.model_validate(_worked_example())
    rp = request_profile(resp)

    assert rp.request_id == resp.request_id
    assert [p.agent for p in rp.agents] == ["incident", "docs"]
    assert rp.answer.path == "answer" and rp.answer.evidence == tuple(resp.answer.evidence)
    assert rp.intent.path == "intent" and rp.intent.flags == ()  # exempt from evidence
    assert len(rp.fields) == sum(len(p.fields) for p in rp.agents) + 2
    assert sum(rp.by_band.values()) == len(rp.fields)


def test_the_intent_is_exempt_from_the_citation_flag_but_a_confident_uncited_answer_is_not():
    payload = _worked_example()
    payload["intent"]["confidence"] = 0.97
    payload["intent"]["evidence"] = []
    payload["answer"]["confidence"] = 0.97  # cites whatever the example cites
    rp = request_profile(CoordinatorResponse.model_validate(payload))
    assert rp.intent.flags == ()
    assert (Flag.TWO_SOURCES_BAND_UNDER_CITED in rp.answer.flags) == (len(rp.answer.evidence) < 2)


# ----------------------------------------------------------------------- rendering


def test_the_profile_renders_one_line_per_judgement_and_serialises():
    p = profile(_example_responses()["incident"])
    lines = p.render()
    assert lines[0].startswith("incident inv_a1: overall 0.70 [single_source], 3 judgement(s)")
    assert any(
        line.strip().startswith("findings.root_cause: @0.68 inferred [ev_1, ev_2]")
        for line in lines
    )
    assert json.dumps(p.to_dict())  # JSON-clean for the run records
    rp = request_profile(CoordinatorResponse.model_validate(_worked_example()))
    rendered = "\n".join(rp.render())
    assert rendered.startswith("request req_8f21:") and "  answer: @" in rendered
    assert json.dumps(rp.to_dict())


def test_the_digest_line_names_the_lowest_and_highest_judgement_and_the_flags():
    incident = _example_responses()["incident"]
    line = digest_line(incident)
    assert line.startswith("confidence: 3 judgement(s), 0 null; ")
    assert "lowest findings.root_cause @0.68 [inferred]" in line
    assert "highest findings.severity @0.84 [single_source]" in line
    assert "flagged" not in line

    payload = incident.model_dump(mode="json")
    payload["overall_confidence"] = 0.99
    payload["findings"]["severity"]["confidence"] = 0.92
    flagged = digest_line(IncidentAgentResponse.model_validate(payload))
    assert (
        "flagged: findings.severity (two_sources_band_under_cited); overall_above_every_field"
        in flagged
    )


def test_every_digest_carries_the_confidence_line_after_the_summary():
    for response in _example_responses().values():
        text = digest(response)
        lines = text.splitlines()
        assert lines[1].startswith("summary: ") and lines[2].startswith("confidence: ")


def test_an_unsupported_claim_above_the_speculation_floor_is_flagged():
    """Found in the recorded Day 15 run: "the corpus contains no document about PR #11"
    reported as an unsupported claim at 0.90 - a negative stated with two-source confidence
    and nothing behind it. The worked example's unsupported claim sits at 0.10 and is not."""
    payload = _example_responses()["docs"].model_dump(mode="json")
    payload["findings"]["claims"][1]["confidence"] = 0.90
    p = profile(DocsAgentResponse.model_validate(payload))
    claim_2 = next(f for f in p.fields if f.path == "findings.claims[1]")
    assert claim_2.null and claim_2.flags == (Flag.UNSUPPORTED_CLAIM_ABOVE_FLOOR,)
    assert profile(_example_responses()["docs"]).flagged == ()
