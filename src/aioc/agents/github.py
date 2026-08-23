"""GitHub agent (Day 11): reads repositories, analyses pull requests, explains diffs.

Follows `docs.py`'s structured pattern - a forced structured-output tool whose JSON Schema
is generated from the frozen models and annotated with the contract's cross-field rules -
with one structural difference: **the model fetches its own data through tools first**.
The Docs agent retrieves in-process before the model call; here the model drives
`get_pull_request` / `list_commits` / `diff_refs` over the real MCP wire
(`aioc.llm.mcp.McpStdioToolset` -> the `aioc-github` stdio server) in an ordinary
`run_tool_loop`, and only then is forced through `emit_github_report`. This is the first
agent that consumes an AIOC MCP tool end to end, which is what makes the contract's tool
envelope (sec 6) and error taxonomy real for the reasoning layer rather than for tests.

Enforced in code, not just prompted:

- **Facts are stamped, not asked for.** `PullRequestAnalysis` is mostly facts (title,
  state, head SHA, file and line counts) the tool already returned; asking the model to
  echo them is a field it can only get wrong (war story #7, the planner's `round`). The
  model reports a PR number plus its two judgements (`risk`, `summary`) and the runtime
  fills the facts from the ledger of tool outputs. `CommitRef` is all facts, so the model
  reports SHAs only.
- **Constrained to fetched data.** Every PR number, commit SHA, and `SuspectChange.change_ref`
  must resolve to something a tool call actually returned; anything else raises
  `GitHubAgentError`. A change the agent never looked at is a hallucination by definition.
- **Excerpts are verbatim.** A commit/pull-request evidence `excerpt` must appear in the
  tool output (whitespace-normalised); a paraphrase raises.
- **Tool calls are recorded honestly.** Every wire call becomes a contract `ToolCallRef`
  with the envelope's `ok`, `error_class`, `meta.token_estimate`, and `meta.truncated`, and
  each evidence entry gets the id of the call that returned what it quotes.
- **A report with no data is not `complete`.** If every tool call failed, `status` must be
  weaker than `complete` - the model may still write a report (and should, with gaps), but
  it may not claim it saw the repository.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from anthropic.types import ToolUseBlock
from pydantic import Field

from aioc.contracts import (
    Assessment,
    CommitRef,
    ErrorClass,
    Evidence,
    Gap,
    GitHubAgentResponse,
    GitHubFindings,
    PullRequestAnalysis,
    PullRequestState,
    ResponseStatus,
    RiskLevel,
    SourceType,
    StrictModel,
    SuspectChange,
    ToolCallRef,
)
from aioc.llm import LLMClient, ToolCallRecord, ToolResult, ToolSpec, Usage
from aioc.llm.mcp import McpStdioToolset
from aioc.tools.github.api import GitHubSettings

from ._annotate import ROOT, apply_guidance
from .incident import _CONFIDENCE_BANDS

AGENT_NAME = "github"

EMIT_TOOL_NAME = "emit_github_report"

GITHUB_SERVER_MODULE = "aioc.tools.github.server"

DEFAULT_MAX_TOOL_ROUNDS = 8

_GROUND_RULES = f"""\
You are the GitHub agent of AIOC, an AI operations center. You are an expert software
engineer doing change analysis for a site-reliability organisation: you read repositories,
analyse pull requests, explain what a diff changes, and link code changes to observed
symptoms.

Ground rules:

1. Read the repository through the tools provided - `get_pull_request`, `list_commits`,
   `diff_refs`. You inherit nothing and you know nothing about this repository beyond what
   those tools return in this conversation. Never invent a PR number, a SHA, a file, or a
   commit message.
2. Every tool reply is a JSON envelope: `ok: true` with `data` and `meta`, or `ok: false`
   with a structured `error`. Read `error.class`: retry only `transient` (after the stated
   `retry_after_ms`); on `validation` change the request; on `business` or `permission` do
   not retry - record what you could not read as a gap. `meta.truncated: true` means you saw
   part of the answer, and your findings must say so.
