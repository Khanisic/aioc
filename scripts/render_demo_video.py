"""Render the demo video from a recorded service run: break, diagnose, trace and cost, architecture.

    uv run python scripts/render_demo_video.py --run test-results/runs/2026-10-10/<day25-run>
    uv run python scripts/render_demo_video.py --run <run-dir> --frames <dir>  # keep the PNGs
    uv run python scripts/render_demo_video.py --run <run-dir> --plan          # timings only

Free - no API calls, no network. The input is one `scripts/check_day25_service.py` run
directory: `transcript.json` (or the `transcript.replayed.json` its `--replay` writes, preferred
when present), `job.json`, `response.json`, `trace.json` (the Langfuse trace as read back after
the run), and `events.jsonl` (the model). The output is a 1080p H.264 MP4, encoded by `ffmpeg`,
which must be on PATH.

The structure is the plan's (EXECUTION_PLAN Day 28), and it leads with the break:

1. **Break production** - the run's own terminal, steps 1-3: the release, the injected fault,
   and the situation built from live metrics.
2. **Agents diagnose** - steps 4-6: one request to the service, the plan, each agent's
   summary, the answer, the approval gate, the trace read back.
3. **Trace and cost** - the request's spans drawn from `trace.json` at their measured times,
   each tool call where it ran, and the bill priced by `aioc.llm.pricing` from the response's
   own token counts.
4. **Architecture** - `docs/assets/architecture.png`, whole and then at 1:1 in three stops.

Nothing is re-enacted. The terminal frames are `render_demo_gif.terminal_frames` over the
recorded transcript, dead time compressed exactly as the GIF compresses it. Every number in a
caption is read from the run, and a caption that depends on what an agent said is checked
against what it said: if Incident had not named the injected service, the caption would not
claim it did. There is no audio; the captions are the narration, and each holds for at least
its reading time.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_demo_gif import (  # noqa: E402
    BG,
    BLUE,
    DEFAULT,
    DIM,
    GREEN,
    YELLOW,
    frame_size,
    load_font,
    terminal_frames,
)

from aioc.llm.pricing import price, price_uncached  # noqa: E402
from aioc.llm.tool_use import Usage  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[1]

W, H = 1920, 1080
MARGIN = 40
HEADER_H = 72
CAPTION_H = 150
CONTENT = (MARGIN, HEADER_H, W - MARGIN, H - CAPTION_H)  # left, top, right, bottom
CONTENT_W = CONTENT[2] - CONTENT[0]
CONTENT_H = CONTENT[3] - CONTENT[1]

PAGE = (8, 11, 15)
PANEL = BG
GRID = (33, 38, 45)
FAINT = (88, 96, 105)

TERMINAL_FONT = 24
TERMINAL_COLS = 120
TERMINAL_ROWS = 25
TERMINAL_LEADING = 13  # the GIF's 7 crowds descenders at this size
CAPTION_FONT = 34
CAPTION_LINES = 2
CAPTION_LINE_H = 46

# A caption holds for at least this long, so a line that printed in a burst cannot flash a
# caption past: a fixed beat plus a reading pace a viewer can keep.
CAPTION_MIN_MS = 1500
CAPTION_MS_PER_CHAR = 55

SWEEP_S = 12.0  # the trace's playhead crosses the whole request in this long
SWEEP_FPS = 15
PAN_S = 1.6
PAN_FPS = 20
END_HOLD_MS = 6000

CHAPTERS = ("Break production", "Agents diagnose", "Trace and cost", "Architecture")


# --------------------------------------------------------------------------------------
# What the run says
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """One timed observation of the request, in seconds from the request's start."""

    label: str
    start_s: float
    end_s: float
    input_tokens: int | None
    output_tokens: int | None
    invocation_id: str | None


@dataclass(frozen=True)
class ToolTick:
    """One tool call, at the time it ran (its event's timestamp is when it was recorded)."""

    span: str
    name: str
    start_s: float
    duration_s: float
    ok: bool


