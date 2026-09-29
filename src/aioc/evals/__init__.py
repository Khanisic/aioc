"""The eval harness (Day 19, Reasoning Layer): cases, a runner, scoring, a report.

Four steps, each usable without the one after it:

- `aioc.evals.seed` reads the seeded incident corpus from its SQL - the answer key.
- `aioc.evals.cases` loads a case file (`evaluations/cases/`) into `EvalItem`s: what each
  agent is shown, selected verbatim from the seed, with a leak guard enforced at load.
- `aioc.evals.runner` runs the items through the shipped agents, realtime or as a batch.
- `aioc.evals.scoring` scores a response against the seed (accuracy, hallucination rate,
  tool success rate, calibration); `aioc.evals.report` renders and prices a run.

`scripts/run_evals.py` is the entry point. Scoring is pure, so everything but the model
calls themselves is covered offline.
"""

from __future__ import annotations

from .cases import (
    DEFAULT_SET,
    CaseError,
    EvalItem,
    EvalSet,
    Task,
    check_leaks,
    load_cases,
    render_signals,
    sentences,
)
from .report import CostView, cost_view, render_markdown, to_record
from .runner import (
    EvalAborted,
    EvalRun,
    ItemResult,
    RecordingRetriever,
    run_batch,
    run_item,
    run_realtime,
)
from .scoring import (
    BandCalibration,
    Grounding,
    ItemScore,
    Judgement,
    Rate,
    RetryStats,
    Summary,
    calibrate,
    cited_documents,
    ground_diagnosis,
    score_diagnosis,
    score_failure,
    score_item,
    score_recall,
    summarise,
    tool_success,
)
from .seed import SEED_PATH, SeedError, SeedEvent, SeedIncident, load_seed, parse_seed, seed_by_id

__all__ = [
    "DEFAULT_SET",
    "SEED_PATH",
    "BandCalibration",
    "CaseError",
    "CostView",
    "EvalAborted",
    "EvalItem",
    "EvalRun",
    "EvalSet",
    "Grounding",
    "ItemResult",
    "ItemScore",
    "Judgement",
    "Rate",
    "RecordingRetriever",
    "RetryStats",
    "SeedError",
    "SeedEvent",
    "SeedIncident",
    "Summary",
    "Task",
    "calibrate",
    "check_leaks",
    "cited_documents",
    "cost_view",
    "ground_diagnosis",
    "load_cases",
    "load_seed",
    "parse_seed",
    "render_markdown",
    "render_signals",
    "run_batch",
    "run_item",
    "run_realtime",
    "score_diagnosis",
    "score_failure",
    "score_item",
    "score_recall",
    "seed_by_id",
    "sentences",
    "summarise",
    "to_record",
    "tool_success",
]