3. Distinguish facts from judgements. Facts (titles, SHAs, counts, paths, timestamps) are
   what the tools returned. Judgements (risk, what a change means, how it explains a
   symptom) are yours, and every judgement carries a confidence from 0.0 to 1.0 calibrated
   against these bands:

{_CONFIDENCE_BANDS}

   Below 0.25 means you must not state the conclusion at all - record it as a gap instead.
4. Cite evidence: a commit message, a PR title or body line, or a patch line, quoted
   VERBATIM from a tool reply. A judgement with confidence 0.5 or above must cite evidence.
5. Never repeat configuration VALUES even if a patch shows them. Refer to configuration by
   key name only. The tools redact values before you see them; do not try to reconstruct
   them.
6. Be economical with tools: fetch the PR or window the query names, read the patch only
   when the question is about what the code does, and stop when you have what the report
   needs."""


# ------------------------------------------------------------------- structured output


class ReportedPullRequest(StrictModel):
    """The model's half of `PullRequestAnalysis`: which PR, and the two judgements. The
    factual fields are stamped by the runtime from the tool output for that number."""

    number: int
    risk: Assessment[RiskLevel]
    summary: Assessment[str]


class ReportedFindings(StrictModel):
    """`GitHubFindings` with the facts removed: PRs by number + judgement, commits by SHA."""

    ref: str | None = None
    pull_requests: list[ReportedPullRequest] = Field(default_factory=list)
    commit_shas: list[str] = Field(default_factory=list)
    suspect_changes: list[SuspectChange] = Field(default_factory=list)
    diff_summary: Assessment[str]


class GitHubReport(StrictModel):
    """The payload the model owns - the `GitHubAgentResponse` envelope minus the plumbing the
    caller fills in (ids, timestamps, `tool_calls`, `repository`, and every factual field)."""

    status: ResponseStatus
    status_detail: str | None = None
    summary: str
    findings: ReportedFindings
    evidence: list[Evidence] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)


_TOP_LEVEL_DESCRIPTION = """\
The complete change-analysis report. Every property below is a top-level argument of this
tool - pass them directly; do not nest them inside a wrapper object.

Rules validated after you answer - a violation rejects the whole report:
1. Every pull request `number`, every entry in `commit_shas`, and every `change_ref` must be
   something a tool call in this conversation actually returned. Never invent one.
