"""Day 23: model routing, projected rather than measured.

The plan asks for a routing experiment - Haiku or Sonnet for the subagents, Opus for the
coordinator - with cost and latency measured per configuration. From Day 22 the project
spends nothing on the API (HANDOFF sec 1), so this module answers the half that can be
answered without a paid call and says plainly which half that is:

- **Cost is projected.** A workload's tokens were measured on one model (the source); each
  role's tokens are rescaled by a tokenizer ratio counted on the free token-counting
  endpoint (`TokenRatio`, one per model: the same texts counted on both models), then priced
  by `aioc.llm.pricing` with the cache behaviour the source run measured. Output tokens are
  rescaled the same way, which assumes a model writes the same report in its own tokens -
  verbosity per model is not measured, and the report says so.
- **Latency and quality are not projected.** Both need calls. The measured source-model
  numbers are reported as they are; nothing is extrapolated from them.

Three facts decide whether a route is runnable or its cache survives, each read from the
API documentation on 2026-10-06 rather than assumed:

- Claude Opus 5.5 and Claude Sonnet 5.5 refuse a forced ``tool_choice`` (``tool`` / ``any``)
  with a 400, on the token-counting endpoint too. Every agent and the coordinator force
  their emit tool (`agents._retry`, `coordinator.planner`), so a route naming either model is
  priced but marked not runnable until the harness steers with ``auto`` instead.
- A prefix below the model's minimum cacheable length silently does not cache. Haiku 4.5's
  minimum is 4096 tokens; a role whose smallest prefix falls under it is flagged, and every
  route also carries a ceiling with no cache reads at all.
- Claude 4.7 and later count about 30% more tokens than earlier models for the same text,
  so Haiku 4.5 and the 5-series are never compared token for token - hence the ratios.

Nothing here touches the network; `scripts/project_model_routing.py` does the counting.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, get_args

from aioc.llm import Usage
from aioc.llm.config import Role
from aioc.llm.pricing import price, price_uncached

ROLES: tuple[Role, ...] = get_args(Role)

# Models that answer a forced tool_choice with a 400 (read 2026-10-06).
FORCED_TOOL_CHOICE_REFUSED = frozenset({"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"})

# Minimum cacheable prefix in tokens, per model (read 2026-10-06). A model missing here is
# not judged; `project` says so rather than assuming it caches.
CACHE_MINIMUM_TOKENS: dict[str, int] = {
    "claude-opus-5-5": 512,
    "claude-opus-5": 512,
    "claude-sonnet-5-5": 512,
    "claude-sonnet-5": 1024,
    "claude-haiku-4-5": 4096,
}


@dataclass(frozen=True, slots=True)
class Route:
    """One model per role."""

    name: str
    coordinator: str
    subagent: str

    def model(self, role: Role) -> str:
        return self.coordinator if role == "coordinator" else self.subagent


# The plan's two configurations, the cheaper neighbours a reader would ask about next, and
# the current generation, which the harness cannot call yet. The first is the measured one.
ROUTES: tuple[Route, ...] = (
    Route("as run: Sonnet 5 for both", "claude-sonnet-5", "claude-sonnet-5"),
    Route("plan: Opus coordinator, Sonnet subagents", "claude-opus-5", "claude-sonnet-5"),
    Route("plan: Opus coordinator, Haiku subagents", "claude-opus-5", "claude-haiku-4-5"),
    Route("Sonnet coordinator, Haiku subagents", "claude-sonnet-5", "claude-haiku-4-5"),
    Route("Haiku for both", "claude-haiku-4-5", "claude-haiku-4-5"),
    Route("Opus 5.5 coordinator, Sonnet 5 subagents", "claude-opus-5-5", "claude-sonnet-5"),
    Route("current generation: Opus 5.5 and Sonnet 5.5", "claude-opus-5-5", "claude-sonnet-5-5"),
)


@dataclass(frozen=True, slots=True)
class TokenRatio:
    """A model's token count over the source model's for the same texts, input and output
    counted separately (a report is JSON-heavy, a prompt is prose and schema)."""

    input: float
    output: float

    @staticmethod
    def of(model: Sequence[int], source: Sequence[int]) -> float:
        total = sum(source)
        if total <= 0:
            raise ValueError("a ratio needs a non-empty source count")
        return sum(model) / total


IDENTITY = TokenRatio(input=1.0, output=1.0)


@dataclass(frozen=True, slots=True)
class Workload:
    """What one measured run asked of each role, on the model it ran on.

    ``prefixes`` is the cacheable prefix (system prompt plus tool schemas) of every distinct
    call shape in a role, counted on the source model; the smallest is the one a model's
    cache minimum can exclude. ``wall_seconds`` is the measured clock, reported as is.
    """

    name: str
    source_model: str
    usage: Mapping[Role, Usage]
    invocations: Mapping[Role, int]
    prefixes: Mapping[Role, Mapping[str, int]] = field(default_factory=dict)
    wall_seconds: float | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RoleCost:
    role: Role
    model: str
    usage: Usage
    usd: float
    usd_without_cache: float
    # Prefixes this model would not cache (rescaled length below its minimum).
    uncached_prefixes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouteCost:
    route: Route
    roles: tuple[RoleCost, ...]
    runnable: bool
    notes: tuple[str, ...]

    @property
    def usd(self) -> float:
        return sum(r.usd for r in self.roles)

    @property
    def usd_without_cache(self) -> float:
        """The ceiling: every prompt token at the input rate, as if no prefix cached."""
        return sum(r.usd_without_cache for r in self.roles)


def rescale(usage: Usage, ratio: TokenRatio) -> Usage:
    """The same work counted in another model's tokens. The cache counters keep their share
    of the prompt; `None` (never reported) stays `None`."""

    def scaled(n: int | None, by: float) -> int | None:
        return None if n is None else round(n * by)

    return Usage(
        input_tokens=round(usage.input_tokens * ratio.input),
        output_tokens=round(usage.output_tokens * ratio.output),
        cache_read_tokens=scaled(usage.cache_read_tokens, ratio.input),
        cache_write_tokens=scaled(usage.cache_write_tokens, ratio.input),
    )


def project(workload: Workload, route: Route, ratios: Mapping[str, TokenRatio]) -> RouteCost:
    """Price one route over one workload. A model with no ratio is refused rather than
    assumed to tokenize like the source - except the source model itself."""
    roles: list[RoleCost] = []
    notes: list[str] = []
    for role in ROLES:
        measured = workload.usage.get(role)
        if measured is None:
            continue
        model = route.model(role)
        ratio = IDENTITY if model == workload.source_model else ratios.get(model)
        if ratio is None:
            raise KeyError(f"no token ratio for {model}; count it before projecting")
        usage = rescale(measured, ratio)
        usd = price(model, usage)
        flat = price_uncached(model, usage)
        if usd is None or flat is None:
            raise KeyError(f"{model} has no listed price")
        minimum = CACHE_MINIMUM_TOKENS.get(model)
        if minimum is None:
            notes.append(f"{model}: minimum cacheable prefix unknown, cache assumed to work")
            uncached: tuple[str, ...] = ()
        else:
            uncached = tuple(
                sorted(
                    name
                    for name, tokens in workload.prefixes.get(role, {}).items()
                    if tokens * ratio.input < minimum
                )
            )
        if uncached:
            notes.append(
                f"{role} on {model}: {', '.join(uncached)} below the {minimum}-token cache "
                "minimum - the projection keeps the measured cache share; the ceiling does not"
            )
        roles.append(RoleCost(role, model, usage, usd, flat, uncached))
    refused = sorted({r.model for r in roles} & FORCED_TOOL_CHOICE_REFUSED)
    notes.extend(
        f"{model} refuses a forced tool_choice: a route using it is not runnable until the "
        "emit calls steer with auto"
        for model in refused
    )
    return RouteCost(route, tuple(roles), runnable=not refused, notes=tuple(notes))


def project_all(
    workload: Workload, ratios: Mapping[str, TokenRatio], routes: Sequence[Route] = ROUTES
) -> list[RouteCost]:
    """Every route, once per distinct set of models the workload's roles would run: on a
    subagent-only workload, routes that differ only in the coordinator are one route."""
    seen: set[tuple[str, ...]] = set()
    costs = []
    for route in routes:
        key = tuple(route.model(role) for role in ROLES if role in workload.usage)
        if key in seen:
            continue
        seen.add(key)
        costs.append(project(workload, route, ratios))
    return costs


def remainder(total: Usage, part: Usage) -> Usage:
    """What is left of a request's measured `Usage` once one part is taken out - the
    subagents' share, when the part is the coordinator's rebuilt calls. A part larger than
    the whole on any counter means the part was mis-rebuilt, and is refused."""

    def left(whole: int | None, piece: int | None, name: str) -> int | None:
        if whole is None:
            if piece:
                raise ValueError(f"{name}: the total never reported it, the part has {piece}")
            return None
        rest = whole - (piece or 0)
        if rest < 0:
            raise ValueError(f"{name}: the part ({piece:,}) exceeds the total ({whole:,})")
        return rest

    return Usage(
        input_tokens=left(total.input_tokens, part.input_tokens, "input_tokens") or 0,
        output_tokens=left(total.output_tokens, part.output_tokens, "output_tokens") or 0,
        cache_read_tokens=left(
            total.cache_read_tokens, part.cache_read_tokens, "cache_read_tokens"
        ),
        cache_write_tokens=left(
            total.cache_write_tokens, part.cache_write_tokens, "cache_write_tokens"
        ),
    )


def models_needed(routes: Sequence[Route] = ROUTES) -> list[str]:
    """Every model a set of routes names, in first-mention order."""
    seen: dict[str, None] = {}
    for route in routes:
        seen.setdefault(route.coordinator)
        seen.setdefault(route.subagent)
    return list(seen)


# ---------------------------------------------------------------------------- rendering


def _usd(value: float) -> str:
    return f"${value:.3f}" if value < 1 else f"${value:.2f}"


def _delta(value: float, reference: float) -> str:
    if reference <= 0:
        return "-"
    pct = (value - reference) / reference * 100
    return "0%" if abs(pct) < 0.5 else f"{pct:+.0f}%"


def _short(model: str) -> str:
    return model.removeprefix("claude-")


def _label(workload: Workload, route: Route) -> str:
    """The route's name, or the subagent model alone where the workload has no coordinator
    calls - a name about the coordinator would describe nothing that was priced."""
    if "coordinator" in workload.usage:
        return route.name
    return f"subagents on {_short(route.subagent)}"


def _coordinator(workload: Workload, route: Route) -> str:
    return _short(route.coordinator) if "coordinator" in workload.usage else "-"


def render(workload: Workload, costs: Sequence[RouteCost], ratios: Mapping[str, TokenRatio]) -> str:
    """One workload's routes as Markdown: the table, the per-role split, the notes."""
    reference = costs[0].usd if costs else 0.0
    lines = [
        f"### {workload.name}",
        "",
        f"Measured on `{workload.source_model}`: "
        + ", ".join(
            f"{role} {workload.invocations.get(role, 0)} invocation(s), "
            f"{workload.usage[role].input_tokens:,} in / {workload.usage[role].output_tokens:,} out"
            for role in ROLES
            if role in workload.usage
        )
        + (f"; {workload.wall_seconds:.0f} s wall clock" if workload.wall_seconds else "")
        + ".",
        "",
    ]
    lines.extend(f"- {note}" for note in workload.notes)
    if workload.notes:
        lines.append("")
    lines += [
        "| Route | Coordinator | Subagents | Projected | vs as run | Ceiling (no cache) "
        "| Runnable today |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for c in costs:
        label, coordinator = _label(workload, c.route), _coordinator(workload, c.route)
        lines.append(
            f"| {label} | {coordinator} | {_short(c.route.subagent)} "
            f"| {_usd(c.usd)} | {_delta(c.usd, reference)} | {_usd(c.usd_without_cache)} "
            f"| {'yes' if c.runnable else 'no'} |"
        )
    lines += ["", "Per role (projected):", ""]
    lines += [
        "| Route | Role | Model | In | Out | Cache read | Cost |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for c in costs:
        label = _label(workload, c.route)
        for r in c.roles:
            read = r.usage.cache_read_tokens
            lines.append(
                f"| {label} | {r.role} | {_short(r.model)} | {r.usage.input_tokens:,} "
                f"| {r.usage.output_tokens:,} | {'-' if read is None else f'{read:,}'} "
                f"| {_usd(r.usd)} |"
            )
    notes = sorted({n for c in costs for n in c.notes})
    if notes:
        lines += ["", "Notes:", ""]
        lines.extend(f"- {n}" for n in notes)
    return "\n".join(lines)


def render_ratios(source_model: str, ratios: Mapping[str, TokenRatio]) -> str:
    source = _short(source_model)
    lines = [
        f"| Model | Input tokens vs {source} | Output tokens vs {source} |",
        "|---|---:|---:|",
    ]
    for model, r in ratios.items():
        lines.append(f"| {_short(model)} | {r.input:.3f} | {r.output:.3f} |")
    return "\n".join(lines)


def record(workload: Workload, costs: Sequence[RouteCost]) -> dict[str, Any]:
    """The JSON form of one workload's projection."""
    return {
        "workload": workload.name,
        "source_model": workload.source_model,
        "measured": {
            role: {"invocations": workload.invocations.get(role, 0), **_usage(workload.usage[role])}
            for role in ROLES
            if role in workload.usage
        },
        "prefixes": {role: dict(p) for role, p in workload.prefixes.items()},
        "wall_seconds": workload.wall_seconds,
        "routes": [
            {
                "name": c.route.name,
                "coordinator": c.route.coordinator,
                "subagent": c.route.subagent,
                "usd": round(c.usd, 6),
                "usd_without_cache": round(c.usd_without_cache, 6),
                "runnable": c.runnable,
                "roles": [
                    {
                        "role": r.role,
                        "model": r.model,
                        **_usage(r.usage),
                        "usd": round(r.usd, 6),
                        "uncached_prefixes": list(r.uncached_prefixes),
                    }
                    for r in c.roles
                ],
                "notes": list(c.notes),
            }
            for c in costs
        ],
    }


def _usage(u: Usage) -> dict[str, int | None]:
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_tokens,
        "cache_write_tokens": u.cache_write_tokens,
    }
