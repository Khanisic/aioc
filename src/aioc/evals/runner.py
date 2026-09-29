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

**An agent that raised is a scored item; a credential that was refused is not.** A report
the agent gave up on is a result of the thing under test. A rejected API key is a fact
about the environment that every remaining item would repeat, so the run stops on the
first one (`EvalAborted`) instead of recording it thirty-eight times as the agents'
failure - which is what the first live run of this harness did.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal

import anthropic

from aioc.agents import DocsAgent, IncidentAgent, RetryLog
from aioc.agents.docs import DEFAULT_TOP_K, CorpusRetriever
from aioc.contracts import AgentResponse
from aioc.llm import (
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
from .scoring import ItemScore, RetryStats, score_failure, score_item

Mode = Literal["realtime", "batch"]
Progress = Callable[[str], None]

# Refusals that are about the caller, not the request: no item can succeed after one.
_NOT_THE_AGENTS_DOING = (anthropic.AuthenticationError, anthropic.PermissionDeniedError)


class EvalAborted(RuntimeError):  # noqa: N818 - names what happened to the run
    """The run cannot continue, and that is no item's result. ``completed`` is how many
    items had been scored when it stopped."""

    def __init__(self, message: str, *, completed: int) -> None:
        super().__init__(message)
        self.completed = completed


def _aborted(exc: BaseException, *, completed: int, at: str) -> EvalAborted:
    return EvalAborted(
        f"the API refused the credential at {at} ({type(exc).__name__}: {exc}); "
        f"{completed} item(s) had been scored. Nothing more can succeed until the key is "
        "replaced - see HANDOFF.md sec 7 item 10.",
        completed=completed,
    )


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
    try:
        response = run_item(item, client, retriever, usage=usage, retry_log=log)
    except (PendingRequest, *_NOT_THE_AGENTS_DOING):
        raise
    except Exception as exc:  # noqa: BLE001 - an agent that raised is a scored outcome
        response, score = None, score_failure(item, exc)
    else:
        if item.task is Task.RECALL and retriever is not None:
            retrieved = retriever.retrieved(item.query)
        score = score_item(item, response, retrieved=retrieved)
    score = replace(score, usage=usage, retry=_retry_stats(log), seconds=time.monotonic() - started)
    return ItemResult(item=item, score=score, response=response, retrieved=retrieved)


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


def run_realtime(
    items: Sequence[EvalItem],
    client: LLMClient,
    retriever: CorpusRetriever | None = None,
    *,
    progress: Progress | None = None,
) -> EvalRun:
    run = _new_run("realtime", client.settings)
    recording = None if retriever is None else RecordingRetriever(retriever)
    started = time.monotonic()
    for item in items:
        try:
            result = _attempt(item, client, recording)
        except _NOT_THE_AGENTS_DOING as exc:
            raise _aborted(exc, completed=len(run.results), at=item.key) from exc
        run.results.append(result)
        _say(progress, result)
    run.seconds = time.monotonic() - started
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
) -> EvalRun:
    run = _new_run("batch", client.settings)
    recording = None if retriever is None else RecordingRetriever(retriever)
    started = time.monotonic()
    done: dict[str, ItemResult] = {}
    waiting = list(items)
    # One pass per model call an item can make: the first attempt, then each retry.
    passes = client.settings.max_validation_retries + 1
    for number in range(1, passes + 2):
        still: list[EvalItem] = []
        for item in waiting:
            try:
                done[item.key] = _attempt(item, client, recording)
            except PendingRequest:
                still.append(item)
            else:
                _say(progress, done[item.key])
        waiting = still
        if not waiting:
            break
        if number > passes:
            # Cannot happen while the retry cap holds; refuse to loop on it if it does.
            for item in waiting:
                done[item.key] = ItemResult(
                    item,
                    score_failure(item, RuntimeError(f"still pending after {passes} passes")),
                    None,
                )
            break
        if progress is not None:
            progress(f"  pass {number}: {len(client.pending)} request(s) submitted as one batch")
        try:
            batch = client.flush(batcher)
        except _NOT_THE_AGENTS_DOING as exc:
            raise _aborted(exc, completed=len(done), at=f"batch submission {number}") from exc
        if progress is not None and batch is not None:
            progress(
                f"  pass {number}: batch {batch.batch_id} ended in {batch.seconds:.0f}s, "
                f"{batch.succeeded} succeeded, {batch.failed} failed"
            )
    run.results = [done[item.key] for item in items]
    # A batched item's own clock measures replay, not the model: the batches carry the time.
    run.results = [
        replace(result, score=replace(result.score, seconds=None)) for result in run.results
    ]
    run.batches = list(client.runs)
    run.seconds = time.monotonic() - started
    if recording is not None:
        run.retrieval_degraded = recording.degraded()
    return run