2. Every evidence `excerpt` from a commit or pull request must appear verbatim in a tool reply.
3. `status` may be `complete` only if at least one tool call succeeded.
4. Any `*_detail` field must be null unless its partner field is exactly `other`.
5. Configuration values never appear anywhere in the report - keys only."""

_FIELD_GUIDANCE: dict[str, dict[str, str]] = {
    ROOT: {
        "status": (
            "`complete` when the question is answered from fetched data. `partial` when "
            "some of what was asked could not be read (a failed or truncated tool call, a "
            "missing diff). `insufficient_evidence` when the tools established almost "
            "nothing. `error` only when nothing could be read at all."
        ),
        "status_detail": "Null unless `status` is `other`. Do not describe `partial` here.",
        "summary": "One or two sentences: what the repository shows about the question.",
        "evidence": (
            "One entry per distinct fact you rely on, quoted verbatim from a tool reply: a "
            "commit message, a PR title or body line, a patch line. `source_type` is "
            "`commit` or `pull_request`; `source_ref` is the SHA or `#<number>`. Every "
            "evidence id cited by an assessment must appear here."
        ),
        "gaps": (
            "What could not be established: a tool error (kind `tool_error`, or "
            "`insufficient_permission` for a permission error), a truncated answer, a PR "
            "the query named that does not exist. Set `blocks_field` to the findings path "
            "that is null or incomplete because of it."
        ),
        "overall_confidence": "Your confidence in the report as a whole, on the band table.",
    },
    "ReportedFindings": {
        "ref": "The branch, tag, or SHA you examined, or null when the question named a PR.",
        "pull_requests": (
            "One entry per pull request you fetched AND that matters to the question, "
            "with your risk and summary judgements. Facts (title, state, SHA, counts) are "
            "filled in from the tool reply - do not repeat them here."
        ),
        "commit_shas": (
            "The SHAs (full, as returned) of the commits relevant to the question, from "
            "`list_commits`, `diff_refs`, or a PR's commit list. Their facts are filled in "
            "from the tool reply."
        ),
        "suspect_changes": (
            "Changes that could explain an observed symptom, each linked to the symptom "
            "with a confidence-scored `symptom_link`. Empty when the question asks for no "
            "such link, or when nothing fetched plausibly explains it - say which in "
            "`diff_summary`."
        ),
        "diff_summary": (
            "What the examined change(s) do, in plain language, as a judgement with "
            "confidence and evidence. Null value (with a gap) if nothing could be read."
        ),
    },
    "ReportedPullRequest": {
        "number": "The PR number exactly as a `get_pull_request` reply returned it.",
        "risk": (
            "How risky the change is to run in production: `low` (docs, tests, isolated "
            "code), `medium` (behaviour change in a service path), `high` (config, schema, "
            "dependency, infrastructure, or a wide blast radius). `other` with `detail` "
            "only when none fits."
        ),
        "summary": "What the PR does and why it matters to the question, as a judgement.",
    },
    "SuspectChange": {
        "change_ref": "A fetched commit SHA or `#<number>` of a fetched PR. Never invent one.",
        "change_type": (
            "`code`, `dependency`, `config`, `schema`, `infrastructure`, or `other` with "
            "`change_type_detail`."
        ),
        "change_type_detail": "Null unless `change_type` is exactly `other`.",
        "symptom_link": (
            "How this change explains the symptom in the query, with confidence on the "
            "band table and evidence ids. Below 0.25: leave the change out and add a gap."
        ),
    },
    "Assessment_RiskLevel_": {
        "value": "The risk level, or null below 0.25 confidence (then add a gap).",
        "confidence": "Calibrated to the band table in the system prompt.",
        "evidence": "Ids of the evidence entries supporting this, all present in `evidence`.",
        "reasoning": "One line: which fetched facts lead to this level.",
        "detail": "Non-null exactly when `value` is `other`; null otherwise.",
    },
    "Assessment_str_": {
        "value": "Free text, or null below 0.25 confidence (then add a gap).",
        "confidence": "Calibrated to the band table in the system prompt.",
        "evidence": "Ids of the evidence entries supporting this, all present in `evidence`.",
        "reasoning": "One line: how the fetched facts lead to this.",
        "detail": "Always null here. This value is free text, so it has no `other` member.",
    },
    "Evidence": {
        "id": "Opaque id starting `ev_`, referenced by assessments.",
        "source_type": "`commit` for a commit message or patch line; `pull_request` for a PR.",
        "source_type_detail": "Null unless `source_type` is exactly `other`.",
        "source_ref": "The commit SHA, or `#<number>` for a pull request.",
        "excerpt": "Quoted verbatim from the tool reply. Never paraphrase or invent.",
        "uri": "The html_url the tool returned, when it did.",
        "tool_call_id": "Leave null - the runtime records the real tool call id.",
    },
    "Gap": {
        "kind_detail": "Null unless `kind` is exactly `other`.",
        "blocks_field": (
            "The findings path this gap leaves null or incomplete, e.g. "
            "`findings.diff_summary.value` or `findings.pull_requests`."
        ),
        "resolvable": (
            "True only if another agent, a wider window, or a different ref could close "
            "this gap. False stops the coordinator's refinement loop, so set it honestly - "
            "a permission error is not resolvable by retrying."
        ),
        "suggested_query": "The question to ask next, when `resolvable` is true.",
    },
}


def _apply_guidance(schema: dict[str, Any]) -> dict[str, Any]:
    return apply_guidance(
        schema,
        name="github emit",
        description=_TOP_LEVEL_DESCRIPTION,
        guidance=_FIELD_GUIDANCE,
    )


# Generated once at import from the frozen models - never hand-written, so it cannot drift.
_EMIT_SCHEMA: dict[str, Any] = _apply_guidance(GitHubReport.model_json_schema())

GITHUB_SYSTEM_PROMPT = f"""\
{_GROUND_RULES}