@dataclass(frozen=True)
class Timeline:
    trace_id: str
    total_s: float
    spans: tuple[Span, ...]
    tools: tuple[ToolTick, ...]

    def span(self, label: str) -> Span:
        for s in self.spans:
            if s.label == label:
                return s
        raise KeyError(label)


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def read_timeline(trace: dict[str, Any]) -> Timeline:
    """The request's spans and tool calls from a Langfuse trace as `trace.json` records it.

    The root observation is the request; `plan`, each `agent:<name>`, and `hitl_gate` are its
    spans. A tool call is an event whose own timestamp is when the SDK recorded it, so its
    position comes from the measured `started_at` and `duration_ms` in its metadata."""
    observations = trace["observations"]
    roots = [o for o in observations if not o.get("parentObservationId")]
    if len(roots) != 1:
        raise ValueError(f"expected one root observation, found {len(roots)}")
    root = roots[0]
    t0 = _when(root["startTime"])
    total = (_when(root["endTime"]) - t0).total_seconds()

    spans: list[Span] = []
    labels: dict[str, str] = {}
    for o in observations:
        if o is root or o["type"] == "EVENT" or not o.get("endTime"):
            continue
        meta = o.get("metadata") or {}
        label = str(o["name"]).removeprefix("agent:")
        labels[o["id"]] = label
        spans.append(
            Span(
                label=label,
                start_s=(_when(o["startTime"]) - t0).total_seconds(),
                end_s=(_when(o["endTime"]) - t0).total_seconds(),
                input_tokens=meta.get("input_tokens"),
                output_tokens=meta.get("output_tokens"),
                invocation_id=meta.get("invocation_id"),
            )
        )
    spans.sort(key=lambda s: (s.start_s, s.label))

    tools: list[ToolTick] = []
    for o in observations:
        if o["type"] != "EVENT" or o.get("parentObservationId") not in labels:
            continue
        meta = o.get("metadata") or {}
        if "started_at" not in meta:
            continue
        tools.append(
            ToolTick(
                span=labels[o["parentObservationId"]],
                name=str(o["name"]).removeprefix("tool:"),
                start_s=(_when(meta["started_at"]) - t0).total_seconds(),
                duration_s=float(meta.get("duration_ms") or 0) / 1000,
                ok=bool(meta.get("ok", True)),
            )
        )
    tools.sort(key=lambda t: t.start_s)
    return Timeline(
        trace_id=str(trace["id"]), total_s=total, spans=tuple(spans), tools=tuple(tools)
    )


@dataclass(frozen=True)
class Facts:
    """Everything a caption states, read from the run."""

    service: str
    pr: int
    fault: str
    fault_service: str
    fault_knob: str
    probe: str | None
    job_s: float
    model: str
    usage: Usage
    usd: float | None
    usd_uncached: float | None
    parallel: tuple[str, ...]
    sequential: tuple[tuple[str, tuple[str, ...]], ...]  # (agent, the agents it waits for)
    summaries: dict[str, str] = field(default_factory=dict)
    adopted: str | None = None  # the agent whose summary the answer is, if it is one
    decisions: dict[str, int] = field(default_factory=dict)


_FAULT = re.compile(r"\[\d+/\d+\] Breaking production: injecting (\w+)")
_KNOB = re.compile(r"set\s+(\S+)\s+->\s+(\S+)")
_PROBE = re.compile(r"(\S+) x(\d+): statuses=(\{[^}]*\}) mean=(\d+)ms")
_JOB = re.compile(r"([\d.]+)s\s+succeeded")


def _first(pattern: re.Pattern[str], lines: Sequence[str], what: str) -> re.Match[str]:
    for line in lines:
        if m := pattern.search(line):
            return m
    raise ValueError(f"the transcript has no {what} line - is this a check_day25_service.py run?")


def _probe(lines: Sequence[str]) -> str | None:
    for line in lines:
        if m := _PROBE.search(line):
            statuses: dict[str, int] = json.loads(m.group(3).replace("'", '"'))
            calls = int(m.group(2))
            ok = sum(n for code, n in statuses.items() if code.startswith("2"))
            return f"{ok} of {calls} calls succeed, averaging {int(m.group(4))} ms"
    return None


