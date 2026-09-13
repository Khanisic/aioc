"""Deployment agent (Day 12): compares releases and checks rollout health.

The Day 11 pattern, end to end: the model investigates through `diff_release` and
`check_rollout_health` over the real MCP wire (`aioc.llm.mcp.McpStdioToolset` -> the
`aioc-deployment` stdio server) in an ordinary `run_tool_loop`, and is then forced through
`emit_deployment_report`. What is different is what the runtime stamps and what it refuses.

Enforced in code, not just prompted:

- **Facts are stamped, not asked for.** `changed_config_keys`, `image_changes`, and
  `health_signals` are exactly what the two tools returned for the release the model
  reports on; the model never retypes a key, an image reference, or a number (war story
  #7). It reports which service and which releases, and its judgements: `rollout_status`,
  `regression_suspected`, `rollback_recommendation`, and the approval risk.
- **Constrained to fetched data.** The reported service must be one the tools were asked
  about, and the reported releases must match a `diff_release` reply or the version a
  `check_rollout_health` reply assessed; anything else raises `DeploymentAgentError`.
- **Not looked is not empty.** `changed_config_keys: []` means the diff was read and no key
  changed; a report that never ran `diff_release` may not say that, so it must carry a gap
  against `findings.changed_config_keys` or `findings.image_changes`. The same for
  `health_signals`: all-null without a gap against `findings.health_signals` is refused.
  This is the contract's null-versus-`[]` rule (sec 1) applied to the two lists the
  Deployment findings render most visibly.
- **Excerpts are verbatim.** Every evidence excerpt must appear in a tool reply or in the
  explicit context block - the agent has no other source. A quote from a reply gets the
  real tool call id stamped; a quote from the context keeps `tool_call_id` null.
- **Every recommendation is human-gated.** `approval.requires_approval` is const `true`
  (contract sec 4.4); the Day 16 HITL gate reads it. The runtime stamps it regardless of
  what the model wrote.
- **A report with no data is not `complete`.** If every tool call failed, `status` must be
  weaker than `complete`.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

from anthropic.types import ToolUseBlock
from pydantic import Field

from aioc.contracts import (
    ApprovalRequirement,
    Assessment,
    DeploymentAgentResponse,
    DeploymentFindings,
    Environment,
    Evidence,
    Gap,
    HealthSignals,
    ImageChange,
    ReleasesCompared,
    ResponseStatus,
    RollbackRecommendation,
    RolloutStatus,
    StrictModel,
)
from aioc.llm import LLMClient, ToolCallRecord, ToolResult, ToolSpec, Usage
from aioc.llm.mcp import McpStdioToolset

from ._annotate import ROOT, apply_guidance
from ._status import settle_status
from ._toolset import ToolLedger, Toolset, ToolsetFactory, new_id
from .incident import _CONFIDENCE_BANDS

__all__ = ["DeploymentAgent", "DeploymentAgentError", "DeploymentReport"]

AGENT_NAME = "deployment"

EMIT_TOOL_NAME = "emit_deployment_report"

DEPLOYMENT_SERVER_MODULE = "aioc.tools.deployment.server"

DEFAULT_MAX_TOOL_ROUNDS = 8

_GROUND_RULES = f"""\
You are the Deployment agent of AIOC, an AI operations center. You are an expert release
engineer for a site-reliability organisation: you compare releases, check how a rollout is
behaving, decide whether a regression is likely, and recommend what to do about it - always
as a recommendation for a human to approve, never as an action.

Ground rules:

1. Read deployments through the tools provided - `diff_release` (what changed between two
   releases: configuration keys, images, manifest paths, commits) and `check_rollout_health`
   (how a deployed version is behaving now: status, replicas, error rate, latency, restarts,
   and the delta against the previous version). You inherit nothing and you know nothing
   about these services beyond the context you were given and what those tools return in
   this conversation. Never invent a version, a key, an image, or a number.
2. Every tool reply is a JSON envelope: `ok: true` with `data` and `meta`, or `ok: false`
   with a structured `error`. Read `error.class`: retry only `transient` (after the stated
   `retry_after_ms`); on `validation` change the request; on `business` or `permission` do
   not retry - record what you could not read as a gap. `meta.truncated: true` means you saw
   part of the answer, and your findings must say so.