Work in two phases. First, investigate with the repository tools until you can answer the
question or have established that you cannot. Then, when asked, report your findings by
calling `{EMIT_TOOL_NAME}` exactly once, with no prose outside the tool call:

- List the pull requests that matter under `pull_requests` by number, with your `risk` and
  `summary` judgements; list relevant commits under `commit_shas`. Facts are filled in from
  the tool replies - do not repeat them.
- If the question links code to a symptom, list `suspect_changes` - each a fetched SHA or
  `#<number>` with a `change_type` and a confidence-scored `symptom_link`.
- Write `diff_summary` as a judgement with confidence and evidence ids. If nothing could be
  read, set its `value` to null and record a gap naming `findings.diff_summary.value`.
- Every evidence entry quotes a tool reply VERBATIM. Leave `tool_call_id` null; the runtime
  fills it.
- Set `status` to `complete` only when the question is answered from fetched data;
  `partial` when a tool call failed or was truncated; `insufficient_evidence` when the
  tools established almost nothing.
- For any enum, when no member fits, use `other` and put the specifics in that field's
  `detail`; leave `detail` null otherwise.

Set `overall_confidence` to your confidence in the report as a whole."""

_EMIT_INSTRUCTION = (
    f"Investigation complete. Now call `{EMIT_TOOL_NAME}` exactly once with the full report, "
    "built only from the tool replies above."
)


class GitHubAgentError(RuntimeError):
    """The model did not return usable structured output, or its output referenced data it
    was never given. A malformed-but-present payload raises pydantic's ``ValidationError``
    instead."""


def _emit_never_runs(_args: dict[str, Any]) -> ToolResult:
    raise GitHubAgentError(f"{EMIT_TOOL_NAME} is a structured-output tool; it is never executed")


_EMIT_TOOL = ToolSpec(
    name=EMIT_TOOL_NAME,
    description=(
        "Emit the change-analysis report as structured data. Call this exactly once, only "
        "when asked; it is the only way to answer. Pull requests by number with judgements, "
        "commits by SHA, suspect changes linked to the symptom, verbatim evidence."
    ),
    input_schema=_EMIT_SCHEMA,
    handler=_emit_never_runs,
)


# ------------------------------------------------------------------------- the toolset


class Toolset(Protocol):
    """What the agent needs from an open MCP toolset. `McpStdioToolset` satisfies it and
    tests inject in-process fakes through the same seam."""

    @property
    def server_name(self) -> str: ...

    @property
    def tools(self) -> list[ToolSpec]: ...


ToolsetFactory = Callable[[], AbstractContextManager[Toolset]]


def default_toolset() -> AbstractContextManager[Toolset]:
    """The real thing: the `aioc-github` stdio server, launched in this interpreter."""
    return McpStdioToolset.for_module(GITHUB_SERVER_MODULE)


# ---------------------------------------------------------------------------- the ledger


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:8]}"


def _normalise(text: str) -> str:
    return " ".join(text.split())


_PR_REF = re.compile(r"^#?(\d+)$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class _Ledger:
    """Everything the tools returned, indexed for stamping and grounding.

    Built from the loop's `ToolCallRecord`s after the fact - the envelope text is the
    record's ``output``, so no handler wrapping is needed and a fake toolset in tests goes
    through exactly the same path as the wire."""

    def __init__(self, records: list[ToolCallRecord], server: str) -> None:
        self.refs: list[ToolCallRef] = []
        self.ref_for_record: dict[str, str] = {}  # record.id -> ToolCallRef.id
        self.prs: dict[int, tuple[dict[str, Any], str]] = {}  # number -> (facts, tc id)
        self.commits: dict[str, tuple[dict[str, Any], str]] = {}  # sha -> (facts, tc id)
        self.outputs: list[tuple[str, str]] = []  # (tc id, normalised text)
        self.repository: str | None = None
        self.any_ok = False
        for record in records:
            envelope = _parse_envelope(record.output)
            tc_id = _new_id("tc")
            ok = record.ok and bool(envelope and envelope.get("ok"))
            error_class: ErrorClass | None = None
            meta: dict[str, Any] = {}
            if ok:
                self.any_ok = True
                meta = dict((envelope or {}).get("meta") or {})
                self._index(dict((envelope or {}).get("data") or {}), tc_id)
            else:
                error_class = _error_class_of(envelope)
            self.refs.append(
                ToolCallRef(
                    id=tc_id,
                    tool_name=record.name,
                    server=server,
                    started_at=record.started_at,
                    duration_ms=record.duration_ms,
                    ok=ok,
                    error_class=error_class,
                    tokens_returned=meta.get("token_estimate") if ok else None,
                    truncated=bool(meta.get("truncated")) if ok else False,
                )
            )
            self.ref_for_record[record.id] = tc_id
            self.outputs.append((tc_id, _normalise(record.output)))

    def _index(self, data: dict[str, Any], tc_id: str) -> None:
        if isinstance(data.get("repository"), str):
            self.repository = data["repository"]
        pr = data.get("pull_request")
        if isinstance(pr, dict) and isinstance(pr.get("number"), int):
            self.prs[pr["number"]] = (pr, tc_id)
        for commit in data.get("commits") or []:
            if isinstance(commit, dict) and isinstance(commit.get("sha"), str):
                self.commits[commit["sha"]] = (commit, tc_id)

    def pull_request(self, number: int) -> tuple[dict[str, Any], str] | None:
        return self.prs.get(number)

    def commit(self, ref: str) -> tuple[dict[str, Any], str] | None:
        """Exact SHA, or a unique prefix of at least seven characters."""
        ref = ref.lower()
        if ref in self.commits:
            return self.commits[ref]
        if not _SHA.match(ref):
            return None
        matches = [sha for sha in self.commits if sha.startswith(ref)]
        return self.commits[matches[0]] if len(matches) == 1 else None

    def resolve(self, ref: str) -> str | None:
        """The tool call id that returned ``ref`` (a SHA or `#N`), or None if never fetched."""
        ref = ref.strip()
        match = _PR_REF.match(ref)
        if match and int(match.group(1)) in self.prs:
            return self.prs[int(match.group(1))][1]
        found = self.commit(ref)
        return found[1] if found else None

    def quoted_in(self, excerpt: str) -> str | None:
        """The tool call id whose output contains ``excerpt`` verbatim, or None."""
        needle = _normalise(excerpt)
        if not needle:
            return None
        for tc_id, text in self.outputs:
            if needle in text:
                return tc_id
        return None


def _parse_envelope(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _error_class_of(envelope: dict[str, Any] | None) -> ErrorClass:
    """The envelope's class when it has one; a transport failure (the server died, the call
    timed out, the text was not JSON) is transient by the taxonomy's own definition."""
    error = (envelope or {}).get("error")
    if isinstance(error, dict):
        try:
            return ErrorClass(str(error.get("class")))
        except ValueError:
            pass
    return ErrorClass.TRANSIENT


