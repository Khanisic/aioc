"""Incident agent (Days 3-4): expert-SRE prompt, then schema-validated output.

Day 3 shipped the skeleton - ``investigate`` returns *prose*. Day 4 adds ``diagnose``,
which returns a schema-validated ``aioc.contracts.IncidentAgentResponse`` by forcing the
model through a single structured-output tool (``tool_use`` + ``tool_choice``). Day 5 wires
real Prometheus data into the context both methods already accept.

Both methods share one set of ground rules (`_GROUND_RULES`) so the SRE persona, the
evidence-citation rule, and the CONTRACTS.md sec 2.1 confidence bands can never drift between
the prose and structured paths. The two invariants that are cheap and graded are enforced in
code, not just prompted:

- **Explicit context passing.** Both entry points require a non-empty ``context`` block and
  embed it verbatim - nothing is inherited (CONTRACTS.md sec 5, ``context_passed``).
- **Confidence bands.** The band table is in the system prompt verbatim; the eval harness
  (Day 19) scores calibration against those exact ranges.

Why force a tool instead of asking for JSON: ``tool_choice`` guarantees the model emits a
value that matches a JSON Schema, and that schema is generated straight from the frozen
Pydantic models, so the wire shape cannot drift from the contract. The final validation still
runs the response-scoped envelope invariants (evidence resolution, null-value-needs-a-Gap,
``status`` vs null analytic values); a payload that violates them raises here. Re-requesting on
that failure with the error attached is the Day 17 validation-retry loop - deliberately not
built yet, so a bad payload surfaces loudly rather than being silently patched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from anthropic.types import Message, TextBlock, ToolUseBlock
from pydantic import Field

from aioc.contracts import (
    Evidence,
    Gap,
    IncidentAgentResponse,
    IncidentFindings,
    ResponseStatus,
    StrictModel,
)
from aioc.llm import LLMClient, ToolResult, ToolSpec, Usage

from ._annotate import ROOT, apply_guidance

AGENT_NAME = "incident"

# The band table is quoted from CONTRACTS.md sec 2.1 and must not drift from it - the eval
# harness (Day 19) scores calibration against these exact ranges.
_CONFIDENCE_BANDS = """\
| Range | Meaning |
|---|---|
| 0.90-1.00 | Directly evidenced by two or more independent sources |
| 0.70-0.89 | Directly evidenced by a single reliable source |
| 0.50-0.69 | Inferred from correlated signals; no direct statement |
| 0.25-0.49 | Plausible hypothesis, weak or partial evidence |
| below 0.25 | Speculation - do not state a conclusion; record it as a gap instead |"""

# Shared by both entry points so the graded behaviours (SRE persona, evidence citation,
# confidence bands) are defined once. The prose tail and the structured tail append to this.
_GROUND_RULES = f"""\
You are the Incident agent of AIOC, an AI operations center. You are an expert Site
Reliability Engineer investigating production incidents: metric anomalies, error spikes,
latency regressions, failed deploys, and resource exhaustion.

Ground rules:

1. Work ONLY from the context provided in the message. You inherit nothing. If the context
   does not contain something, you do not know it - say so instead of guessing.
2. Cite evidence for every claim. Name the specific metric, log line, event, or timestamp
   that supports it. A claim with no citable evidence is a hypothesis and must be labelled
   as one.
3. Estimate confidence for every conclusion as a number from 0.0 to 1.0, calibrated
   against these bands:

{_CONFIDENCE_BANDS}

   Below 0.25 means you must not state the conclusion at all - list it under gaps as
   something that could not be determined and what data would resolve it.
4. Distinguish "looked and found nothing" from "could not look". They are different
   findings and both are worth reporting.
5. Never repeat configuration VALUES (connection strings, tokens, passwords) even if they
   appear in the context. Refer to configuration by key name only."""

INCIDENT_SYSTEM_PROMPT = f"""\
{_GROUND_RULES}

Structure your prose response as:
- **Summary** - one or two sentences on what is happening.
- **Findings** - each with its evidence citation and confidence number.
- **Gaps** - what you could not determine and what data would resolve it.
- **Recommended next steps** - ordered, most valuable first.

