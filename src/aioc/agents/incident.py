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
from aioc.llm import LLMClient, ToolResult, ToolSpec

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


# Generated once at import from the frozen models - never hand-written, so it cannot drift.
_EMIT_SCHEMA: dict[str, Any] = IncidentReport.model_json_schema()

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
    ) -> IncidentAgentResponse:
        """Answer one operational query as a schema-validated `IncidentAgentResponse` (Day 4).

        The model is forced through the ``emit_incident_report`` tool, so its output matches the
        contract's JSON Schema; the assembled response is then validated against the full
        `IncidentAgentResponse`, which runs the response-scoped invariants. ``request_id`` /
        ``invocation_id`` come from the coordinator once it exists (Day 6); a standalone caller
        may omit them and a local id is generated.

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