# ------------------------------------------------------------------------------ the agent


class GitHubAgent:
    """GitHub agent: tool-driven, schema-validated change analysis (Day 11)."""

    name = AGENT_NAME

    def __init__(
        self,
        client: LLMClient | None = None,
        *,
        toolset: ToolsetFactory | None = None,
        repository: str | None = None,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    ) -> None:
        self._client = client or LLMClient()
        self._toolset = toolset or default_toolset
        self._repository = repository
        self._max_rounds = max_tool_rounds

    def analyze(
        self,
        query: str,
        *,
        context: str,
        request_id: str | None = None,
        invocation_id: str | None = None,
        usage: Usage | None = None,
    ) -> GitHubAgentResponse:
        """Answer one repository question as a schema-validated `GitHubAgentResponse`.

        ``context`` is the coordinator's explicit context block (``context_passed``,
        CONTRACTS.md sec 5) and must be non-empty - the same rule as every agent. The model
        investigates with the toolset's tools, is then forced through ``emit_github_report``,
        and the assembled response is validated against the contract envelope plus this
        module's grounding checks.

        Raises `GitHubAgentError` for missing/ungrounded output, `McpToolsetError` if the
        server cannot be started, and pydantic's ``ValidationError`` for a payload that
        violates the contract. The validation-retry loop is Day 17, deliberately not here.
        """
        if not query.strip():
            raise ValueError("query must be non-empty")
        if not context.strip():
            raise ValueError(
                "context must be non-empty - the GitHub agent inherits nothing; "
                "pass everything it needs explicitly (CONTRACTS.md sec 5, context_passed)"
            )

        prompt = self._prompt(query.strip(), context.strip())
        with self._toolset() as toolset:
            server = toolset.server_name
            data_tools = list(toolset.tools)
            loop = self._client.run_tool_loop(
                messages=[{"role": "user", "content": prompt}],
                tools=data_tools,
                system=GITHUB_SYSTEM_PROMPT,
                max_iterations=self._max_rounds,
            )
        if usage is not None:
            usage.add(loop.usage)

        # Phase 2: the same conversation, now forced through the emit tool. The data tools
        # stay defined so the prior tool_use blocks in the history remain well-formed.
        resp = self._client.complete(
            messages=[*loop.messages, {"role": "user", "content": _EMIT_INSTRUCTION}],
            system=GITHUB_SYSTEM_PROMPT,
            tools=[*data_tools, _EMIT_TOOL],
            tool_choice={"type": "tool", "name": EMIT_TOOL_NAME},
        )
        if usage is not None:
            usage.input_tokens += resp.usage.input_tokens
            usage.output_tokens += resp.usage.output_tokens
        if resp.stop_reason == "max_tokens":
            raise GitHubAgentError(
                f"{EMIT_TOOL_NAME} output was truncated at the max_tokens limit "
                f"({resp.usage.output_tokens} output tokens); the report is incomplete. "
                "Raise AIOC_MAX_TOKENS or narrow the query."
            )

        payload = _extract_tool_input(resp, EMIT_TOOL_NAME)
        report = GitHubReport.model_validate(payload)
        ledger = _Ledger(loop.tool_calls, server)
        return self._assemble(report, ledger, request_id, invocation_id)

    @staticmethod
    def _prompt(query: str, context: str) -> str:
        return f"<context>\n{context}\n</context>\n\nRepository query: {query}"

    def _assemble(
        self,
        report: GitHubReport,
        ledger: _Ledger,
        request_id: str | None,
        invocation_id: str | None,
    ) -> GitHubAgentResponse:
        if report.status is ResponseStatus.COMPLETE and not ledger.any_ok:
            raise GitHubAgentError(
                "status is `complete` but no tool call succeeded - a report that read "
                "nothing cannot claim to have answered from the repository"
            )

        pull_requests = [
            _stamp_pull_request(reported, ledger) for reported in report.findings.pull_requests
        ]
        commits = [_stamp_commit(sha, ledger) for sha in report.findings.commit_shas]
        for change in report.findings.suspect_changes:
            if ledger.resolve(change.change_ref) is None:
                raise GitHubAgentError(
                    f"suspect change {change.change_ref!r} was never fetched by a tool call - "
                    "a change the agent did not read cannot be a suspect"
                )
        evidence = [_ground_evidence(entry, ledger) for entry in report.evidence]

        repository = ledger.repository or self._repository or GitHubSettings().repository
        if repository is None:
            raise GitHubAgentError(
                "no repository is known - set GITHUB_REPO or pass repository= to the agent"
            )
        findings = GitHubFindings(
            repository=repository,
            ref=report.findings.ref,
            pull_requests=pull_requests,
            commits=commits,
            suspect_changes=report.findings.suspect_changes,
            diff_summary=report.findings.diff_summary,
        )
        return GitHubAgentResponse(
            request_id=request_id or _new_id("req"),
            invocation_id=invocation_id or _new_id("inv"),
            status=report.status,
            status_detail=report.status_detail,
            summary=report.summary,
            findings=findings,
            evidence=evidence,
            gaps=report.gaps,
            overall_confidence=report.overall_confidence,
            tool_calls=ledger.refs,
            generated_at=datetime.now(UTC),
        )


