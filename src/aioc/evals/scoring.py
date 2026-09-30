"""Scoring (Day 19): one agent response against the seed's answer key.

Pure functions over contract models - no model call, no database, no clock. The same
response always scores the same, so a recorded run can be re-scored when a rule changes
and a score can be argued with by reading this file.

The three measurements the plan asks for, and what each one counts here:

- **Accuracy** - a judgement compared with recorded truth. For a diagnosis: the failure
  mode and the severity against the row's ``true_*`` columns, and the affected services
  as precision and recall. For a recall: whether the incident's own post-mortem was cited.
  An abstention (a null value) is not correct, and is counted apart from a wrong answer,
  because the contract asks for a null over a guess and a score that cannot tell them
  apart rewards guessing.
- **Hallucination rate** - a checkable statement with nothing behind it. For a diagnosis
  every evidence excerpt, service, timeline timestamp, impact number, and similar-incident
  id is looked for in the context the agent was given (`ground_diagnosis`); what is not
  there was invented. The Incident agent has no in-code grounding check, so this is
  measured on its accepted output. For a recall the agent's own grounding rule refuses an
  unretrieved source or a paraphrased quote before a response exists, so the rate is read
  off the retry records instead (`Summary.grounding_refusals`), plus the one thing that
  rule cannot see: a precedent asserted for a question the corpus has no answer to.
- **Tool success rate** - ``ToolCallRef.ok`` over every ``tool_calls`` entry.

**A stitched excerpt is not an invented one.** The first live run flagged two evidence
excerpts as ungrounded whose every part was in the context word for word: the model had
quoted two adjacent metric lines as one excerpt, once dropping the list marker between
them and once joining them with a semicolon. Nothing in them was made up, so counting
them would have measured this module's line breaks. An excerpt is therefore read three
ways: ``verbatim`` (it is in the context as written), ``stitched`` (it is not, but every
part of it is - counted and reported on its own line, never as a hallucination), and
ungrounded (some part of it is in the context nowhere).

Calibration is scored against the contract's bands (sec 2.1): each scored judgement lands
in the band its stated confidence names, and a band's accuracy is what that confidence
was worth.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import anthropic

from aioc.contracts import AgentResponse, DocsAgentResponse, IncidentAgentResponse, SourceType
from aioc.coordinator.confidence import BANDS, Band, band
from aioc.llm import BatchError, Usage

from .cases import EvalItem, Task
from .seed import IMPACT_COLUMNS, SeedIncident

_SEVERITY_ORDER = ("sev1", "sev2", "sev3", "sev4")


@dataclass(frozen=True, slots=True)
class Judgement:
    """One scored analytic field."""

    field: str
    expected: str
    actual: str | None  # None: the agent abstained
    confidence: float
    band: Band
    correct: bool

    @property
    def abstained(self) -> bool:
        return self.actual is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
            "confidence": self.confidence,
            "band": self.band.value,
            "correct": self.correct,
        }


@dataclass(frozen=True, slots=True)
class Grounding:
    """What was looked for in the agent's context, and what was not there. ``stitched``
    is the evidence whose parts are all in the context but not as one run of text; it is
    among the ``checked`` and is not among the ``ungrounded``."""

    checked: int = 0
    ungrounded: tuple[str, ...] = ()
    stitched: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "ungrounded": list(self.ungrounded),
            "stitched": list(self.stitched),
        }


@dataclass(frozen=True, slots=True)
class RetryStats:
    attempts: int = 1
    rejections: tuple[str, ...] = ()  # the kind of each refused attempt, in order
    outcome: str = "accepted"  # accepted | recovered | exhausted

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "rejections": list(self.rejections),
            "outcome": self.outcome,
        }


@dataclass(frozen=True, slots=True)
class ItemScore:
    """One item, scored. ``correct`` is the item's headline: the failure mode for a
    diagnosis, the expected citation for a recall, the abstention for a probe; ``None``
    when the agent produced no response to score."""

    key: str
    case_id: str
    task: Task
    agent: str
    incident_id: str | None
    answered: bool
    error: str | None
    correct: bool | None
    # Whose failure it was, for an item with no response: the `agent` gave up on its
    # report (a result of the thing under test), or the `environment` refused the call
    # (the API, the network, the account - a result of nothing, and worth running again).
    error_kind: str | None = None
    judgements: tuple[Judgement, ...] = ()
    services_precision: float | None = None
    services_recall: float | None = None
    severity_within_one: bool | None = None
    grounding: Grounding = field(default_factory=Grounding)
    retrieved_expected: bool | None = None
    cited_documents: tuple[str, ...] = ()
    supported_claims: int = 0
    invented_precedent: bool | None = None
    tool_calls: int = 0
    tool_calls_ok: int = 0
    status: str | None = None
    overall_confidence: float | None = None
    retry: RetryStats = field(default_factory=RetryStats)
    usage: Usage = field(default_factory=Usage)
    seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "case_id": self.case_id,
            "task": self.task.value,
            "agent": self.agent,
            "incident_id": self.incident_id,
            "answered": self.answered,
            "error": self.error,
            "error_kind": self.error_kind,
            "correct": self.correct,
            "judgements": [j.to_dict() for j in self.judgements],
            "services_precision": self.services_precision,
            "services_recall": self.services_recall,
            "severity_within_one": self.severity_within_one,
            "grounding": self.grounding.to_dict(),
            "retrieved_expected": self.retrieved_expected,
            "cited_documents": list(self.cited_documents),
            "supported_claims": self.supported_claims,
            "invented_precedent": self.invented_precedent,
            "tool_calls": self.tool_calls,
            "tool_calls_ok": self.tool_calls_ok,
            "status": self.status,
            "overall_confidence": self.overall_confidence,
            "retry": self.retry.to_dict(),
            "usage": self.usage.as_record(),
            "seconds": None if self.seconds is None else round(self.seconds, 2),
        }


# ----------------------------------------------------------------------------- diagnose


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


_BULLET = re.compile(r"^\s*[-*]\s+", re.M)
# Where a model joins two quoted lines: a line break, or a semicolon or a bar between them.
_JOIN = re.compile(r"\n|;\s+|\s\|\s")


def _unlisted(text: str) -> str:
    """Flattened, with list markers removed - the context is rendered as a list, and a
    quote that spans two items should not have to reproduce the marker between them."""
    return _flat(_BULLET.sub("", text))


def quoted(excerpt: str, context: str) -> str | None:
    """How an excerpt stands in the context: ``verbatim``, ``stitched``, or ``None`` when
    some part of it is not there at all."""
    shown = _unlisted(context)
    if _unlisted(excerpt) in shown:
        return "verbatim"
    parts = [_unlisted(part).strip(" .;,") for part in _JOIN.split(excerpt)]
    parts = [part for part in parts if part]
    if len(parts) > 1 and all(part in shown for part in parts):
        return "stitched"
    return None


def _judge(name: str, expected: str, actual: Any, confidence: float) -> Judgement:
    value = None if actual is None else str(getattr(actual, "value", actual))
    return Judgement(
        field=name,
        expected=expected,
        actual=value,
        confidence=confidence,
        band=band(confidence),
        correct=value == expected,
    )


def _within_one(expected: str, actual: str | None) -> bool | None:
    """Adjacent on the sev1-sev4 scale. ``None`` when either side is off the scale."""
    if actual not in _SEVERITY_ORDER or expected not in _SEVERITY_ORDER:
        return None
    return abs(_SEVERITY_ORDER.index(actual) - _SEVERITY_ORDER.index(expected)) <= 1


def ground_diagnosis(
    response: IncidentAgentResponse, context: str, incident: SeedIncident
) -> Grounding:
    """Every checkable statement in a diagnosis, looked for in the context it came from."""
    flat = _flat(context)
    checked = 0
    missing: list[str] = []
    stitched: list[str] = []

    for entry in response.evidence:
        checked += 1
        standing = quoted(entry.excerpt, context)
        if standing is None:
            missing.append(f"evidence {entry.id}: excerpt is not in the context")
        elif standing == "stitched":
            stitched.append(f"evidence {entry.id}: joined from lines that are each verbatim")
    for service in response.findings.affected_services:
        checked += 1
        if _flat(service) not in flat:
            missing.append(f"affected service {service!r} is not in the context")
    for event in response.findings.timeline:
        checked += 1
        stamp = event.at.strftime("%Y-%m-%dT%H:%M:%SZ")
        if stamp.casefold() not in flat:
            missing.append(f"timeline event {event.id}: no signal at {stamp}")
    for incident_id in response.findings.similar_incidents:
        # The context names no prior incident, so any id here came from nowhere.
        checked += 1
        missing.append(f"similar incident {incident_id!r}: the context names none")
    impact = response.findings.impact
    for name in IMPACT_COLUMNS:
        stated = getattr(impact, name)
        if stated is None:
            continue
        checked += 1
        shown = incident.impact[name]
        if shown is None or float(stated) != float(shown):
            was = "not measured" if shown is None else shown
            missing.append(f"impact.{name}: stated {stated}, the context shows {was}")
    return Grounding(checked=checked, ungrounded=tuple(missing), stitched=tuple(stitched))


def score_diagnosis(item: EvalItem, response: IncidentAgentResponse) -> ItemScore:
    incident = item.incident
    if incident is None:
        raise ValueError(f"{item.key}: a diagnosis is scored against an incident")
    findings = response.findings
    mode = _judge(
        "failure_mode",
        incident.true_failure_mode,
        findings.failure_mode.value,
        findings.failure_mode.confidence,
    )
    severity = _judge(
        "severity", incident.true_severity, findings.severity.value, findings.severity.confidence
    )
    named, truth = set(findings.affected_services), set(incident.affected_services)
    hits = len(named & truth)
    return ItemScore(
        key=item.key,
        case_id=item.case_id,
        task=item.task,
        agent=item.agent,
        incident_id=incident.id,
        answered=True,
        error=None,
        correct=mode.correct,
        judgements=(mode, severity),
        services_precision=hits / len(named) if named else None,
        services_recall=hits / len(truth) if truth else None,
        severity_within_one=_within_one(severity.expected, severity.actual),
        grounding=ground_diagnosis(response, item.context, incident),
        **_envelope(response),
    )


# ------------------------------------------------------------------------------- recall


def _doc(ref: str) -> str:
    return ref.split("#", 1)[0]


def cited_documents(response: DocsAgentResponse) -> tuple[str, ...]:
    """The documents a recall stands on: every source of a supported claim, and every
    document evidence entry. An unsupported claim cites nothing by definition."""
    cited: list[str] = []
    for claim in response.findings.claims:
        if claim.supported:
            cited.extend(source.document_id for source in claim.sources)
    cited.extend(
        _doc(entry.source_ref)
        for entry in response.evidence
        if entry.source_type is SourceType.DOCUMENT
    )
    return tuple(dict.fromkeys(cited))


def score_recall(
    item: EvalItem, response: DocsAgentResponse, *, retrieved: Sequence[str] | None = None
) -> ItemScore:
    """``retrieved`` is the document ids retrieval returned for this question, when the
    runner recorded them - it separates "retrieval missed it" from "the agent ignored it"."""
    cited = cited_documents(response)
    supported = sum(1 for claim in response.findings.claims if claim.supported)
    answer = response.findings.answer
    expected = item.expected_document
    quotes = sum(
        1
        for claim in response.findings.claims
        for source in claim.sources
        if source.quote is not None
    )
    if expected is None:
        # A probe: the corpus has no answer, so the only correct one is none. A stated
        # answer standing on supported claims is a precedent that does not exist.
        correct = answer.value is None
        invented: bool | None = answer.value is not None and supported > 0
        retrieved_expected = None
    else:
        correct = expected in cited
        invented = None
        retrieved_expected = None if retrieved is None else expected in retrieved
    return ItemScore(
        key=item.key,
        case_id=item.case_id,
        task=item.task,
        agent=item.agent,
        incident_id=None if item.incident is None else item.incident.id,
        answered=True,
        error=None,
        correct=correct,
        # Accepted output is grounded by the agent's own rule; the count is the quotes
        # that rule verified, so the denominator is honest and the numerator is zero.
        grounding=Grounding(checked=quotes),
        retrieved_expected=retrieved_expected,
        cited_documents=cited,
        supported_claims=supported,
        invented_precedent=invented,
        **_envelope(response),
    )


def _envelope(response: AgentResponse) -> dict[str, Any]:
    return {
        "tool_calls": len(response.tool_calls),
        "tool_calls_ok": sum(1 for call in response.tool_calls if call.ok),
        "status": response.status.value,
        "overall_confidence": response.overall_confidence,
    }


def score_item(
    item: EvalItem, response: AgentResponse, *, retrieved: Sequence[str] | None = None
) -> ItemScore:
    if item.task is Task.DIAGNOSE:
        if not isinstance(response, IncidentAgentResponse):
            raise TypeError(f"{item.key}: expected an incident response")
        return score_diagnosis(item, response)
    if not isinstance(response, DocsAgentResponse):
        raise TypeError(f"{item.key}: expected a docs response")
    return score_recall(item, response, retrieved=retrieved)


def is_environment_error(error: BaseException) -> bool:
    """The call itself failed: the API refused it, the network dropped it, or the batch
    lost it. Not the agent's doing, and not evidence about the agent."""
    return isinstance(error, (anthropic.APIError, BatchError))


