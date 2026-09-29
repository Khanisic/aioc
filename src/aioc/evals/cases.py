"""The eval set (Day 19): cases drawn from the seeded incidents, and what each agent is shown.

A case file **selects, it never authors**. Every line an agent under evaluation reads
about an incident is verbatim from a seeded row - a sentence of its summary, a timeline
event, an impact metric - so the eval data cannot drift from the corpus, and "did the
answer leak into the question" is a string comparison rather than a judgement
(`check_leaks`, enforced at load). The one authored thing is the recall question, which is
a question and carries no answer.

Each seeded incident gives two tasks:

- **diagnose** (Incident agent): the signals an on-call engineer would have had while the
  incident was open - the symptoms, the metrics, the events up to the point somebody
  worked out the cause. The title, the root cause, the resolution, the events that record
  the finding and the fix, and every recorded severity are withheld; the failure mode and
  the severity are then scored against the row's ``true_*`` columns.
- **recall** (Docs agent): an on-call question about the same symptoms. The incident's own
  post-mortem is in the corpus, so the score is whether retrieval found it and the answer
  cited it.

A case with no incident is a **no-precedent probe**: a recall question about something the
corpus has never recorded. The only correct answer is that nothing was found, which makes
it the direct measurement of whether the Docs agent invents a precedent.

The case file format is not frozen (CLAUDE.md: eval record formats churn freely); the
answer key is, because it is the seed.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from aioc.contracts import FailureMode

from .seed import SeedEvent, SeedIncident, seed_by_id

CASES_DIR = Path(__file__).resolve().parents[3] / "evaluations" / "cases"
DEFAULT_SET = CASES_DIR / "seeded-incidents.json"

# A mode scored on one case is a coin flip; the corpus test holds the seed to the same floor.
MIN_CASES_PER_MODE = 2


class CaseError(ValueError):
    """The case file is malformed, points outside the seed, or leaks an answer."""


class Task(StrEnum):
    DIAGNOSE = "diagnose"
    RECALL = "recall"


# ---------------------------------------------------------------------- the file's shape


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SignalSelection(_Model):
    """Which of the incident's recorded lines the Incident agent is shown."""

    summary_sentences: list[int] = Field(default_factory=list)
    events: list[str] = Field(default_factory=list)


class CaseSpec(_Model):
    id: str = Field(pattern=r"^case_[a-z0-9_]+$")
    incident_id: str | None = None
    signals: SignalSelection | None = None
    recall_query: str | None = None
    note: str | None = None


class CaseFile(_Model):
    set: str
    version: int
    description: str
    diagnose_query: str
    recall_context: str
    cases: list[CaseSpec]


# ------------------------------------------------------------------------ what is scored


@dataclass(frozen=True, slots=True)
class EvalItem:
    """One request to one agent: the unit that is run, scored, and priced."""

    case_id: str
    task: Task
    agent: str
    query: str
    context: str
    incident: SeedIncident | None  # None: a no-precedent probe

    @property
    def key(self) -> str:
        return f"{self.case_id}:{self.task.value}"

    @property
    def expected_document(self) -> str | None:
        return None if self.incident is None else self.incident.document_id


@dataclass(frozen=True, slots=True)
class EvalSet:
    name: str
    version: int
    description: str
    path: Path
    sha256: str  # of the case file: two runs are comparable only on the same set
    items: tuple[EvalItem, ...]

    def select(
        self, *, tasks: set[Task] | None = None, cases: set[str] | None = None
    ) -> tuple[EvalItem, ...]:
        return tuple(
            item
            for item in self.items
            if (tasks is None or item.task in tasks) and (cases is None or item.case_id in cases)
        )


# ---------------------------------------------------------------------------- rendering

_SENTENCE = re.compile(r"(?<=[.!?])\s+")

_METRICS = (
    ("error rate", "error_rate_before", "error_rate_after", ""),
    ("p50 latency", "p50_latency_ms_before", "p50_latency_ms_after", " ms"),
    ("p99 latency", "p99_latency_ms_before", "p99_latency_ms_after", " ms"),
)


def sentences(summary: str) -> list[str]:
    """A summary split on sentence boundaries - the unit a case selects by index."""
    return [part for part in _SENTENCE.split(summary.strip()) if part]


def _stamp(event: SeedEvent) -> str:
    return event.at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _event_line(event: SeedEvent) -> str:
    # The recorded severity is left out on purpose: an alert tagged `sev2` makes the
    # severity a transcription, and the eval is scoring a judgement.
    kind = event.kind if event.kind_detail is None else f"{event.kind}: {event.kind_detail}"
    return f"- {_stamp(event)} {event.service} [{kind}] {event.description} ({event.id})"


def _metric(value: float | int | None, unit: str) -> str:
    return "not measured" if value is None else f"{value}{unit}"


def render_signals(incident: SeedIncident, selection: SignalSelection) -> str:
    """The Incident agent's context block for one case: only selected, verbatim lines."""
    parts = sentences(incident.summary)
    events = {event.id: event for event in incident.events}
    lines = [
        "Signals for an incident that is in progress. Times are UTC.",
        "",
        "Reported symptoms:",
    ]
    shown = [parts[i] for i in selection.summary_sentences]
    lines.extend(f"- {sentence}" for sentence in shown)
    if not shown:
        lines.append("- (none recorded)")
    lines.extend(["", "Metrics (before the incident -> during it):"])
    for label, before, after, unit in _METRICS:
        lines.append(
            f"- {label}: {_metric(incident.impact[before], unit)} -> "
            f"{_metric(incident.impact[after], unit)}"
        )
    lines.append(f"- requests affected: {_metric(incident.impact['requests_affected'], '')}")
    lines.extend(["", "Events, oldest first:"])
    chosen = sorted((events[event_id] for event_id in selection.events), key=lambda e: e.at)
    lines.extend(_event_line(event) for event in chosen)
    if not chosen:
        lines.append("- (none recorded)")
    return "\n".join(lines)


