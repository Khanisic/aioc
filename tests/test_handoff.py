"""Day 13: the handoff digest - what a dependent invocation is told about its dependency.

Offline, no network. The digest is tested on its own here (shape, bounds, coverage of all
four agents); the executor tests prove it reaches the dependent as context and is recorded
verbatim. The Incident and Docs responses come from the contract's worked example, so the
digest is exercised on the canonical payloads rather than on fixtures that happen to agree
with them.
"""

from __future__ import annotations

from typing import Any

from pydantic import TypeAdapter

from aioc.contracts import (
    AgentInvocation,
    AgentName,
    AnyAgentResponse,
    DeploymentAgentResponse,
    DocsAgentResponse,
    GitHubAgentResponse,
    IncidentAgentResponse,
)
from aioc.coordinator import handoff as handoff_module
from aioc.coordinator.handoff import (
    HANDOFF_HEADER,
    MAX_DIGEST_CHARS,
    MAX_ITEMS,
    MAX_TEXT,
    ROSTER_HEADER,
    ROSTER_RULE,
    TRUNCATION_MARKER,
    compose_dependent_context,
    digest,
    roster_block,
    with_roster,
)
from tests.test_contract import _worked_example
from tests.test_executor import _github_response

_ANY = TypeAdapter(AnyAgentResponse)


def _example_responses() -> dict[str, Any]:
    return {r["agent"]: _ANY.validate_python(r) for r in _worked_example()["agent_responses"]}


def _deployment_response() -> DeploymentAgentResponse:
    return DeploymentAgentResponse.model_validate(
        {
            "agent": "deployment",
            "request_id": "req_x",
            "invocation_id": "inv_dep",
            "status": "partial",
            "status_detail": None,
            "summary": "Release 1522000 restarted checkout-api once; no regression evident.",
            "findings": {
                "service": "checkout-api",
                "environment": "development",
                "environment_detail": None,
                "releases_compared": {"from_version": "7e5c94f", "to_version": "1522000"},
                "rollout_status": {
                    "value": "degraded",
                    "confidence": 0.9,
                    "evidence": ["ev_health"],
                    "reasoning": "one restart and one failed scrape in the window",
                    "detail": None,
                },
                "changed_config_keys": ["DEMO_GIT_SHA", "SERVICE_NAME"],
                "image_changes": [
                    {
                        "container": "checkout-api",
                        "from_image": None,
                        "to_image": "aioc-demo-service:day3",
                        "from_digest": None,
                        "to_digest": None,
                    }
                ],
                "health_signals": {
                    "replicas_desired": 1,
                    "replicas_ready": 1,
                    "restart_count": 1,
                    "probe_failures": 1,
                    "error_rate": 0.0,
                    "p99_latency_ms": 19.1,
                    "observed_over_seconds": 1800,
                },
                "regression_suspected": {
                    "value": None,
                    "confidence": 0.2,
                    "evidence": [],
                    "reasoning": "no baseline to compare against",
                    "detail": None,
                },
                "rollback_recommendation": {
                    "value": "hold_and_monitor",
                    "confidence": 0.6,
                    "evidence": ["ev_health"],
                    "reasoning": "degraded by the redeploy itself",
                    "detail": None,
                },
                "approval": {
                    "requires_approval": True,
                    "risk": "low",
                    "risk_detail": None,
                    "blast_radius": "checkout-api in development, single replica",
                },
            },
            "evidence": [
                {
                    "id": "ev_health",
                    "source_type": "metric",
                    "source_type_detail": None,
                    "source_ref": "check_rollout_health",
                    "excerpt": '"status": "degraded"',
                    "observed_at": "2026-09-09T10:00:00Z",
                    "uri": None,
                    "tool_call_id": "tc_2",
                }
            ],
            "gaps": [
                {
                    "id": "gap_baseline",
                    "description": "compared_to_baseline was null; the previous version left "
                    "the window 20 seconds earlier.",
                    "kind": "missing_data",
                    "kind_detail": None,
                    "blocks_field": "findings.regression_suspected.value",
                    "suggested_agent": "deployment",
                    "suggested_query": "Re-check checkout-api health with a 120 minute lookback.",
                    "resolvable": True,
                }
            ],
            "overall_confidence": 0.68,
            "tool_calls": [],
            "generated_at": "2026-09-09T10:01:00Z",
        }
    )


