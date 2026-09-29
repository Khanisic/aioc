"""What a measured `Usage` costs (Day 19): the one price table, and the two cost levers.

Pricing is dev tooling, not a contract concern: `CoordinatorResponse.cost.usd` stays null
because a response knows its tokens and not which price table is current. What reads this
module is `scripts/cost_review.py` (every recorded live run) and the eval report
(`aioc.evals`), where the point is the delta between two ways of running the same cases.

Prices are USD per million tokens from the published table as read on 2026-09-25. A model
missing from `PRICES` is unpriced rather than guessed at: `price` returns ``None``.

The two levers, and how each changes the arithmetic:

- **Prompt caching.** The prompt is billed in three parts. Tokens read from the cache cost
  `Price.cache_read`; tokens written to it cost the input rate times
  `CACHE_WRITE_MULTIPLIER` for the TTL used; the rest costs the input rate. `Usage` keeps
  the total and the two cache counters, so the uncached part is the difference.
- **The Batch API.** Half price on every token, cached or not (`BATCH_DISCOUNT`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .tool_use import Usage

CacheTtl = Literal["5m", "1h"]


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float


PRICES: dict[str, Price] = {
    "claude-opus-5-5": Price(input=4.00, output=20.00, cache_read=0.20),
    "claude-opus-5": Price(input=5.00, output=25.00, cache_read=0.50),
    "claude-sonnet-5-5": Price(input=2.00, output=10.00, cache_read=0.20),
    "claude-sonnet-5": Price(input=2.00, output=10.00, cache_read=0.20),
    "claude-haiku-4-5": Price(input=1.00, output=5.00, cache_read=0.10),
}

# A cache write is the input rate times this; the longer TTL is the dearer write.
CACHE_WRITE_MULTIPLIER: dict[CacheTtl, float] = {"5m": 1.25, "1h": 2.0}

BATCH_DISCOUNT = 0.5


def price_key(model: str) -> str | None:
    """The `PRICES` entry for a model id. `claude-haiku-4-5-20251001` is priced as
    `claude-haiku-4-5`; the longest match wins, so `claude-opus-5-5` is never priced as
    `claude-opus-5`."""
    known = [name for name in PRICES if model.startswith(name)]
    return max(known, key=len) if known else None


def price(
    model: str,
    usage: Usage,
    *,
    batch: bool = False,
    cache_ttl: CacheTtl = "5m",
) -> float | None:
    """USD for one accumulator, or ``None`` for a model with no listed price."""
    key = price_key(model)
    if key is None:
        return None
    p = PRICES[key]
    read = usage.cache_read_tokens or 0
    write = usage.cache_write_tokens or 0
    usd = (
        usage.uncached_input_tokens * p.input
        + write * p.input * CACHE_WRITE_MULTIPLIER[cache_ttl]
        + read * p.cache_read
        + usage.output_tokens * p.output
    ) / 1e6
    return usd * BATCH_DISCOUNT if batch else usd


def price_uncached(model: str, usage: Usage, *, batch: bool = False) -> float | None:
    """What the same tokens would have cost with caching off - every prompt token at the
    input rate. The delta against `price` is what the cache saved (or, when nothing was
    read back, what the writes cost)."""
    flat = Usage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)
    return price(model, flat, batch=batch)