# ------------------------------------------------------------------------- the leak guard


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def check_leaks(context: str, incident: SeedIncident, selection: SignalSelection) -> None:
    """Refuse a diagnose context that carries any part of its own answer.

    Enforced at load rather than reviewed, for the reason `chaos_knob_value` is excluded
    in code (`aioc.observability.prometheus`): a leaked answer does not fail an eval, it
    passes it, and nothing downstream would ever notice.
    """
    flat = _flat(context)
    parts = sentences(incident.summary)
    withheld: dict[str, str] = {
        "the incident title": incident.title,
        "the root cause": incident.true_root_cause,
        "the resolution": incident.resolution,
        "the incident id": incident.id,
    }
    # `other` is an English word and appears in honest signals; the named modes are not.
    if incident.true_failure_mode != "other":
        withheld["the failure mode"] = incident.true_failure_mode
    if incident.true_failure_mode_detail is not None:
        withheld["the failure mode detail"] = incident.true_failure_mode_detail
    for index, sentence in enumerate(parts):
        if index not in selection.summary_sentences:
            withheld[f"withheld summary sentence {index}"] = sentence
    for event in incident.events:
        if event.id not in selection.events:
            withheld[f"withheld event {event.id}"] = event.description
    for what, text in withheld.items():
        if _flat(text) in flat:
            raise CaseError(f"{incident.id}: the diagnose context contains {what}")
    if re.search(r"\bsev[1-4]\b", flat):
        raise CaseError(f"{incident.id}: the diagnose context names a severity level")


# ------------------------------------------------------------------------------ loading


def _items(spec: CaseSpec, file: CaseFile, seed: dict[str, SeedIncident]) -> list[EvalItem]:
    if spec.incident_id is None:
        if spec.signals is not None:
            raise CaseError(f"{spec.id}: signals need an incident to select from")
        if not spec.recall_query:
            raise CaseError(f"{spec.id}: a case with no incident is a recall probe and needs one")
        return [
            EvalItem(spec.id, Task.RECALL, "docs", spec.recall_query, file.recall_context, None)
        ]

    incident = seed.get(spec.incident_id)
    if incident is None:
        raise CaseError(f"{spec.id}: {spec.incident_id} is not a seeded incident")
    items: list[EvalItem] = []
    if spec.signals is not None:
        selection = spec.signals
        count = len(sentences(incident.summary))
        bad = [i for i in selection.summary_sentences if not 0 <= i < count]
        if bad:
            raise CaseError(f"{spec.id}: {incident.id} has no summary sentence {bad}")
        known = {event.id for event in incident.events}
        foreign = [event_id for event_id in selection.events if event_id not in known]
        if foreign:
            raise CaseError(f"{spec.id}: {foreign} are not events of {incident.id}")
        if not selection.summary_sentences and not selection.events:
            raise CaseError(f"{spec.id}: a diagnose task with no signals cannot be answered")
        context = render_signals(incident, selection)
        check_leaks(context, incident, selection)
        items.append(
            EvalItem(spec.id, Task.DIAGNOSE, "incident", file.diagnose_query, context, incident)
        )
    if spec.recall_query:
        items.append(
            EvalItem(spec.id, Task.RECALL, "docs", spec.recall_query, file.recall_context, incident)
        )
    if not items:
        raise CaseError(f"{spec.id}: no task - give it signals, a recall_query, or both")
    return items


def load_cases(path: Path | None = None) -> EvalSet:
    """Read, validate, and render a case file. Raises `CaseError` on anything that would
    make a score meaningless: a dangling reference, a leaked answer, a mode too thin to
    score."""
    path = path or DEFAULT_SET
    raw = path.read_bytes()
    try:
        file = CaseFile.model_validate(json.loads(raw))
    except ValueError as exc:  # pydantic's ValidationError and json's error are both ValueError
        raise CaseError(f"{path.name}: {exc}") from exc

    ids = Counter(spec.id for spec in file.cases)
    repeated = sorted(case_id for case_id, n in ids.items() if n > 1)
    if repeated:
        raise CaseError(f"{path.name}: duplicate case ids {repeated}")
    incidents = Counter(spec.incident_id for spec in file.cases if spec.incident_id)
    reused = sorted(incident_id for incident_id, n in incidents.items() if n > 1)
    if reused:
        raise CaseError(f"{path.name}: incidents used by more than one case {reused}")

    seed = seed_by_id()
    items: list[EvalItem] = []
    for spec in file.cases:
        items.extend(_items(spec, file, seed))

    modes = Counter(
        item.incident.true_failure_mode
        for item in items
        if item.task is Task.DIAGNOSE and item.incident is not None
    )
    thin = {m.value: modes.get(m.value, 0) for m in FailureMode}
    thin = {mode: n for mode, n in thin.items() if n < MIN_CASES_PER_MODE}
    if thin:
        raise CaseError(f"{path.name}: too few diagnose cases to score a failure mode: {thin}")

    return EvalSet(
        name=file.set,
        version=file.version,
        description=file.description,
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        items=tuple(items),
    )