def read_facts(
    lines: Sequence[str],
    job: dict[str, Any],
    response: dict[str, Any],
    run_meta: dict[str, Any],
    model: str,
) -> Facts:
    fault = _first(_FAULT, lines, "fault injection").group(1)
    knob = _first(_KNOB, lines, "chaos knob")
    cost = response["cost"]
    usage = Usage(
        input_tokens=cost["input_tokens"],
        output_tokens=cost["output_tokens"],
        cache_read_tokens=cost.get("cache_read_tokens"),
        cache_write_tokens=cost.get("cache_write_tokens"),
    )
    invocations = [i for i in response["selected_agents"] if i["round"] == 0]
    by_id = {i["invocation_id"]: i["agent"] for i in invocations}
    parallel = tuple(i["agent"] for i in invocations if not i["depends_on"])
    sequential = tuple(
        (i["agent"], tuple(by_id[d] for d in i["depends_on"]))
        for i in invocations
        if i["depends_on"]
    )
    summaries = {r["agent"]: r["summary"] for r in response["agent_responses"]}
    answer = (response.get("answer") or {}).get("value")
    adopted = next((agent for agent, s in summaries.items() if s == answer), None)
    decisions: dict[str, int] = {}
    for d in job.get("approvals", []):
        decisions[d["decision"]] = decisions.get(d["decision"], 0) + 1
    return Facts(
        service=str(run_meta["service"]),
        pr=int(run_meta["pr"]),
        fault=fault,
        fault_service=knob.group(1),
        fault_knob=knob.group(2),
        probe=_probe(lines),
        job_s=float(_first(_JOB, lines, "job succeeded").group(1)),
        model=model,
        usage=usage,
        usd=price(model, usage),
        usd_uncached=price_uncached(model, usage),
        parallel=parallel,
        sequential=sequential,
        summaries=summaries,
        adopted=adopted,
        decisions=decisions,
    )


def _names(agents: Sequence[str]) -> str:
    names = [_agent_name(a) for a in agents]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _agent_name(agent: str) -> str:
    return {"github": "GitHub", "hitl_gate": "Approval gate"}.get(agent, agent.capitalize())


def _count(n: int) -> str:
    words = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
    return words[n] if 0 <= n < len(words) else str(n)


def _knob_phrase(knob: str) -> str:
    """`extra_latency_ms=800` reads as "800 ms of extra latency"; anything else as itself."""
    if m := re.fullmatch(r"extra_latency_ms=(\d+)", knob):
        return f"{m.group(1)} ms of extra latency"
    return knob


# --------------------------------------------------------------------------------------
# Captions for the terminal chapters, keyed by the transcript line that changes them
# --------------------------------------------------------------------------------------

Cue = tuple[Callable[[str], bool], Callable[[Facts], str]]


def _starts(prefix: str) -> Callable[[str], bool]:
    return lambda line: line.strip().startswith(prefix)


def _incident_caption(f: Facts) -> str:
    said = f.summaries.get("incident", "")
    if f.fault_service in said:
        return (
            f"Incident has no tools: it reads the metrics alone, and names {f.fault_service}, "
            "the service the fault was injected into."
        )
    return "Incident has no tools: it reads the metrics alone."


def _answer_caption(f: Facts) -> str:
    if f.adopted is None:
        return "The synthesis writes one answer from the four reports."
    return (
        f"The default synthesis adopts one report, here {_agent_name(f.adopted)}'s. "
        "The opt-in model synthesis combines all four."
    )


def _gate_caption(f: Facts) -> str:
    denied = f.decisions.get("denied", 0)
    total = sum(f.decisions.values())
    if total and denied == total:
        return (
            "Every recommended action needs a human's yes; with no one to ask, "
            f"the fail-closed gate denies all {_count(total)}."
        )
    return "The fail-closed gate decides every recommendation and logs each decision."


def _selection_caption(f: Facts) -> str:
    parts = [f"Dynamic selection: {_names(f.parallel)} run in parallel"]
    for agent, deps in f.sequential:
        parts.append(f"{_agent_name(agent)} runs after {_names(deps)}, whose findings it needs")
    return "; ".join(parts) + "."


CUES: tuple[Cue, ...] = (
    (
        _starts("$ "),
        lambda f: f"{f.service} runs the release that shipped PR #{f.pr}. Nothing is wrong yet.",
    ),
    (
        _starts("[2/6]"),
        lambda f: (
            f"Production breaks for real: {f.fault_service} gets "
            f"{_knob_phrase(f.fault_knob)} ({f.fault})."
        ),
    ),
    (
        _starts("Probing"),
        lambda f: (
            f"{f.service} calls {f.fault_service} on every request, so users feel it"
            + (f": {f.probe}." if f.probe else ".")
        ),
    ),
    (_starts("waiting"), lambda f: "Prometheus needs its rate() windows to fill. Compressed here."),
    (
        _starts("[3/6]"),
        lambda f: (
            "The agents get live Prometheus metrics and what on-call knows, "
            "with no hint of what was injected."
        ),
    ),
    (
        _starts("[4/6]"),
        lambda f: (
            "One request to the deployed service. The coordinator plans, the agents investigate: "
            f"{f.job_s:.0f} s, compressed here."
        ),
    ),
    (_starts("selected"), _selection_caption),
    (_starts("-- incident agent"), _incident_caption),
    (
        _starts("-- docs agent"),
        lambda f: "Docs searches the incident corpus for precedent and cites what it finds.",
    ),
    (_starts("answer ("), _answer_caption),
    (_starts("gate"), _gate_caption),
    (
        _starts("[6/6]"),
        lambda f: "The trace is read back from Langfuse, to check the overlap really happened.",
    ),
)