def score_failure(item: EvalItem, error: BaseException) -> ItemScore:
    """An item that produced no response, and whose failure it was."""
    notes = getattr(error, "__notes__", [])
    text = f"{type(error).__name__}: {error}"
    if notes:
        text += " | " + " | ".join(notes)
    return ItemScore(
        key=item.key,
        case_id=item.case_id,
        task=item.task,
        agent=item.agent,
        incident_id=None if item.incident is None else item.incident.id,
        answered=False,
        error=" ".join(text.split()),
        error_kind="environment" if is_environment_error(error) else "agent",
        correct=None,
    )


# ------------------------------------------------------------------------------ summary


@dataclass(frozen=True, slots=True)
class Rate:
    """A count over a denominator. ``value`` is ``None`` when there was nothing to count -
    the contract's distinction again: not measured is not zero."""

    hits: int
    total: int

    @property
    def value(self) -> float | None:
        return self.hits / self.total if self.total else None

    def render(self) -> str:
        if not self.total:
            return "n/a (0)"
        return f"{self.hits}/{self.total} = {self.hits / self.total:.0%}"

    def to_dict(self) -> dict[str, Any]:
        return {"hits": self.hits, "total": self.total, "rate": self.value}


@dataclass(frozen=True, slots=True)
class BandCalibration:
    band: Band
    lower: float
    judgements: int
    correct: int
    mean_confidence: float | None

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.judgements if self.judgements else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "band": self.band.value,
            "lower": self.lower,
            "judgements": self.judgements,
            "correct": self.correct,
            "accuracy": self.accuracy,
            "mean_confidence": self.mean_confidence,
        }


