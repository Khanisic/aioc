"""Synthesis: the coordinator's answer from the agents' reports (Day 7 deterministic, Day 14
model-written).

Two implementations of one seam, and the executor holds the seam:

**`deterministic`** is the Day 7 fallback and the one every offline test exercises. The
answer is adopted from the highest-confidence agent report and cites that report's own
evidence ids, so the `CoordinatorResponse` invariant - the coordinator cites its subagents
and never mints evidence ids - holds by construction. It costs nothing and cannot
hallucinate, and its known limitation is the one the Day 10 demo recorded: with two agents
it picks one report's summary as the headline rather than merging both halves.

**`ModelSynthesiser`** is the Day 14 model-written synthesis, opt-in at the entry point the
same way tracing is (`Executor(synthesiser=ModelSynthesiser())`; the default executor stays
deterministic so the offline suite makes no network call). It is given the query, the
intent, one bounded digest per agent response (`aioc.coordinator.handoff.digest` - the same
block a dependent agent is handed, so what the synthesis reads is what the agents said, not
the raw envelopes), and the gaps still open after the last refinement round, and it is
forced through one structured-output tool. The grounding rule is enforced in code, not
requested in the prompt: every evidence id the answer cites must exist in some agent
response's ``evidence[]``, and an answer that claims the evidenced band (>= 0.5) must cite
something. A synthesis that fails either check raises `SynthesisError`; the executor then
falls back to `deterministic` and says so in the answer's ``reasoning``, because a request
that has already paid for every agent run must not be thrown away for one bad sentence at
the end - but it must not carry an ungrounded sentence either.

The model is asked for exactly the two fields it is competent to write (the prose
`synthesis` and the `answer` assessment); `refinement_rounds`, `status`, `cost`, and the
gap list are facts about the run and are stamped by the executor.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import Field

from aioc.agents._annotate import ROOT, apply_guidance
from aioc.agents._retry import (
    RejectionKind,
    ReportRejected,
    RetryLog,
    default_retry_log,
    emit_with_retry,
    render_rejection,
)
from aioc.agents.incident import _CONFIDENCE_BANDS
from aioc.contracts import (
    AgentInvocation,
    AgentResponse,
    Assessment,
    Gap,
    Intent,
    StrictModel,
    walk_assessments,
)
from aioc.llm import LLMClient, ToolResult, ToolSpec, Usage

from .handoff import digest

EMIT_TOOL_NAME = "emit_synthesis"


@dataclass(frozen=True, slots=True)
class SynthesisRequest:
    """Everything a synthesiser may draw on. Nothing else reaches it - in particular not
    the situation block or the plan, which the agents already received as context."""

    query: str
    intent: Assessment[Intent]
    # Each response with the invocation that produced it (round, mode, context) - the
    # executor knows both, and the round is a fact a synthesiser must not guess.
    reports: Sequence[tuple[AgentInvocation, AgentResponse]]
    execution_gaps: Sequence[Gap]
    unresolved_gaps: Sequence[Gap]
    refinement_rounds: int

    @property
    def responses(self) -> list[AgentResponse]:
        return [response for _, response in self.reports]


@dataclass(frozen=True, slots=True)
class Synthesis:
    synthesis: str
    answer: Assessment[str]


class Synthesiser(Protocol):
    def synthesise(self, request: SynthesisRequest, *, usage: Usage) -> Synthesis: ...


class SynthesisError(ReportRejected):
    """The model's synthesis could not be used: a malformed payload, ungrounded evidence,
    no tool call, or truncation. The executor falls back to `deterministic` on this.

    ``kind`` is the validation-retry loop's (`agents._retry`): a malformed payload and a
    confident answer that cites nothing are ``format`` (the ids are in the digests it was
    given), an invented id is ``grounding``, and truncation or a missing call are ``None``
    and never re-requested."""


# ------------------------------------------------------------------------- deterministic


def deterministic(request: SynthesisRequest) -> Synthesis:
    """The Day 7 synthesis: adopt the highest-confidence report's summary and its own
    evidence ids, so every id resolves by construction."""
    lines: list[str] = []
    for inv, r in request.reports:
        rnd = f", round {inv.round}" if inv.round else ""
        lines.append(
            f"- {r.agent.value} ({r.status.value}, confidence {r.overall_confidence:.2f}{rnd}): "
            f"{r.summary}"
        )
    for gap in request.execution_gaps:
        lines.append(f"- not executed: {gap.description}")

    if not request.responses:
        synthesis = "No selected agent could be executed for this query."
        if lines:
            synthesis += "\n" + "\n".join(lines)
        answer = Assessment[str](
            value=None,
            confidence=0.0,
            evidence=[],
            reasoning="No selected agent produced a response; see unresolved_gaps.",
            detail=None,
        )
        return Synthesis(synthesis=synthesis, answer=answer)

    primary = max(request.responses, key=lambda r: r.overall_confidence)
    cited = cited_evidence(primary)
    confidence = primary.overall_confidence
    reasoning = (
        f"Adopted from the {primary.agent.value} agent's report, the highest-confidence "
        f"response of {len(request.responses)}."
    )
    if not cited:
        # A report can carry confidence while citing nothing only when its own status is
        # insufficient_evidence or error; an uncited answer must not claim the >= 0.5 band
        # the contract reserves for evidenced conclusions.
        confidence = min(confidence, 0.49)
        reasoning += " No evidence was cited by that report, so confidence is capped below 0.5."

    intent = request.intent.value.value if request.intent.value else "unclassified"
    rounds = (
        f", after {request.refinement_rounds} refinement round(s)"
        if request.refinement_rounds
        else ""
    )
    synthesis = (
        f"Synthesis of {len(request.responses)} agent response(s) (intent: {intent}{rounds}):\n"
        + "\n".join(lines)
    )
    answer = Assessment[str](
        value=primary.summary,
        confidence=confidence,
        evidence=cited,
        reasoning=reasoning,
        detail=None,
    )
    return Synthesis(synthesis=synthesis, answer=answer)


def cited_evidence(response: AgentResponse) -> list[str]:
    """The evidence ids a report's own assessments cite, deduplicated, in citation order.

    Falls back to everything the report recorded: evidence an agent gathered but attached
    no conclusion to is still the basis of its summary.
    """
    seen: dict[str, None] = {}
    for _, assessment in walk_assessments(response.findings, "findings"):
        for ev in assessment.evidence:
            seen.setdefault(ev, None)
    if not seen:
        for ev_entry in response.evidence:
            seen.setdefault(ev_entry.id, None)
    return list(seen)


# ------------------------------------------------------------------------ model-written


class ModelSynthesis(StrictModel):
    """The exact shape the model is asked for: the prose and the one-line conclusion, as
    flat scalars. The runtime assembles the `Assessment[str]`.

    Flat on purpose, measured: the first live run asked for ``answer: Assessment[str]`` as
    a nested object, and Sonnet - primed by a prompt full of ``<handoff>`` blocks - wrote
    the nested argument as XML-style parameter text inside a string, which pydantic
    refused and the executor fell back on. A flat field has no nesting to get wrong.
    """

    synthesis: str
    answer: str | None
    confidence: float
    evidence: list[str] = Field(default_factory=list)
    reasoning: str


_GUIDANCE: dict[str, dict[str, str]] = {
    ROOT: {
        "synthesis": (
            "The prose answer for the operator: what was found and by which agent, where "
            "the reports agree or disagree, and what is still open. Every statement must "
            "come from a handoff block below; add no facts of your own. Plain paragraphs, "
            "no headings."
        ),
        "answer": (
            "The one- or two-sentence conclusion that answers the operator's question "
            "directly. Null only if no report supports any conclusion at all - then say "
            "why in `reasoning` and keep `confidence` below 0.25."
        ),
        "confidence": (
            "Your confidence in `answer`, 0 to 1, calibrated against the bands in the "
            "system prompt. Not higher than the reports it rests on."
        ),
        "evidence": (
            "Evidence ids exactly as listed under `evidence` in the handoff blocks "
            "(`ev_...`) that `answer` rests on. Required when `answer` is set and "
            "confidence is 0.5 or more. Never invent an id and never cite one that is not "
            "listed."
        ),
        "reasoning": "One or two sentences: which reports drove the conclusion, and why.",
    },
}

SYNTHESIS_SYSTEM_PROMPT = f"""\
You are the coordinator of AIOC, an AI operations centre. Specialist agents have already
investigated the operator's question, each with only the context it was explicitly handed,
and their reports are below as handoff blocks. You write the final synthesis. You do not
investigate, you do not add facts, and you do not soften a gap.