3. Distinguish facts from judgements. Facts (versions, key names, image references, replica
   counts, rates, latencies) are what the tools returned and are filled into the report from
   the tool replies. Judgements (rollout status as you assess it, whether a regression is
   suspected, what to recommend, how risky it is) are yours, and every judgement carries a
   confidence from 0.0 to 1.0 calibrated against these bands:

{_CONFIDENCE_BANDS}

   Below 0.25 means you must not state the conclusion at all - record it as a gap instead.
4. Cite evidence: a line from a tool reply (a status, a signal such as `"error_rate": 0.31`,
   a changed key name, a commit message) or from the context you were given, quoted
   VERBATIM. A judgement with confidence 0.5 or above must cite evidence.
5. Never state configuration VALUES. The tools return key names only; refer to configuration
   by key name and never guess what a value was or is.
6. Be economical with tools: check health when the question is about behaviour, diff when it
   is about cause, both when it is about whether a release caused a symptom, and stop when
   you have what the report needs."""


# ------------------------------------------------------------------- structured output


class ReportedReleases(StrictModel):
    """Which two releases the report is about. `from_version` is null for a first release
    or when only health was checked and no previous version was seen."""

    from_version: str | None = None
    to_version: str


class ReportedFindings(StrictModel):
    """`DeploymentFindings` with the facts removed: the service, the releases, and the four
    judgements. `changed_config_keys`, `image_changes`, and `health_signals` are stamped by
    the runtime from the tool replies for these releases.

    `evidence` and `gaps` are deliberately absent: they live on the envelope, one level up
    (the Day 11 lesson - the schema says so on the object itself)."""

    service: str
    environment: Environment
    environment_detail: str | None = None
    releases_compared: ReportedReleases
    rollout_status: Assessment[RolloutStatus]
    regression_suspected: Assessment[bool]
    rollback_recommendation: Assessment[RollbackRecommendation]
    approval: ApprovalRequirement


class DeploymentReport(StrictModel):
    """The payload the model owns - the `DeploymentAgentResponse` envelope minus the
    plumbing the caller fills in (ids, timestamps, `tool_calls`, and every factual field)."""

    status: ResponseStatus
    status_detail: str | None = None
    summary: str
    findings: ReportedFindings
    evidence: list[Evidence] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)


_TOP_LEVEL_DESCRIPTION = """\
The complete deployment report. Every property below is a top-level argument of this
tool - pass them directly; do not nest them inside a wrapper object.

Rules validated after you answer - a violation rejects the whole report:
0. `evidence` and `gaps` are top-level arguments of this tool. Never put them inside
   `findings`; `findings` accepts no keys beyond its eight.
1. `findings.service` must be a service you asked a tool about, and
   `findings.releases_compared` must be exactly what a `diff_release` reply compared
   (`from_version`, `to_version`) or, when you only checked health, `to_version` must be
   the `version` that `check_rollout_health` reply returned and `from_version` its
   `compared_to_baseline.baseline_version` (or null).
2. Configuration keys, image changes, and health signals are filled in from the tool
   replies for those releases. If you did NOT run `diff_release`, add a gap whose
   `blocks_field` is `findings.changed_config_keys`; if you did NOT get a successful
   `check_rollout_health` for `to_version`, add a gap whose `blocks_field` is
   `findings.health_signals`.