Be concise and concrete. This is an incident, not an essay."""

# ------------------------------------------------------------------- structured output (Day 4)

EMIT_TOOL_NAME = "emit_incident_report"


class IncidentReport(StrictModel):
    """The analytic payload the model owns - the `IncidentAgentResponse` envelope minus the
    plumbing a caller fills in (``schema_version``, ``agent``, the ids, ``generated_at``, and
    ``tool_calls``, which reflects real tool calls the harness records, not model output).

    Its JSON Schema is what the structured-output tool advertises, generated from these frozen
    models so the wire shape stays pinned to the contract. Validation of the assembled response
    still happens on `IncidentAgentResponse`, which is where the response-scoped invariants live.
    """

    status: ResponseStatus
    status_detail: str | None = None
    summary: str
    findings: IncidentFindings
    evidence: list[Evidence] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    overall_confidence: float = Field(ge=0.0, le=1.0)


# ------------------------------------------------------------------- schema annotation
#
# Why annotate at all is documented once, in `aioc.agents._annotate` - this agent was where
# the pattern was measured into existence (Day 4), and the Docs agent (Day 8) shares it.

_ROOT = ROOT

_TOP_LEVEL_DESCRIPTION = """\
The complete incident diagnosis. Every property below is a top-level argument of this tool -
pass them directly; do not nest them inside a wrapper object.

Three rules are validated after you answer, and a violation rejects the whole report:
1. Any `*_detail` field must be null unless its partner field is exactly `other`.
2. Every evidence id you cite must exist in the top-level `evidence` list.
3. A null assessment `value` requires a matching entry in `gaps` naming the field it blocks."""

# Keyed by `$defs` name, or `_ROOT` for the top-level object.
_FIELD_GUIDANCE: dict[str, dict[str, str]] = {
    _ROOT: {
        "status": (
            "`complete` only when no analytic `value` in findings is null. Use `partial` when "
            "some are, or `insufficient_evidence` when almost nothing could be established."
        ),
        "status_detail": (
            "Null unless `status` is `other`. Do not describe a `partial` status here."
        ),
        "summary": "One or two sentences: what is happening and to which service.",
        "evidence": (
            "Every evidence id cited anywhere in `findings` must appear here, each with a "
            "verbatim `excerpt` from the provided context. Cite nothing you cannot quote."
        ),
        "gaps": (
            "What you could not establish. Required whenever an assessment `value` is null - "
            "set that gap's `blocks_field` to the field it blocks."
        ),
        "overall_confidence": "Your confidence in the diagnosis as a whole, on the band table.",
    },
    "Assessment_Severity_": {
        "value": "Null if your confidence is below 0.25. Never guess a severity.",
        "confidence": "Calibrated to the band table in the system prompt.",
        "evidence": "Ids of the evidence entries supporting this, all present in `evidence`.",
        "reasoning": "One line: how the evidence leads to this value.",
        "detail": "Null unless `value` is exactly `other`.",
    },
    "Assessment_FailureMode_": {
        "value": "Null if your confidence is below 0.25. Never guess a failure mode.",
        "confidence": "Calibrated to the band table in the system prompt.",
        "evidence": "Ids of the evidence entries supporting this, all present in `evidence`.",
        "reasoning": "One line: how the evidence leads to this value.",
        "detail": "Null unless `value` is exactly `other`.",
    },
    "Assessment_str_": {
        "value": "Free text, or null if your confidence is below 0.25.",
        "confidence": "Calibrated to the band table in the system prompt.",
        "evidence": "Ids of the evidence entries supporting this, all present in `evidence`.",
        "reasoning": "One line: how the evidence leads to this value.",
        "detail": (
            "Always null here. This value is free text, so it has no `other` member to detail."
        ),
    },
    "Evidence": {
        "id": "Opaque id starting `ev_`, referenced by the assessments that rely on it.",
        "excerpt": "Quoted verbatim from the provided context. Never paraphrase or invent.",
        "source_type_detail": "Null unless `source_type` is exactly `other`.",
    },
    "Gap": {
        "kind_detail": "Null unless `kind` is exactly `other`.",
        "blocks_field": (
            "Dotted path of the field this gap blocks, e.g. `findings.root_cause.value`. "
            "Required when this gap explains a null assessment value."
        ),
        "resolvable": (
            "True only if another agent or more data could close this gap. False stops the "
            "coordinator's refinement loop, so set it honestly."
        ),
        "suggested_query": "The question to ask next, when `resolvable` is true.",
    },
    "RecommendedAction": {
        "risk_detail": "Null unless `risk` is exactly `other`.",
        "requires_approval": (
            "True for anything that mutates production state or is medium/high risk."
        ),
        "command": "Refer to configuration by key name only - never include a config value.",
    },
    "TimelineEvent": {
        "kind_detail": "Null unless `kind` is exactly `other`.",
        "at": (
            "RFC 3339 with an explicit `Z`. Entries must be listed oldest first - a timeline "
            "out of ascending order by this field is rejected."
        ),
    },
    "Impact": {
        "error_rate_before": "Null if not observed. Never estimate a number you did not see.",
        "requests_affected": "Null if not observed. Never estimate a number you did not see.",
    },
    "IncidentFindings": {
        "affected_services": (
            "Empty list means you looked and found none; that is different from not looking, "
            "which is a gap."
        ),
        "timeline": (
            "Oldest event first, strictly ascending by `at`. Sort before you emit; a timeline "
            "in any other order is rejected outright."
        ),
        "similar_incidents": (
            "Empty list unless the context actually names prior incidents. Do not invent ids."
        ),
    },
}


def _apply_guidance(schema: dict[str, Any]) -> dict[str, Any]:
    """Attach the invariant guidance to the generated schema, failing loudly on drift."""
    return apply_guidance(
        schema,
        name="incident emit",
        description=_TOP_LEVEL_DESCRIPTION,
        guidance=_FIELD_GUIDANCE,
    )


# Generated once at import from the frozen models - never hand-written, so it cannot drift.
_EMIT_SCHEMA: dict[str, Any] = _apply_guidance(IncidentReport.model_json_schema())

INCIDENT_STRUCTURED_SYSTEM_PROMPT = f"""\
{_GROUND_RULES}

