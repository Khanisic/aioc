"""What a run has finished, written as it finishes and read back (Day 20).

The second live run of the eval harness was five items in when the account ran out of
credit, and the run before it was stopped by hand. Both had paid for answers that were
then thrown away, because a run kept its results in memory until it ended. An eval item is
one model call that costs a few cents and takes half a minute; nothing about it needs to
wait for the other thirty-seven.

So a run writes each item to `progress.jsonl` in its run directory the moment the item is
scored, under a header that says what kind of run it is. A later run can be handed that
directory, takes from it every item worth keeping, and asks only for the rest.

**What is worth keeping.** An answered item, and an item whose agent gave up on its
report - both are results of the thing under test. An item the *environment* failed (the
API refused the call, the network dropped it) is a result of nothing, so it is left out
and asked again. That distinction is recorded as ``error_kind``; for records written
before it existed it is read off the error's class name.

**What is read back is scored again.** The stored score is kept for what cannot be
recomputed - the tokens, the retries, the clock - and everything else is derived from the
stored response by today's rules. A resumed run is therefore scored by one set of rules
throughout, and `restore` is the same function a re-score uses.

A run directory written before `progress.jsonl` existed is read from its `eval.json` and
`responses.json`, which hold the same things once a run has ended.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from aioc.contracts import AgentResponse, DocsAgentResponse, IncidentAgentResponse
from aioc.llm import Usage

from .cases import EvalItem, EvalSet, Task
from .runner import ItemResult
from .scoring import RetryStats, score_failure, score_item

PROGRESS = "progress.jsonl"

# Error classes that mean the call failed, not the agent. Used only for records that
# predate `error_kind`; a new record says which it was.
_ENVIRONMENT_ERRORS = frozenset(
    {
        "APIConnectionError",
        "APIError",
        "APIStatusError",
        "APITimeoutError",
        "AuthenticationError",
        "BadRequestError",
        "BatchError",
        "ConflictError",
        "InternalServerError",
        "NotFoundError",
        "OverloadedError",
        "PermissionDeniedError",
        "RateLimitError",
        "ServiceUnavailableError",
        "UnprocessableEntityError",
    }
)


class StoreError(ValueError):
    """The run directory cannot be read, or is not the kind of run it is wanted for."""


@dataclass(frozen=True, slots=True)
class RunConfig:
    """What makes two runs the same run, continued: the set, the model, and the levers."""

    set_sha256: str
    model: str
    mode: str
    prompt_caching: bool
    cache_ttl: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "set_sha256": self.set_sha256,
            "model": self.model,
            "mode": self.mode,
            "prompt_caching": self.prompt_caching,
            "cache_ttl": self.cache_ttl,
        }

    def differs_from(self, other: RunConfig) -> list[str]:
        """The fields on which the two disagree, as `name: this, not that`. The cache
        lifetime is compared only when caching is on - off, it changes nothing."""
        names = ["set_sha256", "model", "mode", "prompt_caching"]
        if self.prompt_caching and other.prompt_caching:
            names.append("cache_ttl")
        return [
            f"{name}: {getattr(other, name)!r}, not {getattr(self, name)!r}"
            for name in names
            if getattr(self, name) != getattr(other, name)
        ]


@dataclass(frozen=True, slots=True)
class StoredRun:
    path: Path
    config: RunConfig
    results: dict[str, ItemResult]  # every item the run recorded, by key
    complete: bool  # the run ended and wrote its record

    def reusable(self) -> dict[str, ItemResult]:
        """The items a continued run need not ask again."""
        return {
            key: result
            for key, result in self.results.items()
            if result.score.answered or result.score.error_kind == "agent"
        }


def failure_kind(score: Mapping[str, Any]) -> str | None:
    """Whose failure a stored item was. ``None`` for an item that was answered."""
    if score.get("answered"):
        return None
    kind = score.get("error_kind")
    if kind in ("agent", "environment"):
        return str(kind)
    name = str(score.get("error") or "").split(":", 1)[0].strip()
    return "environment" if name in _ENVIRONMENT_ERRORS else "agent"


def entry(result: ItemResult) -> dict[str, Any]:
    """One finished item as it is written: the score, the response, what was retrieved."""
    response = result.response
    return {
        "key": result.item.key,
        "score": result.score.to_dict(),
        "response": None if response is None else response.model_dump(mode="json"),
        "retrieved": None if result.retrieved is None else list(result.retrieved),
    }


def restore(
    item: EvalItem,
    score: Mapping[str, Any],
    response: Mapping[str, Any] | None,
    retrieved: list[str] | None,
) -> ItemResult:
    """A stored item as an `ItemResult`, scored by today's rules. The tokens, the retries,
    and the clock are the stored ones: they were measured, and cannot be measured again."""
    found = None if retrieved is None else tuple(retrieved)
    parsed: AgentResponse | None = None
    if response is None:
        restored = score_failure(item, RuntimeError(score.get("error") or "no response recorded"))
        restored = replace(restored, error=score.get("error"), error_kind=failure_kind(score))
    else:
        model = IncidentAgentResponse if item.task is Task.DIAGNOSE else DocsAgentResponse
        parsed = model.model_validate(response)
        restored = score_item(item, parsed, retrieved=found)
    usage, retry = score["usage"], score["retry"]
    restored = replace(
        restored,
        usage=Usage(
            input_tokens=usage["in"],
            output_tokens=usage["out"],
            cache_read_tokens=usage.get("cache_read"),
            cache_write_tokens=usage.get("cache_write"),
        ),
        retry=RetryStats(
            attempts=retry["attempts"],
            rejections=tuple(retry["rejections"]),
            outcome=retry["outcome"],
        ),
        seconds=score.get("seconds"),
    )
    return ItemResult(item, restored, parsed, found)


class ProgressFile:
    """`progress.jsonl`: a header, then one line for each item as it finishes. Appended
    and flushed a line at a time, so a run killed mid-item loses that item and no other."""

    def __init__(self, directory: Path, config: RunConfig) -> None:
        self.path = directory / PROGRESS
        self.path.write_text(json.dumps({"header": config.to_dict()}) + "\n", encoding="utf-8")

    def write(self, result: ItemResult) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry(result)) + "\n")
            handle.flush()


def _from_progress(path: Path, items: Mapping[str, EvalItem]) -> tuple[RunConfig, dict[str, Any]]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise StoreError(f"{path} is empty")
    try:
        header = json.loads(lines[0])["header"]
        config = RunConfig(**header)
    except (ValueError, KeyError, TypeError) as exc:
        raise StoreError(f"{path} does not begin with a run header") from exc
    entries: dict[str, Any] = {}
    for line in lines[1:]:
        try:
            stored = json.loads(line)
        except ValueError:
            # The last line of a run that was killed while writing it.
            continue
        if stored.get("key") in items:
            entries[stored["key"]] = stored
    return config, entries


def _from_record(run_dir: Path, items: Mapping[str, EvalItem]) -> tuple[RunConfig, dict[str, Any]]:
    try:
        record = json.loads((run_dir / "eval.json").read_text(encoding="utf-8"))
        kept = json.loads((run_dir / "responses.json").read_text(encoding="utf-8"))
        config = RunConfig(
            set_sha256=record["set"]["sha256"],
            model=record["run"]["model"],
            mode=record["run"]["mode"],
            prompt_caching=record["run"]["prompt_caching"],
            cache_ttl=record["run"]["cache_ttl"],
        )
    except (OSError, ValueError, KeyError) as exc:
        raise StoreError(f"{run_dir} holds no readable eval record") from exc
    entries = {
        score["key"]: {
            "key": score["key"],
            "score": score,
            "response": (kept.get(score["key"]) or {}).get("response"),
            "retrieved": (kept.get(score["key"]) or {}).get("retrieved"),
        }
        for score in record["items"]
        if score["key"] in items
    }
    return config, entries


def read_run(run_dir: Path, cases: EvalSet) -> StoredRun:
    """Everything a run directory recorded, restored. Items no longer in the case file
    are left out; `StoreError` when the directory holds nothing to read."""
    items = {item.key: item for item in cases.items}
    progress = run_dir / PROGRESS
    if progress.is_file():
        config, entries = _from_progress(progress, items)
    elif (run_dir / "eval.json").is_file():
        config, entries = _from_record(run_dir, items)
    else:
        raise StoreError(f"{run_dir} holds neither {PROGRESS} nor eval.json")
    results = {
        key: restore(items[key], stored["score"], stored["response"], stored["retrieved"])
        for key, stored in entries.items()
    }
    return StoredRun(
        path=run_dir,
        config=config,
        results=results,
        complete=(run_dir / "eval.json").is_file(),
    )
