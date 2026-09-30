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

from .baseline import (
    REFERENCE,
    BaselineError,
    build_baseline,
    compare,
    render_baseline,
    render_comparison,
)
from .baseline import label as run_label
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
from .report import CacheHealth, CostView, cache_health, cost_view, render_markdown, to_record
from .runner import (
    MAX_ENVIRONMENT_FAILURES,
    EvalAborted,
    EvalRun,
    ItemResult,
    RecordingRetriever,
    run_batch,
    run_item,
    run_realtime,
    stops_the_run,
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
    is_environment_error,
    quoted,
    score_diagnosis,
    score_failure,
    score_item,
    score_recall,
    summarise,
    tool_success,
)
from .seed import SEED_PATH, SeedError, SeedEvent, SeedIncident, load_seed, parse_seed, seed_by_id
from .store import (
    BATCHES,
    PROGRESS,
    ProgressFile,
    RunConfig,
    StoredRun,
    StoreError,
    failure_kind,
    read_run,
    restore,
    submitted_batches,
)

__all__ = [
    "DEFAULT_SET",
    "BATCHES",
    "PROGRESS",
    "MAX_ENVIRONMENT_FAILURES",
    "REFERENCE",
    "SEED_PATH",
    "BandCalibration",
    "BaselineError",
    "CacheHealth",
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
    "ProgressFile",
    "Rate",
    "RecordingRetriever",
    "RetryStats",
    "RunConfig",
    "SeedError",
    "SeedEvent",
    "SeedIncident",
    "StoreError",
    "StoredRun",
    "Summary",
    "Task",
    "build_baseline",
    "cache_health",
    "calibrate",
    "check_leaks",
    "cited_documents",
    "compare",
    "cost_view",
    "failure_kind",
    "ground_diagnosis",
    "is_environment_error",
    "load_cases",
    "load_seed",
    "parse_seed",
    "read_run",
    "quoted",
    "render_baseline",
    "render_comparison",
    "render_markdown",
    "render_signals",
    "restore",
    "run_batch",
    "run_item",
    "run_label",
    "run_realtime",
    "score_diagnosis",
    "score_failure",
    "score_item",
    "score_recall",
    "seed_by_id",
    "sentences",
    "stops_the_run",
    "submitted_batches",
    "summarise",
    "to_record",
    "tool_success",
]