# --------------------------------------------------------------------------- shape


def test_digest_is_a_wellformed_block_with_the_response_header():
    text = digest(_github_response("req_x", "inv_gh"))
    assert text.startswith(
        '<handoff from="github" invocation_id="inv_gh" status="complete" confidence="0.78">'
    )
    assert text.rstrip().endswith("</handoff>")
    assert "summary: PR #412 rewrote" in text


def test_github_digest_carries_the_facts_a_deployment_agent_acts_on():
    text = digest(_github_response("req_x", "inv_gh", commits=2))
    assert "repository: acme/shop" in text
    assert "ref: main" in text
    assert (
        '#412 "Tighten payments-api retry budget" merged merged_at=2026-08-07T16:40:00Z '
        "head_sha=9f3c2a1d4e5b6c7a8b9c0d1e2f3a4b5c6d7e8f90 files=3 +41/-12" in text
    )
    assert "touched_paths: services/payments/retry.py, deploy/payments-api.env" in text
    assert "risk: medium @0.70 [ev_pr]" in text
    assert "commits (2):" in text and "0000001 commit 1 at=2026-08-07T15:00:00Z pr=#412" in text
    assert "#412 (config): A shorter timeout would surface as 502s under load. @0.55" in text
    assert "diff_summary: Retry budget and timeout tightened" in text
    assert "gap_base [resolvable]: The base ref the PR was cut from was not fetched." in text
    assert "ev_pr pull_request #412" in text


def test_incident_digest_from_the_worked_example():
    incident = _example_responses()["incident"]
    assert isinstance(incident, IncidentAgentResponse)
    text = digest(incident)
    f = incident.findings
    assert f'<handoff from="incident" invocation_id="{incident.invocation_id}"' in text
    assert f"failure_mode: {f.failure_mode.value.value} @{f.failure_mode.confidence:.2f}" in text
    assert f"severity: {f.severity.value.value} @" in text
    assert f"affected_services: {', '.join(f.affected_services)}" in text
    if f.timeline:
        assert f"timeline ({len(f.timeline)} events):" in text
    if f.recommended_actions:
        assert f.recommended_actions[0].id in text


def test_docs_digest_from_the_worked_example():
    docs = _example_responses()["docs"]
    assert isinstance(docs, DocsAgentResponse)
    text = digest(docs)
    f = docs.findings
    supported = [c for c in f.claims if c.supported]
    assert f"supported_claims ({len(supported)}):" in text
    assert supported[0].id in text
    assert "coverage: searched=" in text
    if f.coverage.unanswered:
        assert "unanswered:" in text


def test_deployment_digest_reports_nulls_and_measured_signals_honestly():
    text = digest(_deployment_response())
    assert "releases_compared: 7e5c94f -> 1522000" in text
    assert "rollout_status: degraded @0.90 [ev_health]" in text
    assert "changed_config_keys (2): DEMO_GIT_SHA, SERVICE_NAME" in text
    assert "checkout-api: none -> aioc-demo-service:day3" in text
    assert "restart_count=1" in text and "p99_latency_ms=19.1" in text
    # A null judgement stays null, with its gap right there - never a filled-in guess.
    assert "regression_suspected: null @0.20" in text
    assert "gap_baseline [resolvable] blocks=findings.regression_suspected.value" in text
    assert 'suggest=deployment: "Re-check checkout-api health' in text
    assert "approval: required, risk=low blast_radius=checkout-api in development" in text


def test_every_agent_type_digests_without_error():
    responses = [
        _github_response("req_x", "inv_gh"),
        _deployment_response(),
        *_example_responses().values(),
    ]
    assert {r.agent.value for r in responses} == {"github", "deployment", "incident", "docs"}
    for r in responses:
        text = digest(r)
        assert text.startswith(f'<handoff from="{r.agent.value}"')
        assert text.rstrip().endswith("</handoff>")
        assert len(text) <= MAX_DIGEST_CHARS + len("\n</handoff>")


# --------------------------------------------------------------------------- bounds