def _stamp_pull_request(reported: ReportedPullRequest, ledger: _Ledger) -> PullRequestAnalysis:
    found = ledger.pull_request(reported.number)
    if found is None:
        raise GitHubAgentError(
            f"pull request #{reported.number} was never fetched by a tool call - "
            "a PR the agent did not read cannot be analysed"
        )
    facts, _ = found
    state = str(facts.get("state") or "other")
    try:
        pr_state = PullRequestState(state)
    except ValueError:
        pr_state = PullRequestState.OTHER
    return PullRequestAnalysis(
        number=reported.number,
        title=str(facts.get("title") or ""),
        state=pr_state,
        state_detail=f"GitHub reported state {state!r}"
        if pr_state is PullRequestState.OTHER
        else None,
        merged_at=facts.get("merged_at"),
        head_sha=str(facts.get("head_sha") or ""),
        files_changed=int(facts.get("files_changed") or 0),
        additions=int(facts.get("additions") or 0),
        deletions=int(facts.get("deletions") or 0),
        touched_paths=[str(p) for p in facts.get("touched_paths") or []],
        risk=reported.risk,
        summary=reported.summary,
    )


def _stamp_commit(sha: str, ledger: _Ledger) -> CommitRef:
    found = ledger.commit(sha)
    if found is None:
        raise GitHubAgentError(
            f"commit {sha!r} was never fetched by a tool call - "
            "a commit the agent did not read cannot be reported"
        )
    facts, _ = found
    return CommitRef(
        sha=str(facts["sha"]),
        short_sha=str(facts.get("short_sha") or facts["sha"][:7]),
        message=str(facts.get("message") or ""),
        authored_at=_parse_timestamp(facts.get("authored_at"), sha),
        touched_paths=[str(p) for p in facts.get("touched_paths") or []],
        pull_request_number=facts.get("pull_request_number"),
    )


