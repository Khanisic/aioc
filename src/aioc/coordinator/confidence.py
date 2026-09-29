"""Field-level confidence (Day 18): every judgement in a response, with its band and what
the band claims that the value does not show.

The contract already makes confidence field-level: every analytic field is an
`Assessment` carrying its own ``confidence`` (CONTRACTS.md sec 2.1), the bands are
normative, and the two invariants that can be validated are - a value below the 0.25 floor
is refused, and a cited value at 0.50 or above must cite something. What nothing did until
Day 18 is *read* those numbers back as a whole: which fields a report is sure of, which it
is guessing at, and where the stated band promises more than the field shows. That is the
Domain 5 shore-up, and it is what the Day 19 eval harness scores calibration against.

This module is a reading of the frozen shapes, never a change to them. It walks every
`Assessment` in a response (`aioc.contracts.walk_assessments`, so a new analytic field is
picked up without an edit here) plus the Docs agent's `Claim`s, which carry a plain
``confidence`` and source documents rather than evidence ids, and produces one
`FieldConfidence` per judgement and one `ResponseProfile` per response. A
`CoordinatorResponse` gets a `RequestProfile`: every agent's profile plus the coordinator's
own two judgements, ``answer`` and ``intent``.

**The flags are the band table read literally.** The 0.90 band says "two or more
independent sources"; a field in it citing fewer than two evidence ids has claimed a band
its evidence does not show (`two_sources_band_under_cited`). Independence cannot be
checked mechanically, so this is a floor. ``overall_confidence`` is, by the contract, not a
mean of the fields - but an overall above every field it summarises is worth a reader's
attention (`overall_above_every_field`). Neither flag is a validation error: they are what
a calibration score is made of, and the contract's own list of anticipated changes is
where a stated rule becomes a validated one.

The band table is pinned by tests against the contract's text and the agents' prompts.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from aioc.contracts import (
    AgentResponse,
    Assessment,
    Claim,
    CoordinatorResponse,
    DocsFindings,
    walk_assessments,
)


class Band(StrEnum):
    """The contract's confidence bands (sec 2.1), lowest bound first in `BANDS`."""

    TWO_SOURCES = "two_sources"  # 0.90-1.00
    SINGLE_SOURCE = "single_source"  # 0.70-0.89
    INFERRED = "inferred"  # 0.50-0.69
    HYPOTHESIS = "hypothesis"  # 0.25-0.49
    SPECULATION = "speculation"  # below 0.25: value must be null, with a gap


# (lower bound inclusive, band, the contract's meaning verbatim). Upper bounds are the next
# entry's lower bound; the first entry runs to 1.00.
BANDS: tuple[tuple[float, Band, str], ...] = (
    (0.90, Band.TWO_SOURCES, "Directly evidenced by two or more independent sources"),
    (0.70, Band.SINGLE_SOURCE, "Directly evidenced by a single reliable source"),
    (0.50, Band.INFERRED, "Inferred from correlated signals; no direct statement"),
    (0.25, Band.HYPOTHESIS, "Plausible hypothesis, weak or partial evidence"),
    (0.00, Band.SPECULATION, "Speculation - do not state a conclusion; record it as a gap instead"),
)

# Minimum evidence the band's meaning states. Only the top band names a count; the
# single-source band's "one" is already the contract's validated >= 0.5 rule.
MIN_EVIDENCE: dict[Band, int] = {Band.TWO_SOURCES: 2}


def band(confidence: float) -> Band:
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence must be in [0, 1], got {confidence}")
    for lower, name, _ in BANDS:
        if confidence >= lower:
            return name
    return Band.SPECULATION  # pragma: no cover - the last bound is 0.0


def band_meaning(name: Band) -> str:
    return next(meaning for _, b, meaning in BANDS if b is name)


class Flag(StrEnum):
    TWO_SOURCES_BAND_UNDER_CITED = "two_sources_band_under_cited"
    OVERALL_ABOVE_EVERY_FIELD = "overall_above_every_field"
    # A Docs claim with no source at a confidence in a band that states a conclusion.
    # Found by the first run over recorded output: two "the corpus contains no document
    # about X" claims at 0.90 with sources=[] - a negative stated with two-source
    # confidence and nothing behind it. sec 4.2 keeps such a claim out of the answer;
    # the flag is what its confidence number is worth.
    UNSUPPORTED_CLAIM_ABOVE_FLOOR = "unsupported_claim_above_floor"