def caption_for(line: str, facts: Facts, current: str) -> str:
    for matches, text in CUES:
        if matches(line):
            return text(facts)
    return current


def reading_ms(caption: str) -> int:
    return CAPTION_MIN_MS + CAPTION_MS_PER_CHAR * len(caption)


# --------------------------------------------------------------------------------------
# Shots: what is on screen, under which caption, for how long
# --------------------------------------------------------------------------------------


@dataclass
class Shot:
    chapter: int
    caption: str
    ms: int
    draw: Callable[[], Image.Image]


def hold_for_reading(shots: list[Shot]) -> None:
    """Stretch the last shot under each caption until the caption has been up long enough to
    read. Runs of one caption are judged together, so only the run's last shot grows."""
    i = 0
    while i < len(shots):
        j = i
        while j + 1 < len(shots) and shots[j + 1].caption == shots[i].caption:
            j += 1
        shown = sum(s.ms for s in shots[i : j + 1])
        need = reading_ms(shots[i].caption) if shots[i].caption else 0
        if shown < need:
            shots[j].ms += need - shown
        i = j + 1


def terminal_font() -> ImageFont.FreeTypeFont:
    """The terminal font at TERMINAL_FONT, or the largest smaller size whose frame fits the
    content area. Consolas fits at TERMINAL_FONT; a wider face (a Linux runner's, or PIL's
    own when no monospace font is installed) is stepped down rather than refused."""
    for size in range(TERMINAL_FONT, 0, -1):
        font = load_font(size)
        w, h = frame_size(font, cols=TERMINAL_COLS, rows=TERMINAL_ROWS, leading=TERMINAL_LEADING)
        if w <= CONTENT_W and h <= CONTENT_H:
            return font
    raise ValueError(f"no terminal font size fits the {CONTENT_W}x{CONTENT_H} area")


def terminal_shots(
    entries: list[dict[str, Any]], facts: Facts, *, title: str, command: str
) -> list[Shot]:
    font = terminal_font()
    frames, durations = terminal_frames(
        entries,
        title=title,
        command=command,
        font=font,
        cols=TERMINAL_COLS,
        rows=TERMINAL_ROWS,
        leading=TERMINAL_LEADING,
    )
    lines = [f"$ {command}"] + [str(e["line"]) for e in entries]
    split = next(i for i, line in enumerate(lines) if line.strip().startswith("[4/6]"))
    shots: list[Shot] = []
    caption = ""
    for i, (frame, ms) in enumerate(zip(frames, durations, strict=True)):
        caption = caption_for(lines[i], facts, caption)
        shots.append(
            Shot(chapter=0 if i < split else 1, caption=caption, ms=ms, draw=lambda f=frame: f)
        )
    shots[-1].ms = END_HOLD_MS
    return shots


# --------------------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------------------


def _ui_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ("seguisb.ttf", "segoeuib.ttf") if bold else ("segoeui.ttf",)
    for name in (*names, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)  # type: ignore[return-value]


def wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if current and font.getlength(trial) > width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _tint(color: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """`color` moved toward white by `amount` (0..1)."""
    r, g, b = color
    return (
        round(r + (255 - r) * amount),
        round(g + (255 - g) * amount),
        round(b + (255 - b) * amount),
    )


def _shade(color: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    """`color` moved toward the panel background by `amount` (0..1)."""
    return tuple(round(c + (p - c) * amount) for c, p in zip(color, PANEL, strict=True))  # type: ignore[return-value]


def compose(content: Image.Image, chapter: int, caption: str, progress: float) -> Image.Image:
    page = Image.new("RGB", (W, H), PAGE)
    draw = ImageDraw.Draw(page)

    brand = _ui_font(26, bold=True)
    draw.text((MARGIN, 22), "AIOC", font=brand, fill=BLUE)
    nav = _ui_font(22)
    nav_bold = _ui_font(22, bold=True)
    x = W - MARGIN
    for i in reversed(range(len(CHAPTERS))):
        label = f"{i + 1}  {CHAPTERS[i]}"
        font = nav_bold if i == chapter else nav
        width = int(font.getlength(label))
        x -= width
        draw.text((x, 26), label, font=font, fill=DEFAULT if i == chapter else FAINT)
        if i == chapter:
            draw.rectangle((x, 58, x + width, 61), fill=BLUE)
        x -= 44

    if content.width > CONTENT_W or content.height > CONTENT_H:
        raise ValueError(f"a {content.size} shot does not fit the {CONTENT_W}x{CONTENT_H} area")
    left = CONTENT[0] + (CONTENT_W - content.width) // 2
    top = CONTENT[1] + (CONTENT_H - content.height) // 2
    page.paste(content, (left, top))

    if caption:
        font = _ui_font(CAPTION_FONT)
        lines = wrap(caption, font, W - 2 * MARGIN - 80)
        if len(lines) > CAPTION_LINES:
            raise ValueError(
                f"caption needs {len(lines)} lines, at most {CAPTION_LINES}: {caption}"
            )
        block = len(lines) * CAPTION_LINE_H
        y = CONTENT[3] + (CAPTION_H - block) // 2
        for line in lines:
            width = int(font.getlength(line))
            draw.text(((W - width) // 2, y), line, font=font, fill=DEFAULT)
            y += CAPTION_LINE_H

    draw.rectangle((0, H - 5, W, H), fill=GRID)
    draw.rectangle((0, H - 5, round(W * progress), H), fill=BLUE)
    return page


def _rounded_panel(width: int, height: int) -> Image.Image:
    panel = Image.new("RGB", (width, height), PAGE)
    ImageDraw.Draw(panel).rounded_rectangle((0, 0, width - 1, height - 1), radius=14, fill=PANEL)
    return panel


def draw_timeline(tl: Timeline, facts: Facts, now_s: float, *, show_cost: bool) -> Image.Image:
    """The request as a timeline: each span a bar at its measured times, each tool call a
    lighter mark inside the bar where it ran, the dependency drawn as an arrow, and the bill
    underneath. Everything to the right of `now_s` is not drawn yet."""
    img = _rounded_panel(CONTENT_W, CONTENT_H)
    draw = ImageDraw.Draw(img)
    small = _ui_font(22)
    label_font = _ui_font(28, bold=True)
    tag_font = _ui_font(20)

    draw.text(
        (40, 28),
        f"Langfuse trace {tl.trace_id[:12]}..., read back after the run. "
        f"Bars are measured start and end; marks inside are tool calls.",
        font=small,
        fill=DIM,
    )

    x0, x1 = 300, CONTENT_W - 380
    top, row_h, bar_h = 96, 86, 38

    def x_at(t: float) -> int:
        return x0 + round((x1 - x0) * min(max(t, 0.0), tl.total_s) / tl.total_s)

    rows_bottom = top + row_h * len(tl.spans)
    step = 10 if tl.total_s > 30 else 5
    for tick in range(0, int(tl.total_s) + 1, step):
        x = x_at(tick)
        draw.line((x, top - 8, x, rows_bottom), fill=GRID, width=1)
        text = f"{tick} s" if tick == 0 else str(tick)
        draw.text((x - small.getlength(text) / 2, rows_bottom + 8), text, font=small, fill=FAINT)

    modes = {a: "parallel" for a in facts.parallel}
    for agent, deps in facts.sequential:
        modes[agent] = f"after {_names(deps)}"

    ys: dict[str, int] = {}
    for i, span in enumerate(tl.spans):
        y = top + i * row_h + (row_h - bar_h) // 2
        ys[span.label] = y
        draw.text((40, y - 2), _agent_name(span.label), font=label_font, fill=DEFAULT)
        tag = modes.get(span.label)
        if tag:
            draw.text((40, y + 32), tag, font=tag_font, fill=DIM)

        color = DIM
        if span.label in modes:
            color = GREEN if modes[span.label].startswith("after") else BLUE
        elif span.label == "hitl_gate":
            color = YELLOW
        if now_s <= span.start_s:
            continue
        end = min(span.end_s, now_s)
        left, right = x_at(span.start_s), max(x_at(end), x_at(span.start_s) + 4)
        draw.rounded_rectangle((left, y, right, y + bar_h), radius=7, fill=_shade(color, 0.35))
        for tool in tl.tools:
            if tool.span != span.label or tool.start_s >= now_s:
                continue
            tl_left = x_at(tool.start_s)
            tl_right = max(x_at(min(tool.start_s + tool.duration_s, now_s)), tl_left + 4)
            draw.rectangle((tl_left, y + 6, tl_right, y + bar_h - 6), fill=_tint(color, 0.55))
        if now_s >= span.end_s and span.input_tokens is not None and span.label != "hitl_gate":
            text = f"{span.input_tokens:,} in / {span.output_tokens or 0:,} out"
            draw.text((x1 + 30, y + 4), text, font=small, fill=DIM)

    for agent, deps in facts.sequential:
        if agent not in ys:
            continue
        child = tl.span(agent)
        if now_s < child.start_s:
            continue
        for dep in deps:
            if dep not in ys:
                continue
            parent = tl.span(dep)
            px, py = x_at(parent.end_s), ys[dep] + bar_h
            cx, cy = x_at(child.start_s), ys[agent]
            draw.line((px, py, px, cy - 10), fill=GREEN, width=3)
            draw.polygon([(cx - 8, cy - 12), (cx + 8, cy - 12), (cx, cy)], fill=GREEN)

    if now_s < tl.total_s:
        x = x_at(now_s)
        draw.line((x, top - 14, x, rows_bottom), fill=YELLOW, width=2)

    if show_cost:
        y = rows_bottom + 66
        draw.line((40, y - 22, CONTENT_W - 40, y - 22), fill=GRID, width=1)
        read = facts.usage.cache_read_tokens or 0
        share = read / facts.usage.input_tokens if facts.usage.input_tokens else 0.0
        stats = [
            (f"${facts.usd:.2f}" if facts.usd is not None else "unpriced", f"paid, {facts.model}"),
            (
                f"{share:.0%}",
                f"of the {facts.usage.input_tokens:,} input tokens read from the cache",
            ),
            (
                f"${facts.usd_uncached:.2f}" if facts.usd_uncached is not None else "unpriced",
                "the same tokens with caching off",
            ),
        ]
        big = _ui_font(52, bold=True)
        column = (CONTENT_W - 80) // len(stats)
        for i, (value, label) in enumerate(stats):
            x = 40 + i * column
            draw.text((x, y), value, font=big, fill=DEFAULT)
            for j, line in enumerate(wrap(label, small, column - 30)):
                draw.text((x, y + 66 + j * 28), line, font=small, fill=DIM)
    return img


def timeline_shots(tl: Timeline, facts: Facts) -> list[Shot]:
    shots: list[Shot] = []
    sweep = (
        f"The trace of that request: {tl.total_s:.0f} s from plan to approval gate, "
        "drawn at its measured times."
    )
    frames = round(SWEEP_S * SWEEP_FPS)
    for i in range(frames + 1):
        now = tl.total_s * i / frames
        shots.append(
            Shot(
                chapter=2,
                caption=sweep,
                ms=round(1000 / SWEEP_FPS),
                draw=lambda now=now: draw_timeline(tl, facts, now, show_cost=False),
            )
        )
    done = tl.total_s + 1

    agents = [s for s in tl.spans if s.label in facts.parallel]
    if agents:
        spread_ms = (max(s.start_s for s in agents) - min(s.start_s for s in agents)) * 1000
        text = f"{_names([s.label for s in agents])} start together, within {spread_ms:.0f} ms."
        shots.append(
            Shot(2, text, 0, lambda: draw_timeline(tl, facts, done, show_cost=False)),
        )
    for agent, deps in facts.sequential:
        child = tl.span(agent)
        last = max(tl.span(d).end_s for d in deps)
        gap_ms = (child.start_s - last) * 1000
        text = (
            f"{_agent_name(agent)} starts {gap_ms:.0f} ms after {_names(deps)} ends: "
            "it waits for the data it needs, and for nothing else."
        )
        shots.append(Shot(2, text, 0, lambda: draw_timeline(tl, facts, done, show_cost=False)))
    if facts.usd is not None and facts.usd_uncached is not None:
        read = facts.usage.cache_read_tokens or 0
        share = read / facts.usage.input_tokens if facts.usage.input_tokens else 0.0
        text = (
            f"${facts.usd:.2f} for the whole request, priced from its own token counts. "
            f"{share:.0%} of the input came from the prompt cache."
        )
    else:
        text = "The request's tokens, as measured; this model has no listed price."
    shots.append(Shot(2, text, 0, lambda: draw_timeline(tl, facts, done, show_cost=True)))
    return shots


ARCHITECTURE_STOPS: tuple[tuple[float, str], ...] = (
    (
        0.0,
        "Live Prometheus metrics become the situation block. "
        "One HTTP request carries the operational query in.",
    ),
    (
        0.585,  # the window opens just above the Reasoning Layer frame
        "The planner picks the agents a query needs and writes each one's context. "
        "The executor runs independent agents in parallel and hands each dependent a digest.",
    ),
    (
        1.0,
        "Agents reach GitHub, release manifests, and Prometheus through custom MCP servers, "
        "and the incident corpus through hybrid retrieval. Every recommendation then passes "
        "a fail-closed gate into an append-only audit log.",
    ),
)
SIDE_W = CONTENT_W - 1384 - 40  # the panel beside the diagram at 1:1


def _diagram_card(diagram: Image.Image, offset_y: float) -> Image.Image:
    """The diagram at 1:1 through a window as tall as the content area, scrolled to
    `offset_y` (0 is the top, 1 the bottom)."""
    window_h = CONTENT_H
    span = max(diagram.height - window_h, 0)
    top = round(span * offset_y)
    card = Image.new("RGB", (diagram.width, window_h), (255, 255, 255))
    card.paste(diagram.crop((0, top, diagram.width, top + window_h)), (0, 0))
    mask = Image.new("L", card.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, card.width - 1, card.height - 1), 14, fill=255)
    out = Image.new("RGB", card.size, PAGE)
    out.paste(card, (0, 0), mask)
    return out


def _architecture_frame(
    diagram: Image.Image, offset_y: float, points: Sequence[str]
) -> Image.Image:
    img = Image.new("RGB", (CONTENT_W, CONTENT_H), PAGE)
    img.paste(_diagram_card(diagram, offset_y), (0, 0))
    draw = ImageDraw.Draw(img)
    font = _ui_font(26)
    x = 1384 + 40
    y = 10
    for n, point in enumerate(points):
        color = DEFAULT if n == len(points) - 1 else FAINT
        for line in wrap(point, font, SIDE_W - 10):
            draw.text((x, y), line, font=font, fill=color)
            y += 36
        y += 26
    return img


def _overview(diagram: Image.Image) -> Image.Image:
    scale = min(CONTENT_W / diagram.width, CONTENT_H / diagram.height)
    size = (round(diagram.width * scale), round(diagram.height * scale))
    small = diagram.resize(size, Image.Resampling.LANCZOS)
    mask = Image.new("L", small.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), 14, fill=255)
    out = Image.new("RGB", small.size, PAGE)
    out.paste(small, (0, 0), mask)
    return out


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def architecture_shots(diagram: Image.Image) -> list[Shot]:
    if diagram.width > CONTENT_W - SIDE_W or diagram.width < 1000:
        raise ValueError(f"the diagram is {diagram.width}px wide; the layout expects ~1384px")
    shots = [
        Shot(
            3,
            "How it fits together: one coordinator, four agents, a frozen contract between them.",
            0,
            lambda: _overview(diagram),
        )
    ]
    shown: list[str] = []
    previous = ARCHITECTURE_STOPS[0][0]
    for stop, point in ARCHITECTURE_STOPS:
        if stop != previous:
            frames = round(PAN_S * PAN_FPS)
            for i in range(1, frames + 1):
                y = previous + (stop - previous) * _ease(i / frames)
                points = tuple(shown)
                shots.append(
                    Shot(
                        3,
                        "",
                        round(1000 / PAN_FPS),
                        lambda y=y, points=points: _architecture_frame(diagram, y, points),
                    )
                )
        shown.append(point)
        points = tuple(shown)
        shots.append(
            Shot(
                3,
                "",
                reading_ms(point),
                lambda stop=stop, points=points: _architecture_frame(diagram, stop, points),
            )
        )
        previous = stop
    return shots


def end_card(run_day: str) -> Shot:
    def draw() -> Image.Image:
        img = Image.new("RGB", (CONTENT_W, CONTENT_H), PAGE)
        d = ImageDraw.Draw(img)
        title = _ui_font(64, bold=True)
        sub = _ui_font(30)
        lines = [
            ("AIOC - Enterprise AI Operations Center", title, DEFAULT),
            (
                "A coordinator, four agents, and a frozen contract, built as CCA-F evidence.",
                sub,
                DIM,
            ),
            (f"The terminal, the trace, and the bill are one recorded run, {run_day}.", sub, DIM),
        ]
        y = CONTENT_H // 2 - 110
        for text, font, color in lines:
            width = font.getlength(text)
            d.text(((CONTENT_W - width) / 2, y), text, font=font, fill=color)
            y += 100 if font is title else 50
        return img

    return Shot(3, "", 5000, draw)


# --------------------------------------------------------------------------------------
# Assembly and encoding
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Storyboard:
    shots: list[Shot]

    @property
    def total_ms(self) -> int:
        return sum(s.ms for s in self.shots)

    def chapter_starts_ms(self) -> list[int]:
        starts: list[int] = []
        elapsed = 0
        for s in self.shots:
            if len(starts) <= s.chapter:
                starts.append(elapsed)
            elapsed += s.ms
        return starts


def _read_json(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"{path} does not exist - is this a check_day25_service.py run?")
    return json.loads(path.read_text(encoding="utf-8"))


def _model(run_dir: Path) -> str:
    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
        data = json.loads(line).get("data") or {}
        if "model" in data:
            return str(data["model"])
    raise ValueError(f"{run_dir / 'events.jsonl'} names no model")


def build_storyboard(run_dir: Path, diagram_path: Path, *, transcript: str | None) -> Storyboard:
    name = transcript or (
        "transcript.replayed.json"
        if (run_dir / "transcript.replayed.json").is_file()
        else "transcript.json"
    )
    entries: list[dict[str, Any]] = _read_json(run_dir / name)
    run = _read_json(run_dir / "run.json")
    job = _read_json(run_dir / "job.json")
    response = _read_json(run_dir / "response.json")
    trace = _read_json(run_dir / "trace.json")
    lines = [str(e["line"]) for e in entries]
    facts = read_facts(lines, job, response, run["metadata"], _model(run_dir))
    timeline = read_timeline(trace)

    shots = terminal_shots(
        entries,
        facts,
        title="AIOC - four agents through the deployed service",
        command="uv run python scripts/check_day25_service.py --deploy",
    )
    shots += timeline_shots(timeline, facts)
    with Image.open(diagram_path) as img:
        diagram = img.convert("RGB")
    shots += architecture_shots(diagram)
    run_day = str(run.get("started_at") or response["received_at"])[:10]
    shots.append(end_card(run_day))
    hold_for_reading(shots)
    return Storyboard(shots)


def _ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found is None:
        raise FileNotFoundError(
            "ffmpeg is not on PATH (winget install Gyan.FFmpeg, brew install ffmpeg)"
        )
    return found


def render(board: Storyboard, out: Path, frames_dir: Path) -> None:
    frames_dir.mkdir(parents=True, exist_ok=True)
    total = board.total_ms
    elapsed = 0
    listing: list[str] = []
    last = ""
    for i, shot in enumerate(board.shots):
        name = f"f{i:05d}.png"
        frame = compose(shot.draw(), shot.chapter, shot.caption, elapsed / total)
        frame.save(frames_dir / name, optimize=False, compress_level=1)
        listing += [f"file '{name}'", f"duration {shot.ms / 1000:.3f}"]
        last = name
        elapsed += shot.ms
    # The concat demuxer drops the last entry's duration unless the file is listed again, and
    # then shows that file for its duration a second time; `-t` cuts the repeat back off.
    listing.append(f"file '{last}'")
    (frames_dir / "frames.txt").write_text("\n".join(listing) + "\n", encoding="utf-8")

    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - fixed arguments, our own files
        [
            _ffmpeg(),
            "-y",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(frames_dir / "frames.txt"),
            "-vf",
            "fps=30,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            "20",
            "-tune",
            "stillimage",
            "-movflags",
            "+faststart",
            "-t",
            f"{total / 1000:.3f}",
            str(out),
        ],
        check=True,
    )


def _clock(ms: int) -> str:
    seconds = round(ms / 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="a check_day25_service.py run directory")
    parser.add_argument("--out", default="docs/assets/demo.mp4")
    parser.add_argument("--transcript", help="transcript file in the run directory")
    parser.add_argument("--diagram", default=str(_REPO_ROOT / "docs/assets/architecture.png"))
    parser.add_argument("--frames", help="keep the rendered PNG frames in this directory")
    parser.add_argument("--plan", action="store_true", help="print the chapter timings only")
    args = parser.parse_args(argv)

    try:
        board = build_storyboard(Path(args.run), Path(args.diagram), transcript=args.transcript)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2

    starts = board.chapter_starts_ms() + [board.total_ms]
    for i, name in enumerate(CHAPTERS):
        print(f"  {_clock(starts[i])}-{_clock(starts[i + 1])}  {name}")
    print(f"  {len(board.shots)} shots, {_clock(board.total_ms)} in all")
    if args.plan:
        return 0

    out = Path(args.out)
    try:
        if args.frames:
            render(board, out, Path(args.frames))
        else:
            with tempfile.TemporaryDirectory() as tmp:
                render(board, out, Path(tmp))
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"{out}  ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
