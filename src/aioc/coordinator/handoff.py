"""The sequential handoff: what a dependent invocation is told about its dependency (Day 13),
and what a re-delegated invocation is told about the gaps it is closing (Day 14).

The planner writes every invocation's ``context_passed`` before anything runs, so on its own
it cannot tell Deployment what GitHub found - it can only say "diff the release containing
the PR GitHub reports". This module is the other half: once a dependency has returned, the
executor composes the dependent's context from the planner's block **plus a structured
digest of each dependency's response**, and records the composed block verbatim in the
invocation's ``context_passed``. The explicit-passing rule survives unchanged: the runner
still receives exactly ``context_passed`` and nothing else; ``context_passed`` is simply
longer than it was when the plan was written.

Two decisions, each written down because each has a tempting alternative:

**A digest, not the raw response.** `BUILD_PLAN.md` Phase 4 says it directly - "pass
Incident's digest to Deployment, not the raw dump". A serialised `AgentResponse` is the
easy thing to append and the wrong one: it is thousands of tokens of ids, timestamps,
nulls, and confidence plumbing that the dependent must then re-parse, and every token of it
is re-sent on every round of the dependent's own tool loop (the Day 11 and Day 12 live runs
each measured that multiplication). The digest keeps the facts a downstream agent can act
on - PR numbers, SHAs, touched paths, the judgements with their confidence, the gaps, the
evidence references - and drops the envelope. It is bounded: lists are capped with an
honest ``(+N more)`` marker and the whole block has a hard ceiling, so a dependency that
returned a lot cannot blow the dependent's prompt.

**Appended to the planner's block, never replacing it.** The planner's block says what the
coordinator wanted from this agent; the digest says what the dependency found. Both are
needed, and the digest goes last so it sits closest to the query, where the model attends
most (Phase 4's position-aware ordering). A dependent's context is composed from its
*direct* dependencies' responses only - a two-hop chain sees the middle agent's digest,
not its composed context. That is what keeps a handoff a digest rather than a snowball.

The digest is plain text with a fixed shape rather than JSON, so the tool-driven agents
can quote a line of it verbatim as evidence (their grounding check accepts the context
block as a source; see `aioc.agents._toolset.ToolLedger.in_context`).

**The refinement loop reuses the same composition (Day 14).** A re-delegated invocation's
context is the planner's block for that agent (when the agent was in the plan), then a
`refinement_block` naming each gap it is asked to close - id, the field it blocks, the
agent that raised it, and the ``suggested_query`` verbatim - then the digest of every
response that raised one of those gaps, through `compose_dependent_context` exactly as a
sequential dependent gets its dependency's digest. The re-delegated agent therefore sees
what the earlier round found and what it could not establish, and nothing is inherited.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from aioc.contracts import (
    AgentInvocation,
    AgentResponse,
    Assessment,
    DeploymentFindings,
    DocsFindings,
    Gap,
    GitHubFindings,
    IncidentFindings,
    walk_assessments,
)

# Bounds. Every list in a digest is capped at MAX_ITEMS entries with a "(+N more)" marker,
# every free-text value at MAX_TEXT characters, and the whole block at MAX_DIGEST_CHARS.
# The ceiling is deliberately far below one tool reply's worth of tokens: a handoff should
# cost the dependent a few hundred tokens, not a few thousand.
MAX_ITEMS = 10
MAX_TEXT = 300
MAX_DIGEST_CHARS = 4000
TRUNCATION_MARKER = "... [digest truncated at {limit} chars]"

HANDOFF_HEADER = (
    "Results handed off from the invocation(s) this one depends on. They are the only view "
    "of that work you have; treat their facts as facts from those agents, cite them as "
    "evidence from your context, and do not re-derive what they already established."
)

REFINEMENT_HEADER = (
    "Refinement round {round}. The coordinator is re-delegating to you because an earlier "
    "round could not establish the following. Close exactly these gaps; the handed-off "
    "results below are what that round found and must not be re-derived."
)


def compose_dependent_context(
    planner_block: str,
    dependencies: Sequence[tuple[AgentInvocation, AgentResponse]],
) -> str:
    """The planner's block, then one digest per direct dependency, in ``depends_on`` order.

    Returns the planner's block unchanged when there is nothing to hand off, so a
    sequential invocation whose dependencies all failed (which the executor turns into a
    gap before reaching here) or a plan with an empty handoff never grows a stray header.
    """
    if not dependencies:
        return planner_block
    parts = [planner_block.rstrip(), "", HANDOFF_HEADER, ""]
    for _, response in dependencies:
        parts.append(digest(response))
        parts.append("")
    return "\n".join(parts).rstrip()


def refinement_block(
    round_number: int,
    gaps: Sequence[Gap],
    raised_by: Mapping[str, str],
) -> str:
    """What a re-delegated invocation is told about the gaps it is closing.

    ``raised_by`` maps a gap id to a one-line origin (agent and invocation) for the gaps
    that came from an agent response; a gap the executor itself raised (a failed
    invocation) or the planner raised has no entry and is shown as the coordinator's.
    The ``suggested_query`` is shown verbatim: the loop consumes it, it does not rewrite it.
    """
    lines = [REFINEMENT_HEADER.format(round=round_number)]
    for g in gaps:
        blocks = f" [blocks {g.blocks_field}]" if g.blocks_field else ""
        origin = raised_by.get(g.id, "the coordinator")
        lines.append(f"- {g.id}{blocks} (raised by {origin}): {_text(g.description)}")
        if g.suggested_query:
            lines.append(f'  asked: "{_text(g.suggested_query)}"')
    return "\n".join(lines)


def refinement_query(gaps: Sequence[Gap]) -> str:
    """The query a re-delegated invocation runs: one gap's ``suggested_query`` verbatim,
    or several listed verbatim when one agent is asked to close more than one gap."""
    queries: list[str] = []
    for g in gaps:
        if g.suggested_query and g.suggested_query not in queries:
            queries.append(g.suggested_query)
    if len(queries) == 1:
        return queries[0]
    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(queries, start=1))
    return f"Resolve each of the following:\n{numbered}"


def digest(response: AgentResponse) -> str:
    """A bounded, structured summary of one agent's response for another agent's context."""
    lines: list[str] = [
        f'<handoff from="{response.agent.value}" invocation_id="{response.invocation_id}" '
        f'status="{response.status.value}" confidence="{response.overall_confidence:.2f}">',
        f"summary: {_text(response.summary)}",
    ]
    findings = response.findings
    if isinstance(findings, GitHubFindings):
        lines.extend(_github(findings))
    elif isinstance(findings, IncidentFindings):
        lines.extend(_incident(findings))
    elif isinstance(findings, DocsFindings):
        lines.extend(_docs(findings))
    elif isinstance(findings, DeploymentFindings):
        lines.extend(_deployment(findings))
    else:  # pragma: no cover - the four findings types are the closed set today
        lines.extend(_generic(findings))
    lines.extend(_gaps(response))
    lines.extend(_evidence(response))
    # The body is bounded and the closing tag is appended afterwards, so a truncated digest
    # is still a well-formed block with a visible marker rather than one that stops mid-line.
    return _bounded("\n".join(lines)) + "\n</handoff>"


# ------------------------------------------------------------------------ per-agent bodies


def _github(f: GitHubFindings) -> list[str]:
    out = [f"repository: {f.repository}"]
    if f.ref:
        out.append(f"ref: {f.ref}")
    if f.pull_requests:
        out.append("pull_requests:")
        for pr in _capped(f.pull_requests, out):
            state = pr.state.value if pr.state_detail is None else f"other ({pr.state_detail})"
            merged = f" merged_at={_ts(pr.merged_at)}" if pr.merged_at else ""
            out.append(
                f'  - #{pr.number} "{_text(pr.title, 120)}" {state}{merged} '
                f"head_sha={pr.head_sha} files={pr.files_changed} "
                f"+{pr.additions}/-{pr.deletions}"
            )
            if pr.touched_paths:
                out.append(f"    touched_paths: {_joined(pr.touched_paths)}")
            out.append(f"    risk: {_assessment(pr.risk)}")
            out.append(f"    summary: {_assessment(pr.summary)}")
    if f.commits:
        out.append(f"commits ({len(f.commits)}):")
        for c in _capped(f.commits, out):
            first_line = c.message.splitlines()[0] if c.message else ""
            pr_ref = f" pr=#{c.pull_request_number}" if c.pull_request_number else ""
            out.append(
                f"  - {c.short_sha} {_text(first_line, 120)} at={_ts(c.authored_at)}{pr_ref}"
            )
            if c.touched_paths:
                out.append(f"    touched_paths: {_joined(c.touched_paths)}")
    if f.suspect_changes:
        out.append("suspect_changes:")
        for s in _capped(f.suspect_changes, out):
            kind = (
                s.change_type.value
                if s.change_type_detail is None
                else f"other ({s.change_type_detail})"
            )
            out.append(f"  - {s.change_ref} ({kind}): {_assessment(s.symptom_link)}")
    out.append(f"diff_summary: {_assessment(f.diff_summary)}")
    return out


def _incident(f: IncidentFindings) -> list[str]:
    end = _ts(f.incident_window.end) if f.incident_window.end else "ongoing"
    out = [
        f"incident_window: {_ts(f.incident_window.start)} -> {end}",
        f"affected_services: {_joined(f.affected_services)}",
        f"severity: {_assessment(f.severity)}",
        f"failure_mode: {_assessment(f.failure_mode)}",
        f"root_cause: {_assessment(f.root_cause)}",
    ]
    if f.contributing_factors:
        out.append("contributing_factors:")
        for a in _capped(f.contributing_factors, out):
            out.append(f"  - {_assessment(a)}")
    if f.timeline:
        out.append(f"timeline ({len(f.timeline)} events):")
        for e in _capped(f.timeline, out):
            kind = e.kind.value if e.kind_detail is None else f"other ({e.kind_detail})"
            sev = f" {e.severity.value}" if e.severity else ""
            out.append(f"  - {_ts(e.at)} {e.service} {kind}{sev}: {_text(e.description, 160)}")
    measured = {k: v for k, v in f.impact.model_dump().items() if v is not None}
    if measured:
        out.append("impact: " + ", ".join(f"{k}={v}" for k, v in measured.items()))
    if f.recommended_actions:
        out.append("recommended_actions:")
        for a in _capped(f.recommended_actions, out):
            approval = "requires approval" if a.requires_approval else "no approval needed"
            target = f" target={a.target_service}" if a.target_service else ""
            out.append(f"  - {a.id} [{a.risk.value}, {approval}]{target}: {_text(a.action, 160)}")
    if f.similar_incidents:
        out.append(f"similar_incidents: {_joined(f.similar_incidents)}")
    return out


def _docs(f: DocsFindings) -> list[str]:
    out = [f"answer: {_assessment(f.answer)}"]
    supported = [c for c in f.claims if c.supported]
    unsupported = [c for c in f.claims if not c.supported]
    if supported:
        out.append(f"supported_claims ({len(supported)}):")
        for c in _capped(supported, out):
            docs = _joined(sorted({s.document_id for s in c.sources}))
            out.append(f"  - {c.id} @{c.confidence:.2f} [{docs}]: {_text(c.statement, 200)}")
    if unsupported:
        out.append(f"unsupported_claims ({len(unsupported)}):")
        for c in _capped(unsupported, out):
            out.append(f"  - {c.id}: {_text(c.statement, 200)}")
    cov = f.coverage
    out.append(
        f"coverage: searched={cov.documents_searched} retrieved={cov.documents_retrieved} "
        f"cited={cov.documents_cited}"
    )
    if cov.unanswered:
        out.append(f"unanswered: {_joined(cov.unanswered)}")
    return out


def _deployment(f: DeploymentFindings) -> list[str]:
    env = f.environment.value if f.environment_detail is None else f"other ({f.environment_detail})"
    out = [
        f"service: {f.service}",
        f"environment: {env}",
        f"releases_compared: {f.releases_compared.from_version or 'null (first release)'} "
        f"-> {f.releases_compared.to_version}",
        f"rollout_status: {_assessment(f.rollout_status)}",
        f"changed_config_keys ({len(f.changed_config_keys)}): "
        f"{_joined(f.changed_config_keys) if f.changed_config_keys else '[]'}",
    ]
    if f.image_changes:
        out.append("image_changes:")
        for i in _capped(f.image_changes, out):
            out.append(f"  - {i.container}: {i.from_image or 'none'} -> {i.to_image}")
    measured = {k: v for k, v in f.health_signals.model_dump().items() if v is not None}
    out.append(
        "health_signals: " + (", ".join(f"{k}={v}" for k, v in measured.items()) or "none measured")
    )
    out.append(f"regression_suspected: {_assessment(f.regression_suspected)}")
    out.append(f"rollback_recommendation: {_assessment(f.rollback_recommendation)}")
    approval = f.approval
    risk = _enum_text(approval.risk.value, approval.risk_detail)
    blast = f" blast_radius={_text(approval.blast_radius, 160)}" if approval.blast_radius else ""
    out.append(f"approval: required, risk={risk}{blast}")
    return out


def _generic(findings: Any) -> list[str]:
    return [f"{path}: {_assessment(a)}" for path, a in walk_assessments(findings, "findings")]


# ------------------------------------------------------------------------- common sections


def _gaps(response: AgentResponse) -> list[str]:
    if not response.gaps:
        return []
    out = [f"gaps ({len(response.gaps)}):"]
    for g in _capped(response.gaps, out):
        flag = "resolvable" if g.resolvable else "unresolvable"
        blocks = f" blocks={g.blocks_field}" if g.blocks_field else ""
        hint = ""
        if g.suggested_agent is not None:
            hint = f' suggest={g.suggested_agent.value}: "{_text(g.suggested_query or "", 160)}"'
        out.append(f"  - {g.id} [{flag}]{blocks}: {_text(g.description, 200)}{hint}")
    return out


def _evidence(response: AgentResponse) -> list[str]:
    if not response.evidence:
        return []
    out = [f"evidence ({len(response.evidence)}):"]
    for e in _capped(response.evidence, out):
        kind = _enum_text(e.source_type.value, e.source_type_detail)
        out.append(f"  - {e.id} {kind} {_text(e.source_ref, 120)}")
    return out


# ---------------------------------------------------------------------------- formatting


def _assessment(a: Assessment[Any]) -> str:
    value: Any = a.value
    if value is None:
        shown = "null"
    elif hasattr(value, "value"):
        shown = str(value.value)
        if a.detail is not None:
            shown = f"{shown} ({_text(a.detail, 120)})"
    else:
        shown = _text(str(value))
    cited = f" [{_joined(a.evidence)}]" if a.evidence else ""
    return f"{shown} @{a.confidence:.2f}{cited}"


def _capped(items: Sequence[Any], out: list[str]) -> Iterable[Any]:
    """Yield at most MAX_ITEMS entries, then append the honest remainder marker to ``out``.

    The marker is appended after the caller's per-item lines because the caller writes
    them while iterating; it lands where the omitted entries would have started.
    """
    shown = list(items[:MAX_ITEMS])
    remainder = len(items) - len(shown)
    yield from shown
    if remainder > 0:
        out.append(f"  (+{remainder} more)")


def _joined(values: Sequence[str]) -> str:
    shown = list(values[:MAX_ITEMS])
    text = ", ".join(shown)
    remainder = len(values) - len(shown)
    return f"{text} (+{remainder} more)" if remainder > 0 else text


def _text(value: str, limit: int = MAX_TEXT) -> str:
    flat = " ".join(value.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _enum_text(member: str, detail: str | None) -> str:
    """An enum member, with its detail string when the member is ``other``."""
    return member if detail is None else f"other ({_text(detail, 120)})"


def _ts(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _bounded(text: str) -> str:
    """Cap the digest body, ending on a whole line and saying so - a cut that stops mid-line
    would leave a half-fact the dependent could quote as if it were whole."""
    if len(text) <= MAX_DIGEST_CHARS:
        return text
    marker = TRUNCATION_MARKER.format(limit=MAX_DIGEST_CHARS)
    budget = MAX_DIGEST_CHARS - len(marker) - 1
    kept = text[:budget]
    cut = kept.rfind("\n")
    if cut > 0:
        kept = kept[:cut]
    return kept.rstrip() + "\n" + marker
