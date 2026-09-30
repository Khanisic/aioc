"""Running the eval set (Day 19): the same cases, realtime or through a batch.

The runner calls the agents the way the executor does - `IncidentAgent.diagnose` and
`DocsAgent.answer`, with an explicit context block and nothing inherited - so what is
scored is the shipped code path, prompt, schema, retry loop and all. Only the client
differs between the two modes:

- **realtime** - an `LLMClient`, one item after another. Sequential on purpose: the first
  request writes the prompt cache and the rest read it, where N concurrent requests would
  each pay for a write none of them can read.
- **batch** - a `DeferredClient`. Every item is run; the ones whose next model call has no
  answer yet raise `PendingRequest`; the queue goes out as one batch; and the waiting items
  are run again. An item needs one pass per model call it makes, so a pass count of
  ``max_validation_retries + 1`` is the most there can be.

Every item gets its own `Usage` and its own `RetryLog`, so tokens and retries are
attributed to the case that spent them. Retrieval is memoised per question: the batch
path replays an item once per pass and must rebuild the same request each time, and the
realtime path should not pay Voyage twice for one question either.

**An agent that raised is a scored item; a call the environment refused is not.** A report
the agent gave up on is a result of the thing under test. A rejected API key, an account
with no credit, or an API that is down is a fact about the environment that every
remaining item would repeat, so the run stops (`EvalAborted`) instead of recording it
thirty-eight times as the agents' failure. Both of the first two live runs of this harness
did exactly that: one on a revoked key, one on an empty balance five items in.

Two rules, because the failures that matter cannot all be listed in advance. A refusal
that is known to be about the caller (the key, the permission, the balance) stops the run
at once. Anything else the API itself fails on is given the benefit of the doubt, item by
item, until `MAX_ENVIRONMENT_FAILURES` in a row have failed that way - one overloaded
response is weather, three in a row is the climate.

**What was paid for is kept.** Every finished item is handed to ``on_result`` as it
finishes, and a run can be given the items it has already ``done``. So a run that is
stopped - by the rule above, by a closed terminal, by anything - costs what it had not
yet done, and nothing twice.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

import anthropic

from aioc.agents import DocsAgent, IncidentAgent, RetryLog
from aioc.agents.docs import DEFAULT_TOP_K, CorpusRetriever
from aioc.contracts import AgentResponse
from aioc.llm import (
    BatchError,
    BatchRun,
    DeferredClient,
    LLMClient,
    LLMSettings,
    MessageBatcher,
    PendingRequest,
    Usage,
)
from aioc.retrieval import RetrievalResult

from .cases import EvalItem, Task
from .scoring import ItemScore, RetryStats, is_environment_error, score_failure, score_item

Mode = Literal["realtime", "batch"]
Progress = Callable[[str], None]

# How many items in a row may fail on the API itself before the run stops.
MAX_ENVIRONMENT_FAILURES = 3

# Refusals that are about the caller, not the request: no item can succeed after one.
_ABOUT_THE_CALLER = (anthropic.AuthenticationError, anthropic.PermissionDeniedError)


class EvalAborted(RuntimeError):  # noqa: N818 - names what happened to the run
    """The run cannot continue, and that is no item's result. ``completed`` is how many
    items had been scored when it stopped; ``results`` is those items, which were paid
    for and can be given to the next run as ``done``."""

    def __init__(self, message: str, *, completed: int, results: Sequence[ItemResult] = ()) -> None:
        super().__init__(message)
        self.completed = completed
        self.results = list(results)


def stops_the_run(exc: BaseException) -> str | None:
    """Why this failure means no later item can succeed, or ``None`` when it does not
    say so on its own."""
    if isinstance(exc, _ABOUT_THE_CALLER):
        return (
            "the API refused the credential. Nothing more can succeed until the key is "
            "replaced - see HANDOFF.md sec 7 item 10"
        )
    if is_environment_error(exc) and "credit balance" in str(exc).lower():
        return (
            "the account has no credit left. Nothing more can succeed until credit is "
            "added in the Console (Plans & Billing); the run can then be resumed"
        )
    return None


def _aborted(
    exc: BaseException, why: str, *, at: str, results: Sequence[ItemResult]
) -> EvalAborted:
    return EvalAborted(
        f"stopped at {at}: {why} ({type(exc).__name__}: {' '.join(str(exc).split())}). "
        f"{len(results)} item(s) had been scored and are kept.",
        completed=len(results),
        results=results,
    )


class _EnvironmentFailure(Exception):
    """An item the environment failed, carried up to the loop that counts them."""

    def __init__(self, cause: BaseException, result: ItemResult) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.result = result


class RecordingRetriever:
    """Retrieval, remembered. One search per distinct question, and the result kept so a
    recall can be scored on whether retrieval found the expected document at all."""

    def __init__(self, inner: CorpusRetriever) -> None:
        self._inner = inner
        self._seen: dict[tuple[str, int], RetrievalResult] = {}

    def search(self, query: str, *, k: int = DEFAULT_TOP_K) -> RetrievalResult:
        key = (query, k)
        if key not in self._seen:
            self._seen[key] = self._inner.search(query, k=k)
        return self._seen[key]

    def retrieved(self, query: str, *, k: int = DEFAULT_TOP_K) -> tuple[str, ...] | None:
        result = self._seen.get((query.strip(), k))
        return None if result is None else tuple(doc.doc_id for doc in result.docs)

    def degraded(self) -> list[str]:
        """Why the vector half was unavailable, for every search where it was."""
        return sorted({r.degraded for r in self._seen.values() if r.degraded})


@dataclass(frozen=True, slots=True)
class ItemResult:
    item: EvalItem
    score: ItemScore
    response: AgentResponse | None
    # What retrieval returned for a recall, kept so a recorded run can be scored again.
    retrieved: tuple[str, ...] | None = None


@dataclass(slots=True)
class EvalRun:
    mode: Mode
    model: str
    prompt_caching: bool
    cache_ttl: Literal["5m", "1h"]
    results: list[ItemResult] = field(default_factory=list)
    batches: list[BatchRun] = field(default_factory=list)
    retrieval_degraded: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def scores(self) -> list[ItemScore]:
        return [result.score for result in self.results]


def _ids(item: EvalItem) -> tuple[str, str]:
    # Stable ids: they are in no prompt, but a recorded response should name its case.
    return f"req_{item.case_id}", f"inv_{item.case_id}_{item.task.value}"


def run_item(
    item: EvalItem,
    client: LLMClient,
    retriever: CorpusRetriever | None,
    *,
    usage: Usage,
    retry_log: RetryLog,
) -> AgentResponse:
    """One item through its agent, exactly as the executor would call it."""
    request_id, invocation_id = _ids(item)
    if item.task is Task.DIAGNOSE:
        return IncidentAgent(client, retry_log=retry_log).diagnose(
            item.query,
            context=item.context,
            request_id=request_id,
            invocation_id=invocation_id,
            usage=usage,
        )
    if retriever is None:
        raise ValueError(f"{item.key}: a recall task needs a retriever")
    return DocsAgent(client, retriever, retry_log=retry_log).answer(
        item.query,
        context=item.context,
        request_id=request_id,
        invocation_id=invocation_id,
        usage=usage,
    )


def _retry_stats(log: RetryLog) -> RetryStats:
    records = log.records
    if not records:
        return RetryStats()
    record = records[-1]
    return RetryStats(
        attempts=record.attempts,
        rejections=tuple(kind.value for kind in record.kinds),
        outcome=record.outcome,
    )


def _attempt(
    item: EvalItem,
    client: LLMClient,
    retriever: RecordingRetriever | None,
) -> ItemResult:
    """Run and score one item. `PendingRequest` passes through - it is the batch path's
    signal that this item is waiting, not a result."""
    usage, log = Usage(), RetryLog()
    started = time.monotonic()
    response: AgentResponse | None
    retrieved: tuple[str, ...] | None = None
    failure: BaseException | None = None
    try:
        response = run_item(item, client, retriever, usage=usage, retry_log=log)
    except PendingRequest:
        raise
    except Exception as exc:  # noqa: BLE001 - an agent that raised is a scored outcome
        response, score, failure = None, score_failure(item, exc), exc
    else:
        if item.task is Task.RECALL and retriever is not None:
            retrieved = retriever.retrieved(item.query)
        score = score_item(item, response, retrieved=retrieved)
    score = replace(score, usage=usage, retry=_retry_stats(log), seconds=time.monotonic() - started)
    result = ItemResult(item=item, score=score, response=response, retrieved=retrieved)
    if failure is not None and is_environment_error(failure):
        raise _EnvironmentFailure(failure, result)
    return result


def _new_run(mode: Mode, settings: LLMSettings) -> EvalRun:
    return EvalRun(
        mode=mode,
        model=settings.model,
        prompt_caching=settings.prompt_caching,
        cache_ttl=settings.prompt_cache_ttl,
    )


def _say(progress: Progress | None, result: ItemResult) -> None:
    if progress is None:
        return
    score = result.score
    if not score.answered:
        verdict = f"FAILED  {score.error}"
    else:
        verdict = "correct" if score.correct else "wrong  "
    progress(f"  {score.key:<20} {verdict}")


class _Tally:
    """The results of a run as they arrive, and the count of environment failures in a row.

    A failure the environment caused is held back rather than recorded: if the next item
    succeeds it was weather and is recorded as the failed item it was; if the run stops,
    it is dropped, so a resumed run asks again instead of inheriting it.
    """

    def __init__(
        self,
        done: Mapping[str, ItemResult] | None,
        progress: Progress | None,
        on_result: Callable[[ItemResult], None] | None,
    ) -> None:
        self.results: dict[str, ItemResult] = dict(done or {})
        self._held: list[ItemResult] = []
        self._progress = progress
        self._on_result = on_result

    def has(self, item: EvalItem) -> bool:
        return item.key in self.results

    def _record(self, result: ItemResult) -> None:
        self.results[result.item.key] = result
        _say(self._progress, result)
        if self._on_result is not None:
            self._on_result(result)

    def add(self, result: ItemResult) -> None:
        for held in self._held:
            self._record(held)
        self._held = []
        self._record(result)

    def failed(self, failure: _EnvironmentFailure, *, at: str) -> None:
        """Count one environment failure, and stop the run if it says to."""
        why = stops_the_run(failure.cause)
        self._held.append(failure.result)
        if why is None and len(self._held) >= MAX_ENVIRONMENT_FAILURES:
            why = (
                f"{len(self._held)} items in a row failed on the API itself, so the next would too"
            )
        if why is not None:
            raise _aborted(failure.cause, why, at=at, results=self.ordered()) from failure.cause

    def finish(self) -> None:
        """The run ended with failures still held: they were the last items, and stand."""
        for held in self._held:
            self._record(held)
        self._held = []

    def ordered(self, items: Sequence[EvalItem] | None = None) -> list[ItemResult]:
        if items is None:
            return list(self.results.values())
        return [self.results[item.key] for item in items if item.key in self.results]


def run_realtime(
    items: Sequence[EvalItem],
    client: LLMClient,
    retriever: CorpusRetriever | None = None,
    *,
    progress: Progress | None = None,
    done: Mapping[str, ItemResult] | None = None,
    on_result: Callable[[ItemResult], None] | None = None,
) -> EvalRun:
    """``done`` is the items a stopped run already finished (by key): they are not run
    again. ``on_result`` is called with each item as it finishes."""
    run = _new_run("realtime", client.settings)
    recording = None if retriever is None else RecordingRetriever(retriever)
    tally = _Tally(done, progress, on_result)
    started = time.monotonic()
    for item in items:
        if tally.has(item):
            continue
        try:
            tally.add(_attempt(item, client, recording))
        except _EnvironmentFailure as failure:
            tally.failed(failure, at=item.key)
    tally.finish()
    run.results = tally.ordered(items)
    # A realtime run is one item after another, so the items kept from an earlier run
    # took their own recorded time; a continued run's clock is the whole of it.
    kept = sum(result.score.seconds or 0.0 for result in (done or {}).values())
    run.seconds = time.monotonic() - started + kept
    if recording is not None:
        run.retrieval_degraded = recording.degraded()
    return run


def run_batch(
    items: Sequence[EvalItem],
    client: DeferredClient,
    batcher: MessageBatcher,
    retriever: CorpusRetriever | None = None,
    *,
    progress: Progress | None = None,
    done: Mapping[str, ItemResult] | None = None,
    on_result: Callable[[ItemResult], None] | None = None,
    submitted: Sequence[str] = (),
    on_submit: Callable[[str, int], None] | None = None,
) -> EvalRun:
    """``submitted`` names batches an earlier run of these items sent and never read
    back; their answers are fetched before anything is submitted, so a run that died
    waiting on a batch pays for it once. ``on_submit`` is told each new batch's id."""
    run = _new_run("batch", client.settings)
    recording = None if retriever is None else RecordingRetriever(retriever)
    tally = _Tally(done, progress, on_result)
    started = time.monotonic()
    waiting = [item for item in items if not tally.has(item)]
    for batch_id in submitted:
        if progress is not None:
            progress(f"  reading batch {batch_id}, submitted by an earlier run")
        try:
            earlier = client.attach(batcher, batch_id)
        except (anthropic.APIError, BatchError) as exc:
            why = stops_the_run(exc) or "an earlier batch could not be read back"
            raise _aborted(exc, why, at=f"batch {batch_id}", results=tally.ordered()) from exc
        if progress is not None:
            progress(
                f"  batch {batch_id}: {earlier.succeeded} answer(s) kept, {earlier.failed} failed"
            )
    # One pass per model call an item can make: the first attempt, then each retry.
    passes = client.settings.max_validation_retries + 1
    for number in range(1, passes + 2):
        still: list[EvalItem] = []
        for item in waiting:
            try:
                tally.add(_unclocked(_attempt(item, client, recording)))
            except PendingRequest:
                still.append(item)
            except _EnvironmentFailure as failure:
                failure.result = _unclocked(failure.result)
                tally.failed(failure, at=item.key)
        waiting = still
        if not waiting:
            break
        if number > passes:
            # Cannot happen while the retry cap holds; refuse to loop on it if it does.
            for item in waiting:
                error = RuntimeError(f"still pending after {passes} passes")
                tally.add(ItemResult(item, score_failure(item, error), None))
            break
        if progress is not None:
            progress(f"  pass {number}: {len(client.pending)} request(s) submitted as one batch")
        try:
            batch = client.flush(batcher, on_submit=on_submit)
        except (anthropic.APIError, BatchError) as exc:
            # The batch is how every waiting item is asked: if it cannot be sent or
            # never ends, none of them can be answered.
            why = stops_the_run(exc) or "the batch could not be run"
            raise _aborted(
                exc, why, at=f"batch submission {number}", results=tally.ordered()
            ) from exc
        if progress is not None and batch is not None:
            progress(
                f"  pass {number}: batch {batch.batch_id} ended in {batch.seconds:.0f}s, "
                f"{batch.succeeded} succeeded, {batch.failed} failed"
            )
    tally.finish()
    run.results = tally.ordered(items)
    run.batches = list(client.runs)
    run.seconds = time.monotonic() - started
    if recording is not None:
        run.retrieval_degraded = recording.degraded()
    return run


def _unclocked(result: ItemResult) -> ItemResult:
    # A batched item's own clock measures replay, not the model: the batches carry the time.
    return replace(result, score=replace(result.score, seconds=None))
