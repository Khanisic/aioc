# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=10"]
# ///
"""Render a recorded demo transcript into a checkpoint GIF.

    uv run scripts/render_demo_gif.py --run test-results/runs/<date>/<run-dir>
    uv run scripts/render_demo_gif.py --run <run-dir> --out docs/assets/day10-demo.gif

`--title` and `--command` set the window title and the command typed at the top of the
replay; the defaults are Day 10's, and the Day 15 checkpoint GIF passes its own.

Free - no API calls, no network. Input is the `transcript.json` artifact that
`scripts/demo_day10.py` and `scripts/check_day15_integration.py` record (every printed line
with its wall-clock offset); output is an animated terminal replay. It is the *real*
transcript re-rendered, not a re-enactment: line content and order are untouched, and only
the dead time is compressed (a 40s model call becomes a ~2s pause, because nobody watches a
GIF buffer).

Pillow is declared as PEP 723 inline script metadata, so `uv run` provisions it on the fly
and the project's own dependency tree stays clean - this is a dev-tooling concern, not a
shipped one.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLS = 104  # wrap width in characters
ROWS = 32  # viewport height in rows
FONT_SIZE = 15
PAD_X = 18
PAD_TOP = 44  # leaves room for the window title bar
PAD_BOTTOM = 16

MIN_FRAME_MS = 90  # a line never flashes faster than this
READ_MS_PER_CHAR = 6  # lines that printed in one burst still reveal at reading pace
MAX_FRAME_MS = 2200  # and a 40s model call compresses to this
END_HOLD_MS = 7000  # hold the finished screen long enough to read the answer
BG = (13, 17, 23)
BAR = (22, 27, 34)
DEFAULT = (230, 237, 243)
DIM = (139, 148, 158)
BLUE = (88, 166, 255)
GREEN = (126, 231, 135)
YELLOW = (227, 179, 65)
CYAN = (121, 192, 255)
TITLE = (201, 209, 217)

_DAY_HEADING = re.compile(r"Day \d+:")
_STEP = re.compile(r"\[\d+/\d+\]")  # "[2/5] Injecting a real fault"


def _color_for(line: str) -> tuple[int, int, int]:
    s = line.strip()
    if line.startswith("===") or s.startswith("AIOC") or _DAY_HEADING.match(s):
        return BLUE
    if _STEP.match(s):
        return YELLOW
    if s.startswith(("selected", "overlapped", "--- ")):
        return GREEN
    if s.startswith("skipped"):
        return DIM
    if s.startswith("--"):
        return BLUE
    if s.startswith("claim ["):
        return CYAN
    if s.startswith(
        ("intent", "failure_mode:", "affected:", "answer (", "status ", "trace ", "synthesis")
    ):
        return YELLOW
    if line.startswith("    "):  # injector output and the metrics block
        return DIM
    return DEFAULT


def _wrap(line: str, cols: int) -> list[str]:
    if len(line) <= cols:
        return [line]
    indent = " " * (len(line) - len(line.lstrip(" ")) + 2)
    out: list[str] = []
    current = line
    while len(current) > cols:
        cut = current.rfind(" ", len(indent) + 8, cols)
        if cut <= 0:
            cut = cols
        out.append(current[:cut])
        current = indent + current[cut:].lstrip(" ")
    out.append(current)
    return out


def load_font(size: int = FONT_SIZE) -> ImageFont.FreeTypeFont:
    for name in ("consola.ttf", "CascadiaMono.ttf", "cour.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    print("no monospace TrueType font found; falling back to PIL default", file=sys.stderr)
    return ImageFont.load_default(size)  # type: ignore[return-value]


def terminal_frames(
    entries: list[dict[str, object]],
    *,
    title: str,
    command: str,
    font: ImageFont.FreeTypeFont,
    cols: int = COLS,
    rows: int = ROWS,
    leading: int = 7,
) -> tuple[list[Image.Image], list[int]]:
    """The transcript as terminal frames and how long each holds, in milliseconds.

    Frame 0 is the typed command alone; frame ``i`` is the screen after ``entries[i - 1]``
    printed. `scripts/render_demo_video.py` cuts the same frames into chapters."""
    box = font.getbbox("M")
    line_h = (box[3] - box[1]) + leading
    # A full row measured as drawn, not the ink box of one glyph times the columns: Consolas
    # at 15px advances 8.25px a character against an 8px ink box, so a full row overran the
    # image and its last character or two were clipped.
    # The window chrome scales with the font; at FONT_SIZE every number is the GIF's own.
    scale = font.size / FONT_SIZE

    def px(n: int) -> int:
        return round(n * scale)

    pad_x, pad_top = px(PAD_X), px(PAD_TOP)
    width = pad_x * 2 + math.ceil(font.getlength("M" * cols))
    height = pad_top + rows * line_h + px(PAD_BOTTOM)

    # The typed command opens the replay; everything after it is the recorded transcript.
    shown: list[tuple[str, tuple[int, int, int]]] = [(f"$ {command}", GREEN)]
    frames: list[Image.Image] = []
    durations: list[int] = []

    def snapshot(duration_ms: int) -> None:
        img = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, width, pad_top - px(14)), fill=BAR)
        for i, color in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            left = pad_x + px(22 * i)
            draw.ellipse((left, px(9), left + px(12), px(21)), fill=color)
        title_w = int(draw.textlength(title, font=font))
        draw.text(((width - title_w) // 2, px(7)), title, font=font, fill=TITLE)
        for row, (text, color) in enumerate(shown[-rows:]):
            draw.text((pad_x, pad_top + row * line_h), text, font=font, fill=color)
        frames.append(img)
        durations.append(duration_ms)

    snapshot(900)
    previous_t = 0.0
    for entry in entries:
        line = str(entry["line"])
        t = float(str(entry["t"]))
        gap_ms = int((t - previous_t) * 1000)
        previous_t = t
        color = _color_for(line)
        for wrapped in _wrap(line, cols):
            shown.append((wrapped, color))
        # The whole answer section prints in one burst (gap ~0), which would flash past
        # unreadably - so a line holds for the larger of its real gap and its reading time.
        read_ms = MIN_FRAME_MS + READ_MS_PER_CHAR * len(line.strip())
        snapshot(min(MAX_FRAME_MS, max(gap_ms, read_ms)))
    return frames, durations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", required=True, help="run directory holding transcript.json")
    parser.add_argument("--out", default="docs/assets/day10-demo.gif")
    parser.add_argument(
        "--transcript",
        default="transcript.json",
        help="the transcript file in the run directory (check_day25_service.py --replay "
        "writes transcript.replayed.json)",
    )
    parser.add_argument("--title", default="AIOC - Day 10 demo", help="window title bar text")
    parser.add_argument(
        "--command",
        default="uv run python scripts/demo_day10.py",
        help="the command typed at the top of the replay",
    )
    args = parser.parse_args(argv)

    transcript_path = Path(args.run) / args.transcript
    if not transcript_path.is_file():
        print(f"{transcript_path} does not exist - did the demo run record it?", file=sys.stderr)
        return 2
    entries = json.loads(transcript_path.read_text(encoding="utf-8"))

    frames, durations = terminal_frames(
        entries, title=args.title, command=args.command, font=load_font()
    )
    durations[-1] = END_HOLD_MS

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        out,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    total_s = sum(durations) / 1000
    print(f"{out}  ({len(frames)} frames, {total_s:.1f}s, {out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