@dataclass(frozen=True, slots=True)
class Summary:
    items: int
    answered: Rate
    # accuracy
    failure_mode: Rate
    failure_mode_abstained: int
    failure_mode_by_mode: dict[str, Rate]
    severity: Rate
    severity_within_one: Rate
    services_precision: float | None
    services_recall: float | None
    recall_cited: Rate
    recall_retrieved: Rate
    probes_abstained: Rate
    # hallucination
    ungrounded: Rate
    items_with_ungrounded: Rate
    stitched: Rate
    invented_precedents: Rate
    grounding_refusals: Rate
    # tools
    tool_success: Rate
    # calibration and retries
    calibration: tuple[BandCalibration, ...]
    retries: dict[str, int]
    usage: Usage

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": self.items,
            "answered": self.answered.to_dict(),
            "accuracy": {
                "failure_mode": self.failure_mode.to_dict(),
                "failure_mode_abstained": self.failure_mode_abstained,
                "failure_mode_by_mode": {
                    mode: rate.to_dict() for mode, rate in self.failure_mode_by_mode.items()
                },
                "severity": self.severity.to_dict(),
                "severity_within_one": self.severity_within_one.to_dict(),
                "services_precision": self.services_precision,
                "services_recall": self.services_recall,
                "recall_cited": self.recall_cited.to_dict(),
                "recall_retrieved": self.recall_retrieved.to_dict(),
                "probes_abstained": self.probes_abstained.to_dict(),
            },
            "hallucination": {
                "ungrounded": self.ungrounded.to_dict(),
                "items_with_ungrounded": self.items_with_ungrounded.to_dict(),
                "stitched": self.stitched.to_dict(),
                "invented_precedents": self.invented_precedents.to_dict(),
                "grounding_refusals": self.grounding_refusals.to_dict(),
            },
            "tool_success": self.tool_success.to_dict(),
            "calibration": [c.to_dict() for c in self.calibration],
            "retries": dict(self.retries),
            "usage": self.usage.as_record(),
        }