Rules, enforced after you answer:

1. Every statement in `synthesis` and in `answer` comes from a handoff block. If the
   blocks disagree, say so and say which is better evidenced; do not average them.
2. `evidence` cites ids from the blocks' `evidence` lists only. An id that is not listed
   does not exist and citing it rejects the whole synthesis. Document, claim, incident,
   action, and gap ids are not evidence ids, however relevant the thing they name.
3. Confidence follows these bands and is never higher than the reports it rests on:

{_CONFIDENCE_BANDS}

4. Unresolved gaps are part of the answer. Name what is still open and why it could not be
   closed; a synthesis that reads as complete when the run was not is wrong.
5. Configuration is referred to by key name only; never repeat a value.

Call `{EMIT_TOOL_NAME}` exactly once. Do not write prose outside the tool call."""


def _emit_never_runs(_args: dict[str, Any]) -> ToolResult:
    raise SynthesisError(f"{EMIT_TOOL_NAME} is a structured-output tool; it is never executed")


_EMIT_SCHEMA = apply_guidance(
    ModelSynthesis.model_json_schema(),
    name="synthesis",
    description=(
        "Your synthesis of the agents' reports as structured data. Every property is a "
        "top-level argument of this tool - pass them directly as plain values, do not nest "
        "them in a wrapper object. Validated after you answer: every evidence id must "
        "exist in a handoff block, and an answer at confidence 0.5 or more must cite at "
        "least one."
    ),
    guidance=_GUIDANCE,
)

_EMIT_TOOL = ToolSpec(
    name=EMIT_TOOL_NAME,
    description=(
        "Emit the final synthesis for the operator. Call this exactly once; it is the only "
        "way to answer."
    ),
    input_schema=_EMIT_SCHEMA,
    handler=_emit_never_runs,
)


class ModelSynthesiser:
    """One forced structured-output call over the agents' digests, grounded in code, inside
    the validation-retry loop every agent's emit runs in (`agents._retry`).

    The loop was missing here until the 2026-09-30 four-agent run: the synthesis came back
    without `answer` and `confidence`, a format slip one re-request fixes, and the whole
    model-written answer was thrown away for the deterministic form instead."""

    def __init__(
        self,
        client: LLMClient | None = None,
        *,
        max_validation_retries: int | None = None,
        retry_log: RetryLog | None = None,
    ) -> None:
        self._client = client or LLMClient()
        # None means the harness setting (AIOC_MAX_VALIDATION_RETRIES); 0 disables the loop.
        self._max_retries = (
            max_validation_retries
            if max_validation_retries is not None
            else self._client.settings.max_validation_retries
        )
        self._retry_log = retry_log if retry_log is not None else default_retry_log()

    def synthesise(self, request: SynthesisRequest, *, usage: Usage) -> Synthesis:
        if not request.responses:
            # Nothing to synthesise from; the deterministic text says so without a call.
            return deterministic(request)

        def build(payload: dict[str, Any]) -> Synthesis:
            try:
                parsed = ModelSynthesis.model_validate(payload)
                answer = Assessment[str](
                    value=parsed.answer.strip() if parsed.answer else None,
                    confidence=parsed.confidence,
                    evidence=parsed.evidence,
                    reasoning=parsed.reasoning.strip(),
                    detail=None,
                )
            except ValueError as exc:
                raise SynthesisError(
                    f"{EMIT_TOOL_NAME} payload failed validation: {render_rejection(exc)}",
                    kind=RejectionKind.FORMAT,
                ) from exc
            check_grounding(answer, request.responses)
            return Synthesis(synthesis=parsed.synthesis.strip(), answer=answer)

        return emit_with_retry(
            self._client,
            agent="synthesis",
            messages=[{"role": "user", "content": render_prompt(request)}],
            system=SYNTHESIS_SYSTEM_PROMPT,
            tools=[_EMIT_TOOL],
            emit_tool=EMIT_TOOL_NAME,
            build=build,
            error_type=SynthesisError,
            usage=usage,
            max_retries=self._max_retries,
            log=self._retry_log,
            request_id=None,
            invocation_id=None,
            grounding_advice=_GROUNDING_ADVICE,
        )


_GROUNDING_ADVICE = (
    "Cite only ids listed under `evidence` in the handoff blocks. Remove the id that is not "
    "listed; if nothing listed supports the answer, lower `confidence` below 0.5 and say in "
    "`reasoning` what the answer lacks."
)


def check_grounding(answer: Assessment[str], responses: Sequence[AgentResponse]) -> None:
    """The coordinator cites its subagents and never mints an id (CONTRACTS.md sec 5)."""
    known = {e.id for r in responses for e in r.evidence}
    unknown = [ev for ev in answer.evidence if ev not in known]
    if unknown:
        raise SynthesisError(
            f"answer cites evidence id(s) {unknown} that no agent response carries - the "
            "synthesis must cite the agents' evidence, not invent its own"
        )
    if answer.value is not None and answer.confidence >= 0.5 and not answer.evidence:
        raise SynthesisError(
            "answer claims the evidenced band (confidence >= 0.5) but cites no evidence "
            "(CONTRACTS.md sec 2.1)",
            kind=RejectionKind.FORMAT,
        )


def render_prompt(request: SynthesisRequest) -> str:
    """What the synthesiser reads: the query, the intent, one digest per response, and the
    open gaps. The digests are the handoff module's, bounded the same way."""
    intent = request.intent.value.value if request.intent.value else "unclassified"
    parts = [
        f"<query>\n{request.query.strip()}\n</query>",
        f"<intent value={intent!r} confidence={request.intent.confidence:.2f} />",
        f"<refinement_rounds>{request.refinement_rounds}</refinement_rounds>",
        "",
        "Agent reports, one handoff block each (the only source you may draw on):",
        "",
    ]
    for inv, response in request.reports:
        if inv.round:
            parts.append(f"(refinement round {inv.round}, invocation {inv.invocation_id})")
        parts.append(digest(response))
        parts.append("")
    if request.execution_gaps:
        parts.append("Invocations that did not produce a report:")
        parts.extend(f"- {g.description}" for g in request.execution_gaps)
        parts.append("")
    if request.unresolved_gaps:
        parts.append("Gaps still open after the last round (name them in the synthesis):")
        for g in request.unresolved_gaps:
            flag = "resolvable" if g.resolvable else "unresolvable"
            blocks = f" blocks={g.blocks_field}" if g.blocks_field else ""
            parts.append(f"- {g.id} [{flag}]{blocks}: {g.description}")
    else:
        parts.append("No gaps remain open.")
    return "\n".join(parts).rstrip()