def test_lists_are_capped_with_an_honest_remainder():
    text = digest(_github_response("req_x", "inv_gh", commits=MAX_ITEMS + 7))
    assert f"commits ({MAX_ITEMS + 7}):" in text
    assert "(+7 more)" in text
    assert f"{MAX_ITEMS + 1:07x} commit" not in text


def test_a_digest_over_the_ceiling_is_cut_on_a_line_and_says_so():
    # Per-value clipping alone cannot reach the ceiling; ten pull requests with long titles
    # and full path lists can. The cut lands on a line boundary, is announced, and the
    # block still closes.
    gh = _github_response("req_x", "inv_gh")
    pr = gh.findings.pull_requests[0]
    many = [
        pr.model_copy(
            update={
                "number": 400 + i,
                "title": f"PR {i}: " + "a long title about the payments retry budget " * 3,
                "touched_paths": [f"services/payments/module_{j}.py" for j in range(10)],
            }
        )
        for i in range(MAX_ITEMS)
    ]
    findings = gh.findings.model_copy(update={"pull_requests": many})
    text = digest(gh.model_copy(update={"findings": findings}))
    assert len(text) <= MAX_DIGEST_CHARS + len("\n</handoff>")
    marker = TRUNCATION_MARKER.format(limit=MAX_DIGEST_CHARS)
    assert marker in text
    body = text[: text.index(marker)]
    assert body.endswith("\n")  # cut on a whole line, never mid-fact
    assert text.rstrip().endswith("</handoff>")


def test_long_values_are_clipped_not_dumped():
    gh = _github_response("req_x", "inv_gh")
    long_summary = "word " * 200
    text = digest(gh.model_copy(update={"summary": long_summary}))
    (line,) = [ln for ln in text.splitlines() if ln.startswith("summary:")]
    assert line.endswith("...") and len(line) < 320


def test_body_bodies_of_commit_messages_are_not_digested():
    text = digest(_github_response("req_x", "inv_gh"))
    assert "longer body that must not be digested" not in text


# ------------------------------------------------------------------- composition


def _invocation(invocation_id: str) -> AgentInvocation:
    return AgentInvocation(
        invocation_id=invocation_id,
        agent="github",  # type: ignore[arg-type]
        reason="reads the PR",
        mode="parallel",  # type: ignore[arg-type]
        depends_on=[],
        context_passed="Repository acme/shop; PR 412; window 14:00-14:15 UTC.",
        round=0,
    )


def test_compose_appends_after_the_planner_block_and_never_replaces_it():
    planner = "Diff the release containing the PR github reports.\n"
    composed = compose_dependent_context(
        planner, [(_invocation("inv_gh"), _github_response("req_x", "inv_gh"))]
    )
    assert composed.startswith("Diff the release containing the PR github reports.")
    assert composed.index(HANDOFF_HEADER) > 0
    assert composed.count("<handoff ") == 1
    assert not composed.endswith("\n")


def test_compose_with_two_dependencies_keeps_depends_on_order():
    gh = _github_response("req_x", "inv_gh")
    dep = _deployment_response()
    composed = compose_dependent_context(
        "planner block", [(_invocation("inv_gh"), gh), (_invocation("inv_dep"), dep)]
    )
    assert composed.index('from="github"') < composed.index('from="deployment"')
    assert composed.count(HANDOFF_HEADER) == 1


def test_compose_with_nothing_to_hand_off_returns_the_planner_block_unchanged():
    assert compose_dependent_context("planner block", []) == "planner block"


# ---------------------------------------------------------------- the roster (Day 16)


def _planned(agent: str, reason: str) -> AgentInvocation:
    return _invocation(f"inv_{agent}").model_copy(
        update={"agent": AgentName(agent), "reason": reason}
    )


def test_roster_lists_every_sibling_with_its_reason_then_the_rule():
    planned = [
        _planned("incident", "diagnose the live spike"),
        _planned("docs", "find precedent in past incidents"),
        _planned("github", "read PR 11"),
    ]
    block = roster_block(AgentName.DOCS, planned)
    assert block is not None
    lines = block.splitlines()
    assert lines[0] == ROSTER_HEADER
    assert lines[1:3] == ["- incident: diagnose the live spike", "- github: read PR 11"]
    assert lines[-1] == ROSTER_RULE
    assert "- docs:" not in block