3. Every evidence `excerpt` must appear verbatim in a tool reply or in the context block.
4. `status` may be `complete` only if at least one tool call succeeded.
5. Any `*_detail` field must be null unless its partner field is exactly `other`.
6. `approval.requires_approval` is always true: every recommendation is human-gated.
7. Configuration values never appear anywhere in the report - keys only."""

_FIELD_GUIDANCE: dict[str, dict[str, str]] = {
    ROOT: {
        "status": (
            "`complete` when the question is answered from tool data. `partial` when some "
            "of what was asked could not be read (a failed or truncated tool call, health "
            "or diff not obtained). `insufficient_evidence` when the tools established "
            "almost nothing. `error` only when nothing could be read at all."
        ),
        "status_detail": "Null unless `status` is `other`. Do not describe `partial` here.",
        "summary": "One or two sentences: what the deployment shows about the question.",
        "evidence": (
            "One entry per distinct fact you rely on, quoted verbatim from a tool reply or "
            'the context: a status, a signal line like `"error_rate": 0.31`, a changed '
            "key name, a commit message. `source_type` is `deployment` for rollout status "
            "and replicas, `metric` for error rate, latency, restarts, or probe failures, "
            "`config` for a configuration key or manifest path, `commit` for a commit. "
            "`source_ref` is `<service>@<version>`, the metric name, the key, or the SHA. "
            "Every evidence id cited by an assessment must appear here."
        ),
        "gaps": (
            "What could not be established: a tool error (kind `tool_error`, or "
            "`insufficient_permission` for a permission error), a diff or health check you "
            "did not or could not run, a version the query named that was never deployed. "
            "Set `blocks_field` to the findings path that is null or incomplete because of "
            "it: `findings.changed_config_keys`, `findings.health_signals`, "
            "`findings.regression_suspected.value`, and so on."
        ),
        "overall_confidence": "Your confidence in the report as a whole, on the band table.",
    },
    "ReportedFindings": {
        "service": (
            "The service the report is about, exactly as you passed it to the tools. NOTE: "
            "`findings` holds ONLY service, environment, environment_detail, "
            "releases_compared, rollout_status, regression_suspected, "
            "rollback_recommendation, and approval. `evidence` and `gaps` go at the TOP "
            "LEVEL of the report, never inside `findings` - an extra key here rejects the "
            "whole report."
        ),
        "environment": (
            "`development`, `staging`, or `production` as the tools or the context name it; "
            "`other` with `environment_detail` only when none fits."
        ),
        "environment_detail": "Null unless `environment` is exactly `other`.",
        "releases_compared": (
            "The two releases this report is about. Copy them from the tool replies: what "
            "`diff_release` compared, or the `version` and `baseline_version` that "
            "`check_rollout_health` returned. Never invent a version."
        ),
        "rollout_status": (
            "Your assessment of the rollout: `healthy`, `degraded`, `failed`, `in_progress`, "
            "`rolled_back`, `unknown`, or `other` with `detail`. The tool's `status` is a "
            "deterministic rule on the signals; your value may differ if you have reason, "
            "and either way cite the evidence."
        ),
        "regression_suspected": (
            "Whether the `to_version` release plausibly caused the symptom in the question "
            "or a visible degradation. `true`/`false` with confidence and evidence; null "
            "value (with a gap) below 0.25 confidence or when nothing was measured."
        ),
        "rollback_recommendation": (
            "`rollback_now` when the release is degrading service and the diff gives a "
            "plausible cause; `hold_and_monitor` when signals are marginal or the cause is "
            "unclear; `no_action` when healthy; `insufficient_data` when health could not "
            "be measured; `other` with `detail`. This is a recommendation for a human, never "
            "an action."
        ),
        "approval": (
            "Always human-gated: `requires_approval` true, `risk` as the risk of ACTING on "
            "the recommendation (`low`, `medium`, `high`, or `other` with `risk_detail`), "
            "and `blast_radius` naming what the action would touch, or null if unknown."
        ),
    },
    "ReportedReleases": {
        "from_version": (
            "The older release, exactly as a tool reply named it; null for a first release "
            "or when health was checked and no baseline version was seen."
        ),
        "to_version": "The newer or currently deployed release, exactly as a tool reply named it.",
    },
    "ApprovalRequirement": {
        "requires_approval": "Always true. Every deployment recommendation is human-gated.",
        "risk": "`low`, `medium`, `high`, or `other` with `risk_detail`.",
        "risk_detail": "Null unless `risk` is exactly `other`.",
        "blast_radius": "What acting on the recommendation touches, or null if unknown.",
    },
    "Assessment_RolloutStatus_": {
        "value": "The rollout status, or null below 0.25 confidence (then add a gap).",
        "confidence": "Calibrated to the band table in the system prompt.",
        "evidence": "Ids of the evidence entries supporting this, all present in `evidence`.",
        "reasoning": "One line: which signals lead to this status.",
        "detail": "Non-null exactly when `value` is `other`; null otherwise.",
    },
    "Assessment_bool_": {
        "value": "true or false, or null below 0.25 confidence (then add a gap).",
        "confidence": "Calibrated to the band table in the system prompt.",
        "evidence": "Ids of the evidence entries supporting this, all present in `evidence`.",
        "reasoning": "One line: which change and which signal link (or fail to link).",
        "detail": "Always null here. A boolean has no `other` member.",
    },
    "Assessment_RollbackRecommendation_": {
        "value": "The recommendation, or null below 0.25 confidence (then add a gap).",
        "confidence": "Calibrated to the band table in the system prompt.",
        "evidence": "Ids of the evidence entries supporting this, all present in `evidence`.",
        "reasoning": "One line: why this recommendation follows from the findings.",
        "detail": "Non-null exactly when `value` is `other`; null otherwise.",
    },
    "Evidence": {
        "id": "Opaque id starting `ev_`, referenced by assessments.",
        "source_type": (
            "`deployment` for status/replicas, `metric` for a signal, `config` for a key or "
            "manifest path, `commit` for a commit, `other` with detail for anything else."
        ),
        "source_type_detail": "Null unless `source_type` is exactly `other`.",
        "source_ref": "`<service>@<version>`, the metric name, the key name, or the SHA.",
        "excerpt": "Quoted verbatim from a tool reply or the context. Never paraphrase.",
        "uri": "Leave null; the tools return no URIs.",
        "tool_call_id": "Leave null - the runtime records the real tool call id.",
    },
    "Gap": {
        "kind_detail": "Null unless `kind` is exactly `other`.",
        "blocks_field": (
            "The findings path this gap leaves null or incomplete, e.g. "
            "`findings.health_signals`, `findings.changed_config_keys`, or "
            "`findings.regression_suspected.value`."
        ),
        "resolvable": (
            "True only if another agent, a wider window, or a different version could close "
            "this gap. False stops the coordinator's refinement loop, so set it honestly - a "
            "permission error or a version that never existed is not resolvable by retrying."
        ),
        "suggested_query": "The question to ask next, when `resolvable` is true.",
    },
}


def _apply_guidance(schema: dict[str, Any]) -> dict[str, Any]:
    return apply_guidance(
        schema,
        name="deployment emit",
        description=_TOP_LEVEL_DESCRIPTION,
        guidance=_FIELD_GUIDANCE,
    )


# Generated once at import from the frozen models - never hand-written, so it cannot drift.
_EMIT_SCHEMA: dict[str, Any] = _apply_guidance(DeploymentReport.model_json_schema())

DEPLOYMENT_SYSTEM_PROMPT = f"""\
{_GROUND_RULES}