def _mean(values: Iterable[float | None]) -> float | None:
    kept = [v for v in values if v is not None]
    return sum(kept) / len(kept) if kept else None


def _judgements(scores: Sequence[ItemScore], name: str) -> list[Judgement]:
    return [j for score in scores for j in score.judgements if j.field == name]


def calibrate(judgements: Sequence[Judgement]) -> tuple[BandCalibration, ...]:
    """Accuracy per contract band, over the judgements that stated a value. An abstention
    states no conclusion, so it has no confidence to be calibrated."""
    stated = [j for j in judgements if not j.abstained]
    rows: list[BandCalibration] = []
    for lower, name, _meaning in BANDS:
        inside = [j for j in stated if j.band is name]
        rows.append(
            BandCalibration(
                band=name,
                lower=lower,
                judgements=len(inside),
                correct=sum(1 for j in inside if j.correct),
                mean_confidence=_mean(j.confidence for j in inside),
            )
        )
    return tuple(rows)


def tool_success(responses: Iterable[AgentResponse]) -> Rate:
    """``ToolCallRef.ok`` over every tool call in the given responses - usable on any
    agent's output, which is how the recorded live runs are read (`scripts/run_evals.py`)."""
    calls = [call for response in responses for call in response.tool_calls]
    return Rate(sum(1 for call in calls if call.ok), len(calls))