def test_roster_is_none_for_an_agent_alone_on_the_request():
    assert roster_block(AgentName.INCIDENT, [_planned("incident", "diagnose")]) is None
    assert with_roster("planner block", None) == "planner block"


def test_roster_clips_a_long_reason():
    block = roster_block(
        AgentName.INCIDENT, [_planned("incident", "x"), _planned("docs", "y" * (MAX_TEXT * 3))]
    )
    assert block is not None
    (docs_line,) = [ln for ln in block.splitlines() if ln.startswith("- docs:")]
    assert len(docs_line) < MAX_TEXT + 20


def test_with_roster_appends_after_the_planner_block():
    composed = with_roster("planner block\n", "ROSTER")
    assert composed == "planner block\n\nROSTER"


def test_digest_lines_are_quotable_verbatim_by_the_deployment_agent():
    """The tool-driven agents ground excerpts against their context block (a substring
    check). Every digest line must therefore be a plain, single-line fact - no JSON
    escaping that would make a quoted line differ from what the model saw."""
    from aioc.agents._toolset import ToolLedger

    text = digest(_github_response("req_x", "inv_gh"))
    ledger = ToolLedger(
        [],
        "aioc-deployment",
        context=compose_dependent_context(
            "plan",
            [
                (_invocation("inv_gh"), _github_response("req_x", "inv_gh")),
            ],
        ),
    )
    for line in text.splitlines():
        assert "\\n" not in line
        assert ledger.in_context(line.strip())
    assert isinstance(_github_response("req_x", "inv_gh"), GitHubAgentResponse)


# -- Day 15: what the first four-agent run found ----------------------------------------------


def _wide_docs_response() -> DocsAgentResponse:
    """The shape that broke the first live four-agent synthesis: ten long supported claims
    and eleven evidence refs, which is past the ceiling."""
    docs = _example_responses()["docs"]
    assert isinstance(docs, DocsAgentResponse)
    claim = next(c for c in docs.findings.claims if c.supported)
    claims = [
        claim.model_copy(update={"id": f"claim_{i:02d}", "statement": f"claim {i} " + "x" * 400})
        for i in range(MAX_ITEMS)
    ]
    evidence = [
        docs.evidence[0].model_copy(update={"id": f"ev_doc{i:04d}"}) for i in range(MAX_ITEMS + 1)
    ]
    findings = docs.findings.model_copy(update={"claims": claims})
    return docs.model_copy(update={"findings": findings, "evidence": evidence})


def test_the_evidence_list_survives_the_ceiling(monkeypatch):
    # The ids a reader may cite are the last thing in a digest, so they used to be the first
    # thing the ceiling cut - and a synthesiser that cannot see the evidence ids cites
    # whatever ids it can see. The body is cut instead; the ceiling still holds. (A lower
    # ceiling here, so the fixture does not have to be as large as the live report was.)
    ceiling = 2000
    monkeypatch.setattr(handoff_module, "MAX_DIGEST_CHARS", ceiling)
    docs = _wide_docs_response()
    text = digest(docs)
    marker = TRUNCATION_MARKER.format(limit=ceiling)
    assert marker in text
    assert len(text) <= ceiling + len("\n</handoff>")
    assert f"evidence ({len(docs.evidence)}):" in text
    for ref in docs.evidence[:MAX_ITEMS]:
        assert f"  - {ref.id} " in text
    assert text.index(marker) < text.index("evidence (")  # the cut is in the body
    assert text.rstrip().endswith("</handoff>")


def test_docs_claims_never_put_a_document_id_where_evidence_ids_go():
    docs = _example_responses()["docs"]
    assert isinstance(docs, DocsAgentResponse)
    claim_lines = [ln for ln in digest(docs).splitlines() if ln.startswith("  - claim_")]
    supported = [c for c in docs.findings.claims if c.supported]
    assert claim_lines
    for claim in supported:
        (line,) = [ln for ln in claim_lines if ln.startswith(f"  - {claim.id} ")]
        assert f"docs={claim.sources[0].document_id}" in line
        assert "[" not in line.split(":", 1)[0]  # brackets mean evidence ids, everywhere