@dataclass(frozen=True, slots=True)
class FieldConfidence:
    """One judgement: an `Assessment` anywhere in the findings, or a Docs `Claim`."""

    path: str  # dotted path from the response root, e.g. findings.root_cause
    kind: str  # "assessment" | "claim"
    confidence: float
    band: Band
    null: bool  # an Assessment with value null; a Claim that is not supported
    evidence: tuple[str, ...]  # evidence ids, or a claim's source document ids
    flags: tuple[Flag, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "confidence": self.confidence,
            "band": self.band.value,
            "null": self.null,
            "evidence": list(self.evidence),
            "flags": [f.value for f in self.flags],
        }


@dataclass(frozen=True, slots=True)
class ResponseProfile:
    agent: str
    invocation_id: str
    overall_confidence: float
    overall_band: Band
    fields: tuple[FieldConfidence, ...]
    flags: tuple[Flag, ...]  # response-level

    @property
    def by_band(self) -> dict[Band, int]:
        counts = Counter(f.band for f in self.fields)
        return {b: counts.get(b, 0) for _, b, _ in BANDS}

    @property
    def nulls(self) -> tuple[FieldConfidence, ...]:
        return tuple(f for f in self.fields if f.null)

    @property
    def stated(self) -> tuple[FieldConfidence, ...]:
        """The judgements that state a value - what a reader can be right or wrong about."""
        return tuple(f for f in self.fields if not f.null)

    @property
    def lowest(self) -> FieldConfidence | None:
        return min(self.stated, key=lambda f: f.confidence, default=None)

    @property
    def highest(self) -> FieldConfidence | None:
        return max(self.stated, key=lambda f: f.confidence, default=None)

    @property
    def flagged(self) -> tuple[FieldConfidence, ...]:
        return tuple(f for f in self.fields if f.flags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "invocation_id": self.invocation_id,
            "overall_confidence": self.overall_confidence,
            "overall_band": self.overall_band.value,
            "judgements": len(self.fields),
            "nulls": len(self.nulls),
            "by_band": {b.value: n for b, n in self.by_band.items()},
            "flags": [f.value for f in self.flags],
            "fields": [f.to_dict() for f in self.fields],
        }

    def render(self) -> list[str]:
        head = (
            f"{self.agent} {self.invocation_id}: overall {self.overall_confidence:.2f} "
            f"[{self.overall_band.value}], {len(self.fields)} judgement(s), "
            f"{len(self.nulls)} null"
        )
        if self.flags:
            head += "; " + ", ".join(f.value for f in self.flags)
        lines = [head]
        for f in self.fields:
            shown = "null" if f.null else f"@{f.confidence:.2f}"
            cited = f" [{', '.join(f.evidence)}]" if f.evidence else ""
            flags = f"  <- {', '.join(x.value for x in f.flags)}" if f.flags else ""
            lines.append(f"  {f.path}: {shown} {f.band.value}{cited}{flags}")
        return lines


@dataclass(frozen=True, slots=True)
class RequestProfile:
    request_id: str
    agents: tuple[ResponseProfile, ...]
    answer: FieldConfidence
    intent: FieldConfidence

    @property
    def fields(self) -> tuple[FieldConfidence, ...]:
        return tuple(f for p in self.agents for f in p.fields) + (self.answer, self.intent)

    @property
    def flagged(self) -> tuple[FieldConfidence, ...]:
        return tuple(f for f in self.fields if f.flags)

    @property
    def by_band(self) -> dict[Band, int]:
        counts = Counter(f.band for f in self.fields)
        return {b: counts.get(b, 0) for _, b, _ in BANDS}

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "answer": self.answer.to_dict(),
            "intent": self.intent.to_dict(),
            "by_band": {b.value: n for b, n in self.by_band.items()},
            "agents": [p.to_dict() for p in self.agents],
        }

    def render(self) -> list[str]:
        lines = [
            f"request {self.request_id}: {len(self.fields)} judgement(s), "
            + ", ".join(f"{n} {b.value}" for b, n in self.by_band.items() if n),
            f"  answer: {_shown(self.answer)}",
            f"  intent: {_shown(self.intent)}",
        ]
        for p in self.agents:
            lines.extend(p.render())
        return lines


