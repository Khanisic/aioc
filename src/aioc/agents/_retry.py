"""The validation-retry loop (Day 17): a rejected report is re-requested with the error attached.

Every agent ends the same way - the model is forced through an ``emit_*`` tool and the
payload is validated against the contract and the agent's own grounding rules. Until Day
17 a refused payload raised, the executor recorded the whole error as a gap, and the
refinement loop re-ran the *whole* agent blind: another investigation, another tool loop,
another report, and no reason to expect a different one. Three live-only refusals were
waiting for exactly this loop (HANDOFF sec 7 items 20 and 23): a paraphrased GitHub
excerpt, three times; a Deployment report about a version no health reply covered; a Docs
report with a field the model invented.

The loop lives here, in the emit step, because that is the only place the rejected
payload and the error are both in hand. The conversation is kept: the model's ``tool_use``
turn is followed by a ``tool_result`` carrying ``is_error: true`` and the rendered error,
and the emit tool is forced again. Nothing is re-investigated and no tool is re-run; the
model is asked to fix what the error names and to change nothing else.

**Two kinds of rejection, told apart in the record and in the feedback.** A ``format``
rejection is pydantic refusing the shape or a cross-field invariant (an extra field, a
``detail`` on a non-``other`` enum, a null with no gap): the information is in the
report and the model mis-shaped it, so the retry says so. A ``grounding`` rejection is the
agent's own rule refusing a claim about data the model was never given (an unfetched PR, a
document retrieval did not return, a quote that is not verbatim): the information may be
genuinely absent, so the feedback says that an honest gap is the correct answer and a
closer paraphrase is not. The record keeps both kinds and whether the retry recovered, so
"retry-resolvable" is measured per run rather than asserted (`RetryLog.summary`).

**Two stopping rules, neither a judgement call.** A cap (`LLMSettings.max_validation_retries`,
default 2), and an identical rejection: the same kind and the same rendered error twice in a
row means the model cannot fix it and a third attempt would fail identically - the same
rule the refinement loop applies to an identical gap. When the loop stops, the last
exception is raised unchanged (the agents' callers and tests keep their error types) with a
note describing the attempts, which the executor's gap keeps.

**What is not retried.** Truncation at ``max_tokens`` (the same budget fails the same
way), a model that did not call the forced tool at all, and errors that are not the
model's doing (no repository configured, a tool reply missing a timestamp): each of these
carries ``kind=None`` and is raised on the first attempt, exactly as before Day 17.

The record is not part of the frozen contract. It sits in a `RetryLog` the agent is given
(or the process-wide default, `default_retry_log`), which the live scripts print and
record next to the response.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Any, cast

from anthropic.types import Message, MessageParam, ToolUseBlock
from pydantic import ValidationError

from aioc.llm import LLMClient, ToolSpec, Usage


class RejectionKind(StrEnum):
    """Why a report was refused, as the loop tells the model and the record keeps it."""

    FORMAT = "format"  # shape or a cross-field invariant: the model has the information
    GROUNDING = "grounding"  # a claim about data the model was not given: it may be absent


class ReportRejected(RuntimeError):
    """Base of the agents' own rejections. ``kind`` says whether the loop re-requests: a
    ``grounding`` error by default, ``None`` for a failure that is not the model's doing
    (truncation, no tool call, missing configuration, a malformed tool reply)."""

    def __init__(
        self, message: str, *, kind: RejectionKind | None = RejectionKind.GROUNDING
    ) -> None:
        super().__init__(message)
        self.kind = kind


def classify_rejection(exc: BaseException) -> RejectionKind | None:
    """The kind of a refusal, or ``None`` when a retry cannot change the outcome."""
    if isinstance(exc, ValidationError):
        return RejectionKind.FORMAT
    if isinstance(exc, ReportRejected):
        return exc.kind
    return None


def render_rejection(exc: BaseException) -> str:
    """The error as the model is shown it: pydantic's errors one per line as ``field:
    message``, without the documentation URLs and the echoed input; anything else is its
    message. The same text is compared for the identical-rejection rule, so it must not
    carry anything that varies between two attempts at the same mistake."""
    if isinstance(exc, ValidationError):
        lines = [f"{exc.error_count()} validation error(s) against the contract:"]
        for err in exc.errors(include_url=False, include_input=False):
            loc = ".".join(str(part) for part in err["loc"]) or "(top level)"
            lines.append(f"- {loc}: {err['msg']}")
        return "\n".join(lines)
    return str(exc)


@dataclass(frozen=True, slots=True)
class Rejection:
    """One refused attempt."""

    attempt: int  # 1-based: the attempt that was refused
    kind: RejectionKind
    error_type: str
    message: str

    def same_as(self, other: Rejection) -> bool:
        return self.kind is other.kind and " ".join(self.message.split()) == " ".join(
            other.message.split()
        )


@dataclass(frozen=True, slots=True)
class RetryRecord:
    """The loop's account of one emit step: how many attempts, what each refusal was, and
    whether the last attempt was accepted. Recorded for every emit, including the ones
    accepted first time, so the log is a complete denominator."""

    agent: str
    request_id: str | None
    invocation_id: str | None
    attempts: int
    rejections: tuple[Rejection, ...]
    accepted: bool
    stopped_by: str | None  # "max_retries" | "identical_rejection" when not accepted

    @property
    def outcome(self) -> str:
        if self.accepted:
            return "accepted" if not self.rejections else "recovered"
        return "exhausted"

    @property
    def kinds(self) -> tuple[RejectionKind, ...]:
        return tuple(r.kind for r in self.rejections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "request_id": self.request_id,
            "invocation_id": self.invocation_id,
            "attempts": self.attempts,
            "outcome": self.outcome,
            "stopped_by": self.stopped_by,
            "rejections": [
                {
                    "attempt": r.attempt,
                    "kind": r.kind.value,
                    "error_type": r.error_type,
                    "message": r.message,
                }
                for r in self.rejections
            ],
        }


class RetryLog:
    """Where the records go. Thread-safe: the executor runs agents on worker threads."""

    def __init__(self) -> None:
        self._records: list[RetryRecord] = []
        self._lock = Lock()

    def record(self, record: RetryRecord) -> None:
        with self._lock:
            self._records.append(record)

    @property
    def records(self) -> list[RetryRecord]:
        with self._lock:
            return list(self._records)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def for_invocation(self, invocation_id: str) -> list[RetryRecord]:
        return [r for r in self.records if r.invocation_id == invocation_id]

    def summary(self) -> dict[str, Any]:
        """Counts by outcome and, for the refused attempts, by kind - the measurement the
        Day 17 plan asks for: how many rejections a retry resolved, and of which kind."""
        records = self.records
        outcomes = Counter(r.outcome for r in records)
        by_kind: dict[str, dict[str, int]] = {}
        for record in records:
            for kind in set(record.kinds):
                bucket = by_kind.setdefault(
                    kind.value, {"rejected": 0, "recovered": 0, "exhausted": 0}
                )
                bucket["rejected"] += 1
                bucket["recovered" if record.accepted else "exhausted"] += 1
        return {
            "emits": len(records),
            "accepted_first_try": outcomes.get("accepted", 0),
            "recovered": outcomes.get("recovered", 0),
            "exhausted": outcomes.get("exhausted", 0),
            "retries": sum(r.attempts - 1 for r in records),
            "by_kind": by_kind,
            "stopped_by": dict(Counter(r.stopped_by for r in records if r.stopped_by)),
        }

    def render_summary(self) -> str:
        s = self.summary()
        if not s["emits"]:
            return "validation-retry loop: no reports emitted"
        lines = [
            f"validation-retry loop: {s['emits']} report(s) emitted, "
            f"{s['accepted_first_try']} accepted first try, {s['recovered']} recovered by "
            f"retry, {s['exhausted']} exhausted ({s['retries']} retry call(s))"
        ]
        for kind, bucket in sorted(s["by_kind"].items()):
            lines.append(
                f"  {kind}: {bucket['rejected']} report(s) rejected, "
                f"{bucket['recovered']} recovered, {bucket['exhausted']} exhausted"
            )
        for record in self.records:
            for rejection in record.rejections:
                first = rejection.message.splitlines()[0]
                lines.append(
                    f"  {record.agent} {record.invocation_id or '-'} attempt "
                    f"{rejection.attempt} [{rejection.kind.value}] {rejection.error_type}: {first}"
                )
        return "\n".join(lines)


_DEFAULT_LOG = RetryLog()


def default_retry_log() -> RetryLog:
    """The process-wide log every agent records to unless given its own."""
    return _DEFAULT_LOG


# ------------------------------------------------------------------------------ the loop


def emit_with_retry[T](
    client: LLMClient,
    *,
    agent: str,
    messages: Sequence[MessageParam],
    system: str,
    tools: Sequence[ToolSpec],
    emit_tool: str,
    build: Callable[[dict[str, Any]], T],
    error_type: type[ReportRejected],
    usage: Usage | None,
    max_retries: int,
    log: RetryLog,
    request_id: str | None,
    invocation_id: str | None,
    captured: dict[str, Any] | None = None,
    grounding_advice: str | None = None,
) -> T:
    """Force ``emit_tool``, validate the payload with ``build``, and re-request on a
    retryable rejection with the error attached, until accepted or a stopping rule.

    ``messages`` is the conversation up to the point the report is asked for. With
    ``captured`` the first payload was already recorded during the tool loop (the model
    emitted on its own) and no first call is made; its retry is a plain user message
    because that ``tool_use`` already has its result. Otherwise the first call is made
    here, and every retry answers the refused ``tool_use`` with an error ``tool_result``.

    ``grounding_advice`` replaces the agents' advice on a ``grounding`` rejection, for an
    emitter whose schema has no gaps to record (the coordinator's synthesis).

    ``build`` turns the payload into the agent's validated response and raises on any
    rejection; `classify_rejection` decides whether the loop re-requests. Token counts go
    to ``usage`` for every attempt, refused ones included - a rejected report still cost.
    """
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    convo: list[MessageParam] = list(messages)
    rejections: list[Rejection] = []
    pending: str | None
    if captured is not None:
        payload, pending = captured, None
    else:
        payload, pending = _request(
            client,
            convo,
            system=system,
            tools=tools,
            emit_tool=emit_tool,
            error_type=error_type,
            usage=usage,
        )
    attempt = 1
    while True:
        try:
            result = build(payload)
        except Exception as exc:
            kind = classify_rejection(exc)
            if kind is None:
                raise
            rejection = Rejection(
                attempt=attempt,
                kind=kind,
                error_type=type(exc).__name__,
                message=render_rejection(exc),
            )
            rejections.append(rejection)
            stopped_by: str | None = None
            if attempt > max_retries:
                stopped_by = "max_retries"
            elif len(rejections) >= 2 and rejection.same_as(rejections[-2]):
                stopped_by = "identical_rejection"
            if stopped_by is not None:
                record = RetryRecord(
                    agent=agent,
                    request_id=request_id,
                    invocation_id=invocation_id,
                    attempts=attempt,
                    rejections=tuple(rejections),
                    accepted=False,
                    stopped_by=stopped_by,
                )
                log.record(record)
                exc.add_note(_exhausted_note(record))
                raise
            feedback = render_feedback(
                emit_tool,
                rejection,
                remaining=max_retries - attempt + 1,
                grounding_advice=grounding_advice,
            )
            if pending is None:
                convo.append({"role": "user", "content": feedback})
            else:
                convo.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": pending,
                                "content": feedback,
                                "is_error": True,
                            }
                        ],
                    }
                )
            payload, pending = _request(
                client,
                convo,
                system=system,
                tools=tools,
                emit_tool=emit_tool,
                error_type=error_type,
                usage=usage,
            )
            attempt += 1
            continue
        log.record(
            RetryRecord(
                agent=agent,
                request_id=request_id,
                invocation_id=invocation_id,
                attempts=attempt,
                rejections=tuple(rejections),
                accepted=True,
                stopped_by=None,
            )
        )
        return result


def _request(
    client: LLMClient,
    convo: list[MessageParam],
    *,
    system: str,
    tools: Sequence[ToolSpec],
    emit_tool: str,
    error_type: type[ReportRejected],
    usage: Usage | None,
) -> tuple[dict[str, Any], str]:
    """One forced call. Appends the assistant turn to ``convo`` and returns the payload
    with the ``tool_use`` id a retry must answer."""
    resp = client.complete(
        messages=convo,
        system=system,
        tools=tools,
        tool_choice={"type": "tool", "name": emit_tool},
    )
    if usage is not None:
        usage.record(resp.usage)
    # Truncation before validation, and never retried: a report cut off mid-JSON parses
    # as a missing required field, and the same budget would cut it the same way.
    if resp.stop_reason == "max_tokens":
        raise error_type(
            f"{emit_tool} output was truncated at the max_tokens limit "
            f"({resp.usage.output_tokens} output tokens); the report is incomplete. "
            "Raise AIOC_MAX_TOKENS or narrow the query.",
            kind=None,
        )
    block = _emit_block(resp, emit_tool, error_type)
    convo.append(cast("MessageParam", {"role": "assistant", "content": resp.content}))
    return dict(block.input), block.id


def _emit_block(resp: Message, emit_tool: str, error_type: type[ReportRejected]) -> ToolUseBlock:
    for block in resp.content:
        if isinstance(block, ToolUseBlock) and block.name == emit_tool:
            if not isinstance(block.input, dict):
                raise error_type(f"{emit_tool} input was not a JSON object", kind=None)
            return block
    raise error_type(
        f"model did not call {emit_tool} (stop_reason={resp.stop_reason!r}); no structured output",
        kind=None,
    )


# ----------------------------------------------------------------------------- the words


_FORMAT_ADVICE = (
    "This is a shape problem. The information is already in your report; re-emit it in the "
    "shape the schema and the rules require."
)
_GROUNDING_ADVICE = (
    "Your report cited or asserted something that is not in the tool replies, documents, or "
    "context you were given. Do not paraphrase it more closely and do not invent it: remove "
    "it, set the affected value to null, and record a gap whose `blocks_field` names the field "
    "it blocks. When the information is genuinely absent, an honest gap is the correct answer."
)


def render_feedback(
    emit_tool: str,
    rejection: Rejection,
    *,
    remaining: int,
    grounding_advice: str | None = None,
) -> str:
    """What the model is told when its report is refused."""
    advice = (
        _FORMAT_ADVICE
        if rejection.kind is RejectionKind.FORMAT
        else grounding_advice or _GROUNDING_ADVICE
    )
    return (
        f"Your `{emit_tool}` call was rejected and has NOT been recorded "
        f"({remaining} attempt(s) remain).\n\n"
        f"Rejected because:\n{rejection.message}\n\n"
        f"Call `{emit_tool}` again with a corrected report. Fix exactly what the error names "
        f"and keep everything else as it was. {advice}"
    )


def _exhausted_note(record: RetryRecord) -> str:
    kinds = ", ".join(k.value for k in record.kinds)
    how = {
        "max_retries": f"the retry cap of {record.attempts - 1} was reached",
        "identical_rejection": (
            "the last two rejections were identical, so another attempt would fail the same way"
        ),
    }[record.stopped_by or "max_retries"]
    return (
        f"validation-retry loop: {record.attempts} attempt(s), rejected {len(record.rejections)} "
        f"time(s) [{kinds}]; {how}"
    )