Investigate with the deployment tools until you can answer the question or have
established that you cannot. Then report your findings by calling `{EMIT_TOOL_NAME}`
exactly once - the report is the only output that counts, so call it as soon as you have
what it needs and write no prose afterwards:

- Name the `service` and the `releases_compared` exactly as the tool replies did. Keys,
  images, and health signals are filled in from those replies - do not repeat them.
- Give the four judgements with confidence and evidence ids: `rollout_status`,
  `regression_suspected`, `rollback_recommendation`, and `approval` (always
  `requires_approval: true`, with your `risk` and `blast_radius`).
- If you did not run `diff_release`, add a gap blocking `findings.changed_config_keys`; if
  you did not get a successful `check_rollout_health` for `to_version`, add a gap blocking
  `findings.health_signals`. Not having looked is a gap, never an empty list.
- Every evidence entry quotes a tool reply or the context VERBATIM. Leave `tool_call_id`
  null; the runtime fills it.
- Set `status` to `complete` only when the question is answered from tool data; `partial`
  when a tool call failed, was truncated, or was not made; `insufficient_evidence` when
  the tools established almost nothing.
- For any enum, when no member fits, use `other` and put the specifics in that field's
  `detail`; leave `detail` null otherwise.

Set `overall_confidence` to your confidence in the report as a whole."""

_EMIT_INSTRUCTION = (
    f"Investigation complete. Now call `{EMIT_TOOL_NAME}` exactly once with the full report, "
    "built only from the tool replies above and the context you were given."
)


class DeploymentAgentError(RuntimeError):
    """The model did not return usable structured output, or its output referenced data it
    was never given. A malformed-but-present payload raises pydantic's ``ValidationError``
    instead.

    ``report`` carries the validated-but-ungrounded report when there is one, so a caller
    (the check script today, the Day 17 validation-retry loop later) can see exactly what
    the model wrote rather than only why it was refused."""

    def __init__(self, message: str, *, report: DeploymentReport | None = None) -> None:
        super().__init__(message)
        self.report = report


def _emit_never_runs(_args: dict[str, Any]) -> ToolResult:
    raise DeploymentAgentError(
        f"{EMIT_TOOL_NAME} is a structured-output tool; it is never executed"
    )


_EMIT_TOOL = ToolSpec(
    name=EMIT_TOOL_NAME,
    description=(
        "Emit the deployment report as structured data. Call this exactly once, only when "
        "asked; it is the only way to answer. The service and releases as the tools named "
        "them, the four judgements with confidence, verbatim evidence, honest gaps."
    ),
    input_schema=_EMIT_SCHEMA,
    handler=_emit_never_runs,
)


# ------------------------------------------------------------------------- the toolset


def default_toolset() -> AbstractContextManager[Toolset]:
    """The real thing: the `aioc-deployment` stdio server, launched in this interpreter."""
    return McpStdioToolset.for_module(DEPLOYMENT_SERVER_MODULE)


# ---------------------------------------------------------------------------- the ledger


class _Ledger(ToolLedger):
    """The shared ledger plus the deployment facts it stamps from: every successful
    `diff_release` reply and every successful `check_rollout_health` reply, each with the
    arguments it was called with (the health lookback is an argument, not a reply field)."""

    def __init__(self, records: list[ToolCallRecord], server: str, *, context: str) -> None:
        self.diffs: list[tuple[dict[str, Any], str]] = []  # (data, tc id)
        self.healths: list[tuple[dict[str, Any], str, dict[str, Any]]] = []  # + arguments
        self.services: set[str] = set()
        super().__init__(records, server, context=context)

    def index(self, data: dict[str, Any], tc_id: str, record: ToolCallRecord) -> None:
        service = data.get("service")
        if isinstance(service, str):
            self.services.add(service)
        if "config_keys_added" in data:
            self.diffs.append((data, tc_id))
        elif "signals" in data:
            self.healths.append((data, tc_id, dict(record.arguments)))

    def diff_for(
        self, service: str, from_version: str | None, to_version: str
    ) -> tuple[dict[str, Any], str] | None:
        for data, tc_id in reversed(self.diffs):
            if (
                data.get("service") == service
                and data.get("from_version") == from_version
                and data.get("to_version") == to_version
            ):
                return data, tc_id
        return None

    def health_for(
        self, service: str, version: str
    ) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
        """The latest health reply for the service that assessed ``version``, or one that
        assessed no particular version (the running instances export none)."""
        for data, tc_id, arguments in reversed(self.healths):
            if data.get("service") == service and data.get("version") in (version, None):
                return data, tc_id, arguments
        return None


# ------------------------------------------------------------------------------ the agent


class DeploymentAgent:
    """Deployment agent: tool-driven, schema-validated release analysis (Day 12)."""

    name = AGENT_NAME

    def __init__(
        self,
        client: LLMClient | None = None,
        *,
        toolset: ToolsetFactory | None = None,
        max_tool_rounds: int = DEFAULT_MAX_TOOL_ROUNDS,
    ) -> None:
        self._client = client or LLMClient()
        self._toolset = toolset or default_toolset
        self._max_rounds = max_tool_rounds

    def assess(
        self,
        query: str,
        *,
        context: str,
        request_id: str | None = None,
        invocation_id: str | None = None,
        usage: Usage | None = None,
    ) -> DeploymentAgentResponse:
        """Answer one deployment question as a schema-validated `DeploymentAgentResponse`.

        ``context`` is the coordinator's explicit context block (``context_passed``,
        CONTRACTS.md sec 5) and must be non-empty - the same rule as every agent. The model
        investigates with the toolset's tools, is then forced through
        ``emit_deployment_report``, and the assembled response is validated against the
        contract envelope plus this module's grounding checks.

        Raises `DeploymentAgentError` for missing/ungrounded output, `McpToolsetError` if
        the server cannot be started, and pydantic's ``ValidationError`` for a payload that
        violates the contract. The validation-retry loop is Day 17, deliberately not here.
        """
        if not query.strip():
            raise ValueError("query must be non-empty")
        if not context.strip():
            raise ValueError(
                "context must be non-empty - the Deployment agent inherits nothing; "
                "pass everything it needs explicitly (CONTRACTS.md sec 5, context_passed)"
            )

        prompt = self._prompt(query.strip(), context.strip())
        # The emit tool is offered during the investigation too (the Day 11 lesson): a
        # model that finishes early and emits on its own is done. The last capture wins.
        captured: list[dict[str, Any]] = []

        def capture(args: dict[str, Any]) -> ToolResult:
            captured.append(dict(args))
            return ToolResult(content="Report recorded. Stop; no further output is needed.")

        emit_tool = ToolSpec(
            name=EMIT_TOOL_NAME,
            description=_EMIT_TOOL.description,
            input_schema=_EMIT_TOOL.input_schema,
            handler=capture,
        )
        with self._toolset() as toolset:
            server = toolset.server_name
            data_tools = list(toolset.tools)
            loop = self._client.run_tool_loop(
                messages=[{"role": "user", "content": prompt}],
                tools=[*data_tools, emit_tool],
                system=DEPLOYMENT_SYSTEM_PROMPT,
                max_iterations=self._max_rounds,
            )
        if usage is not None:
            usage.add(loop.usage)

        if captured:
            payload = captured[-1]
        else:
            resp = self._client.complete(
                messages=[*loop.messages, {"role": "user", "content": _EMIT_INSTRUCTION}],
                system=DEPLOYMENT_SYSTEM_PROMPT,
                tools=[*data_tools, emit_tool],
                tool_choice={"type": "tool", "name": EMIT_TOOL_NAME},
            )
            if usage is not None:
                usage.input_tokens += resp.usage.input_tokens
                usage.output_tokens += resp.usage.output_tokens
            if resp.stop_reason == "max_tokens":
                raise DeploymentAgentError(
                    f"{EMIT_TOOL_NAME} output was truncated at the max_tokens limit "
                    f"({resp.usage.output_tokens} output tokens); the report is incomplete. "
                    "Raise AIOC_MAX_TOKENS or narrow the query."
                )
            payload = _extract_tool_input(resp, EMIT_TOOL_NAME)

        report = DeploymentReport.model_validate(payload)
        wire_calls = [r for r in loop.tool_calls if r.name != EMIT_TOOL_NAME]
        ledger = _Ledger(wire_calls, server, context=context)
        try:
            return _assemble(report, ledger, request_id, invocation_id)
        except DeploymentAgentError as exc:
            exc.report = report
            raise

    @staticmethod
    def _prompt(query: str, context: str) -> str:
        return f"<context>\n{context}\n</context>\n\nDeployment query: {query}"


# ------------------------------------------------------------------------------ assembly


def _assemble(
    report: DeploymentReport,
    ledger: _Ledger,
    request_id: str | None,
    invocation_id: str | None,
) -> DeploymentAgentResponse:
    if report.status is ResponseStatus.COMPLETE and not ledger.any_ok:
        raise DeploymentAgentError(
            "status is `complete` but no tool call succeeded - a report that read nothing "
            "cannot claim to have answered from the deployment"
        )
    reported = report.findings
    releases = reported.releases_compared
    if ledger.any_ok and reported.service not in ledger.services:
        raise DeploymentAgentError(
            f"service {reported.service!r} was never asked of a tool - the tools answered "
            f"for {sorted(ledger.services)}"
        )

    diff = ledger.diff_for(reported.service, releases.from_version, releases.to_version)
    health = ledger.health_for(reported.service, releases.to_version)
    if ledger.any_ok and diff is None and health is None:
        raise DeploymentAgentError(
            f"releases {releases.from_version!r} -> {releases.to_version!r} of "
            f"{reported.service!r} match no diff_release reply and no check_rollout_health "
            "reply - the report must be about releases a tool actually returned"
        )
    if health is not None and diff is None:
        # Health alone: the baseline it reported is the only honest `from_version`.
        data = health[0]
        baseline = (data.get("compared_to_baseline") or {}).get("baseline_version")
        if releases.from_version not in (None, baseline):
            raise DeploymentAgentError(
                f"from_version {releases.from_version!r} was not returned by a tool - the "
                f"health check's baseline was {baseline!r}"
            )

    if diff is None:
        _require_gap(
            report.gaps,
            ("findings.changed_config_keys", "findings.image_changes"),
            "no diff_release reply covers these releases, so changed_config_keys and "
            "image_changes are unknown, not empty",
        )
        changed_keys: list[str] = []
        images: list[ImageChange] = []
    else:
        changed_keys, images = _stamp_diff(diff[0])

    if health is None:
        _require_gap(
            report.gaps,
            ("findings.health_signals",),
            "no check_rollout_health reply covers to_version, so health_signals are unknown",
        )
        signals = HealthSignals()
    else:
        signals = _stamp_health(health[0], health[2])

    evidence = [_ground_evidence(entry, ledger) for entry in report.evidence]

    findings = DeploymentFindings(
        service=reported.service,
        environment=reported.environment,
        environment_detail=reported.environment_detail,
        releases_compared=ReleasesCompared(
            from_version=releases.from_version, to_version=releases.to_version
        ),
        rollout_status=reported.rollout_status,
        changed_config_keys=changed_keys,
        image_changes=images,
        health_signals=signals,
        regression_suspected=reported.regression_suspected,
        rollback_recommendation=reported.rollback_recommendation,
        approval=reported.approval.model_copy(update={"requires_approval": True}),
    )
    return DeploymentAgentResponse(
        request_id=request_id or new_id("req"),
        invocation_id=invocation_id or new_id("inv"),
        # Settled, not copied: `complete` over a null judgement is `partial` (sec 3), and
        # the runtime can see the judgements. The first live sequential run was refused
        # for exactly this - one honest null with its gap, and `complete` on the envelope.
        status=settle_status(report.status, findings),
        status_detail=report.status_detail,
        summary=report.summary,
        findings=findings,
        evidence=evidence,
        gaps=report.gaps,
        overall_confidence=report.overall_confidence,
        tool_calls=ledger.refs,
        generated_at=datetime.now(UTC),
    )


def _require_gap(gaps: list[Gap], fields: tuple[str, ...], why: str) -> None:
    for gap in gaps:
        blocked = gap.blocks_field or ""
        if any(blocked == f or blocked.startswith(f + ".") for f in fields):
            return
    raise DeploymentAgentError(
        f"{why}; the report must carry a gap with blocks_field one of {list(fields)} "
        "(CONTRACTS.md sec 1: not looked is null with a gap, never an empty list)"
    )


def _stamp_diff(data: dict[str, Any]) -> tuple[list[str], list[ImageChange]]:
    keys: set[str] = set()
    for section in ("config_keys_added", "config_keys_removed", "config_keys_changed"):
        for key in data.get(section) or []:
            if isinstance(key, str):
                keys.add(key)
    images: list[ImageChange] = []
    for raw in data.get("image_changes") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("to_image"), str):
            continue
        images.append(
            ImageChange(
                container=str(raw.get("container") or data.get("service") or ""),
                from_image=raw.get("from_image"),
                to_image=raw["to_image"],
                from_digest=raw.get("from_digest"),
                to_digest=raw.get("to_digest"),
            )
        )
    return sorted(keys), images


def _stamp_health(data: dict[str, Any], arguments: dict[str, Any]) -> HealthSignals:
    replicas: dict[str, Any] = data.get("replicas") or {}
    signals: dict[str, Any] = data.get("signals") or {}
    lookback = arguments.get("lookback_minutes", 30)
    return HealthSignals(
        replicas_desired=_int_or_none(replicas.get("desired")),
        replicas_ready=_int_or_none(replicas.get("ready")),
        restart_count=_int_or_none(signals.get("restart_count")),
        probe_failures=_int_or_none(signals.get("probe_failures")),
        error_rate=_float_or_none(signals.get("error_rate")),
        p99_latency_ms=_float_or_none(signals.get("p99_latency_ms")),
        observed_over_seconds=int(lookback) * 60 if isinstance(lookback, int) else None,
    )


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _float_or_none(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _ground_evidence(entry: Evidence, ledger: _Ledger) -> Evidence:
    """Every excerpt must quote a tool reply or the context verbatim; a reply's quote gets
    the real tool call id stamped, a context quote keeps `tool_call_id` null."""
    tc_id = ledger.quoted_in(entry.excerpt)
    if tc_id is not None:
        return entry.model_copy(update={"tool_call_id": tc_id})
    if ledger.in_context(entry.excerpt):
        return entry.model_copy(update={"tool_call_id": None})
    raise DeploymentAgentError(
        f"evidence {entry.id} excerpt does not appear verbatim in any tool reply or in the "
        "context - excerpts must never be paraphrased"
    )


def _extract_tool_input(resp: Any, tool_name: str) -> dict[str, Any]:
    """Pull the forced tool call's input object out of the response, or fail clearly."""
    for block in resp.content:
        if isinstance(block, ToolUseBlock) and block.name == tool_name:
            if not isinstance(block.input, dict):
                raise DeploymentAgentError(f"{tool_name} input was not a JSON object")
            return dict(block.input)
    raise DeploymentAgentError(
        f"model did not call {tool_name} (stop_reason={resp.stop_reason!r}); no structured output"
    )