# -------------------------------------------------------------------------- the reading


def profile(response: AgentResponse) -> ResponseProfile:
    """Every judgement in one agent response, and the response-level flags."""
    fields = [_field(path, a) for path, a in walk_assessments(response.findings, "findings")]
    if isinstance(response.findings, DocsFindings):
        fields.extend(
            _claim(f"findings.claims[{i}]", c) for i, c in enumerate(response.findings.claims)
        )
    flags: list[Flag] = []
    stated = [f for f in fields if not f.null]
    if stated and response.overall_confidence > max(f.confidence for f in stated):
        flags.append(Flag.OVERALL_ABOVE_EVERY_FIELD)
    return ResponseProfile(
        agent=response.agent.value,
        invocation_id=response.invocation_id,
        overall_confidence=response.overall_confidence,
        overall_band=band(response.overall_confidence),
        fields=tuple(fields),
        flags=tuple(flags),
    )


def request_profile(response: CoordinatorResponse) -> RequestProfile:
    """Every agent's profile plus the coordinator's own two judgements. ``intent`` is the
    contract's one evidence-exempt Assessment, so it is never flagged for citations."""
    return RequestProfile(
        request_id=response.request_id,
        agents=tuple(profile(r) for r in response.agent_responses),
        answer=_field("answer", response.answer),
        intent=_field("intent", response.intent, evidence_exempt=True),
    )


def digest_line(response: AgentResponse) -> str:
    """One line for the handoff digest: how sure the report is, field by field, in the
    words a dependent can act on - the lowest stated judgement is what it should not build
    on, and a flag is a band the evidence does not show."""
    p = profile(response)
    parts = [f"{len(p.fields)} judgement(s), {len(p.nulls)} null"]
    if p.lowest is not None:
        parts.append(f"lowest {p.lowest.path} @{p.lowest.confidence:.2f} [{p.lowest.band.value}]")
    if p.highest is not None and p.highest is not p.lowest:
        parts.append(
            f"highest {p.highest.path} @{p.highest.confidence:.2f} [{p.highest.band.value}]"
        )
    flagged = [f"{f.path} ({', '.join(x.value for x in f.flags)})" for f in p.flagged]
    flagged.extend(x.value for x in p.flags)
    if flagged:
        parts.append("flagged: " + "; ".join(flagged))
    return "confidence: " + "; ".join(parts)


# ------------------------------------------------------------------------------ helpers


def _field(path: str, a: Assessment[Any], *, evidence_exempt: bool = False) -> FieldConfidence:
    b = band(a.confidence)
    flags = _flags(b, a.value is None, len(a.evidence), evidence_exempt)
    return FieldConfidence(
        path=path,
        kind="assessment",
        confidence=a.confidence,
        band=b,
        null=a.value is None,
        evidence=tuple(a.evidence),
        flags=flags,
    )


def _claim(path: str, c: Claim) -> FieldConfidence:
    b = band(c.confidence)
    docs = tuple(dict.fromkeys(s.document_id for s in c.sources))
    if c.supported:
        flags = _flags(b, False, len(docs), False)
    else:
        flags = (Flag.UNSUPPORTED_CLAIM_ABOVE_FLOOR,) if b is not Band.SPECULATION else ()
    return FieldConfidence(
        path=path,
        kind="claim",
        confidence=c.confidence,
        band=b,
        null=not c.supported,
        evidence=docs,
        flags=flags,
    )


def _flags(b: Band, null: bool, cited: int, exempt: bool) -> tuple[Flag, ...]:
    if null or exempt:
        return ()
    needed = MIN_EVIDENCE.get(b, 0)
    return (Flag.TWO_SOURCES_BAND_UNDER_CITED,) if cited < needed else ()


def _shown(f: FieldConfidence) -> str:
    shown = "null" if f.null else f"@{f.confidence:.2f}"
    cited = f" [{', '.join(f.evidence)}]" if f.evidence else ""
    flags = f"  <- {', '.join(x.value for x in f.flags)}" if f.flags else ""
    return f"{shown} {f.band.value}{cited}{flags}"


def render_bands() -> Sequence[str]:
    """The band table as the agents are prompted with it, for anything that prints it."""
    return [f"{lower:.2f}+ {b.value}: {meaning}" for lower, b, meaning in BANDS]