def summarise(scores: Sequence[ItemScore]) -> Summary:
    answered = [s for s in scores if s.answered]
    diagnoses = [s for s in answered if s.task is Task.DIAGNOSE]
    recalls = [s for s in answered if s.task is Task.RECALL and s.incident_id is not None]
    probes = [s for s in answered if s.task is Task.RECALL and s.incident_id is None]

    modes = _judgements(diagnoses, "failure_mode")
    severities = _judgements(diagnoses, "severity")
    by_mode: dict[str, Rate] = {}
    for expected in sorted({j.expected for j in modes}):
        of_mode = [j for j in modes if j.expected == expected]
        by_mode[expected] = Rate(sum(1 for j in of_mode if j.correct), len(of_mode))

    within = [s.severity_within_one for s in diagnoses if s.severity_within_one is not None]
    measured = [s for s in recalls if s.retrieved_expected is not None]
    usage = Usage()
    for score in scores:
        usage.add(score.usage)

    emits = sum(1 for s in scores if s.answered or s.retry.rejections)
    refused = sum(1 for s in scores if "grounding" in s.retry.rejections)
    return Summary(
        items=len(scores),
        answered=Rate(len(answered), len(scores)),
        failure_mode=Rate(sum(1 for j in modes if j.correct), len(modes)),
        failure_mode_abstained=sum(1 for j in modes if j.abstained),
        failure_mode_by_mode=by_mode,
        severity=Rate(sum(1 for j in severities if j.correct), len(severities)),
        severity_within_one=Rate(sum(1 for ok in within if ok), len(within)),
        services_precision=_mean(s.services_precision for s in diagnoses),
        services_recall=_mean(s.services_recall for s in diagnoses),
        recall_cited=Rate(sum(1 for s in recalls if s.correct), len(recalls)),
        recall_retrieved=Rate(sum(1 for s in measured if s.retrieved_expected), len(measured)),
        probes_abstained=Rate(sum(1 for s in probes if s.correct), len(probes)),
        ungrounded=Rate(
            sum(len(s.grounding.ungrounded) for s in diagnoses),
            sum(s.grounding.checked for s in diagnoses),
        ),
        items_with_ungrounded=Rate(
            sum(1 for s in diagnoses if s.grounding.ungrounded), len(diagnoses)
        ),
        stitched=Rate(
            sum(len(s.grounding.stitched) for s in diagnoses),
            sum(s.grounding.checked for s in diagnoses),
        ),
        invented_precedents=Rate(sum(1 for s in probes if s.invented_precedent), len(probes)),
        grounding_refusals=Rate(refused, emits),
        tool_success=Rate(
            sum(s.tool_calls_ok for s in answered), sum(s.tool_calls for s in answered)
        ),
        calibration=calibrate([*modes, *severities]),
        retries={
            "accepted_first_try": sum(
                1 for s in scores if s.retry.outcome == "accepted" and s.answered
            ),
            "recovered": sum(1 for s in scores if s.retry.outcome == "recovered"),
            "exhausted": sum(1 for s in scores if s.retry.outcome == "exhausted"),
            "retry_calls": sum(s.retry.attempts - 1 for s in scores),
        },
        usage=usage,
    )