def _parse_timestamp(raw: Any, sha: str) -> datetime:
    """GitHub stamps every commit's author date; a missing or unparsable one is a tool
    fault, not a fact to fabricate."""
    if not isinstance(raw, str):
        raise GitHubAgentError(f"commit {sha!r} carries no authored_at timestamp")
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise GitHubAgentError(f"commit {sha!r} has an unparsable authored_at {raw!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


_GROUNDED_SOURCES = (SourceType.COMMIT, SourceType.PULL_REQUEST)


def _ground_evidence(entry: Evidence, ledger: _Ledger) -> Evidence:
    """Commit and pull-request evidence must quote a tool reply verbatim and name something
    that was fetched; the runtime then stamps the real tool call id. Other source types
    (the coordinator's context, say) pass through untouched."""
    if entry.source_type not in _GROUNDED_SOURCES:
        return entry
    if ledger.resolve(entry.source_ref) is None:
        raise GitHubAgentError(
            f"evidence {entry.id} cites {entry.source_ref!r}, which no tool call returned"
        )
    tc_id = ledger.quoted_in(entry.excerpt)
    if tc_id is None:
        raise GitHubAgentError(
            f"evidence {entry.id} excerpt does not appear verbatim in any tool reply - "
            "excerpts must never be paraphrased"
        )
    return entry.model_copy(update={"tool_call_id": tc_id})


def _extract_tool_input(resp: Any, tool_name: str) -> dict[str, Any]:
    """Pull the forced tool call's input object out of the response, or fail clearly."""
    for block in resp.content:
        if isinstance(block, ToolUseBlock) and block.name == tool_name:
            if not isinstance(block.input, dict):
                raise GitHubAgentError(f"{tool_name} input was not a JSON object")
            return dict(block.input)
    raise GitHubAgentError(
        f"model did not call {tool_name} (stop_reason={resp.stop_reason!r}); no structured output"
    )