Report your diagnosis by calling the `{EMIT_TOOL_NAME}` tool exactly once. Do not write any
prose outside the tool call. Fill its fields as follows:

- The analytic fields - `severity`, `failure_mode`, `root_cause`, and each
  `contributing_factors` entry - are assessments: give a `value`, a `confidence` in [0, 1]
  using the bands above, the `evidence` ids that support it, and a one-line `reasoning`. If
  your confidence would be below 0.25, set `value` to null instead of guessing.
- Every evidence id you cite must appear in the top-level `evidence` list, each carrying a
  verbatim `excerpt` from the context and its `source_type`.
- A null assessment `value` REQUIRES a matching entry in `gaps` whose `blocks_field` names the
  field it blocks (for example `findings.root_cause.value`). Never leave a null unexplained.
- Set `status` to `complete` only when no analytic value is null; otherwise use `partial`
  (or `insufficient_evidence` if you could establish almost nothing).
- For any enum, when no member fits, use `other` and put the specifics in that field's
  `detail`; leave `detail` null otherwise.
- In `recommended_actions`, anything that mutates production state or is medium/high risk must
  set `requires_approval` to true. Refer to configuration by key name only, never a value.
- Leave any `impact` metric you did not observe as null rather than guessing a number, and keep
  `timeline` in ascending time order.

Set `overall_confidence` to your confidence in the diagnosis as a whole."""


class IncidentAgentError(RuntimeError):
    """The model did not return usable structured output (no forced tool call, or non-object
    input). A malformed-but-present payload raises pydantic's ``ValidationError`` instead."""


def _emit_never_runs(_args: dict[str, Any]) -> ToolResult:
    # `diagnose` forces this tool with tool_choice and reads the tool_use block directly, so the
    # handler is never invoked. Raise loudly if some future caller routes it through a tool loop.
    raise IncidentAgentError(f"{EMIT_TOOL_NAME} is a structured-output tool; it is never executed")


_EMIT_TOOL = ToolSpec(
    name=EMIT_TOOL_NAME,
    description=(
        "Emit the incident diagnosis as structured data. Call this exactly once; it is the only "
        "way to answer. Analytic fields are assessments (value, confidence, evidence ids, "
        "reasoning); every cited evidence id must appear in the evidence list; a null value "
        "needs a matching gap."
    ),
    input_schema=_EMIT_SCHEMA,
    handler=_emit_never_runs,
)


def _new_id(prefix: str) -> str:
    """A local opaque id for standalone runs. The coordinator supplies real ids from Day 6."""
    return f"{prefix}_{uuid4().hex[:8]}"


@dataclass(slots=True)
class IncidentProse:
    """Day 3 return shape: the agent's prose plus the accounting the later phases need."""

    text: str
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int


class IncidentAgent:
    """Incident agent: prose (Day 3, ``investigate``) or schema-validated (Day 4, ``diagnose``)."""

    name = AGENT_NAME

    def __init__(self, client: LLMClient | None = None) -> None:
        self._client = client or LLMClient()

    def investigate(self, query: str, *, context: str) -> IncidentProse:
        """Answer one operational query from the given context, in prose (Day 3).

        ``context`` is the explicit context block a coordinator would record in
        ``AgentInvocation.context_passed``. It must be non-empty: an empty context means the
        caller assumed inheritance, which is exactly the failure this project demonstrates the
        absence of.
        """
        prompt = self._prompt(query, context)
        resp = self._client.complete(
            messages=[{"role": "user", "content": prompt}],
            system=INCIDENT_SYSTEM_PROMPT,
        )
        text = "".join(block.text for block in resp.content if isinstance(block, TextBlock))
        return IncidentProse(
            text=text,
            model=resp.model,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )

    def diagnose(
        self,
        query: str,
        *,
        context: str,
        request_id: str | None = None,
        invocation_id: str | None = None,
        usage: Usage | None = None,
    ) -> IncidentAgentResponse:
        """Answer one operational query as a schema-validated `IncidentAgentResponse` (Day 4).

        The model is forced through the ``emit_incident_report`` tool, so its output matches the
        contract's JSON Schema; the assembled response is then validated against the full
        `IncidentAgentResponse`, which runs the response-scoped invariants. ``request_id`` /
        ``invocation_id`` come from the coordinator once it exists (Day 6); a standalone caller
        may omit them and a local id is generated.

        ``usage`` is the Day 7 cost seam: the executor passes one `Usage` accumulator through
        every model call in a request, and `CoordinatorResponse.cost` is read off it rather
        than estimated. Token counts are added even when the call fails validation later -
        a rejected report still cost real tokens.

        Raises `IncidentAgentError` if the model does not emit the forced tool, and pydantic's
        ``ValidationError`` if the payload is present but violates the contract.
        """
        prompt = self._prompt(query, context)
        resp = self._client.complete(
            messages=[{"role": "user", "content": prompt}],
            system=INCIDENT_STRUCTURED_SYSTEM_PROMPT,
            tools=[_EMIT_TOOL],
            tool_choice={"type": "tool", "name": EMIT_TOOL_NAME},
        )
        if usage is not None:
            usage.input_tokens += resp.usage.input_tokens
            usage.output_tokens += resp.usage.output_tokens
        # Check truncation before validating. A report cut off mid-JSON still yields a tool_use
        # block holding whatever parsed, so pydantic reports it as a missing required field -
        # which reads as "the model forgot `overall_confidence`" when the real cause is the token
        # budget. Measured: Opus fills 4096 output tokens on this schema without finishing.
        if resp.stop_reason == "max_tokens":
            raise IncidentAgentError(
                f"{EMIT_TOOL_NAME} output was truncated at the max_tokens limit "
                f"({resp.usage.output_tokens} output tokens); the report is incomplete. "
                "Raise AIOC_MAX_TOKENS or narrow the query."
            )
        payload = _extract_tool_input(resp, EMIT_TOOL_NAME)

        data: dict[str, Any] = dict(payload)
        # Plumbing the model does not own; schema_version, agent, and tool_calls take their
        # contract defaults (1.0.0, "incident", []). Day 5 populates tool_calls from real calls.
        data["request_id"] = request_id or _new_id("req")
        data["invocation_id"] = invocation_id or _new_id("inv")
        data["generated_at"] = datetime.now(UTC)
        return IncidentAgentResponse.model_validate(data)

    @staticmethod
    def _prompt(query: str, context: str) -> str:
        if not query.strip():
            raise ValueError("query must be non-empty")
        if not context.strip():
            raise ValueError(
                "context must be non-empty - the Incident agent inherits nothing; "
                "pass everything it needs explicitly (CONTRACTS.md sec 5, context_passed)"
            )
        return f"<context>\n{context.strip()}\n</context>\n\nOperational query: {query.strip()}"


def _extract_tool_input(resp: Message, tool_name: str) -> dict[str, Any]:
    """Pull the forced tool call's input object out of the response, or fail clearly."""
    for block in resp.content:
        if isinstance(block, ToolUseBlock) and block.name == tool_name:
            if not isinstance(block.input, dict):
                raise IncidentAgentError(f"{tool_name} input was not a JSON object")
            return dict(block.input)
    raise IncidentAgentError(
        f"model did not call {tool_name} (stop_reason={resp.stop_reason!r}); no structured output"
    )
