# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow>=10"]
# ///
"""Render a Day 10 demo transcript into the checkpoint GIF.

    uv run scripts/render_demo_gif.py --run test-results/runs/<date>/<run-dir>
    uv run scripts/render_demo_gif.py --run <run-dir> --out docs/assets/day10-demo.gif

Free - no API calls, no network. Input is the `transcript.json` artifact that
`scripts/demo_day10.py` records (every printed line with its wall-clock offset); output is
an animated terminal replay. It is the *real* transcript re-rendered, not a re-enactment:
line content and order are untouched, and only the dead time is compressed (a 40s model
call becomes a ~2s pause, because nobody watches a GIF buffer).

Pillow is declared as PEP 723 inline script metadata, so `uv run` provisions it on the fly
and the project's own dependency tree stays clean - this is a dev-tooling concern, not a
shipped one.
"""

from __future__ import annotations

import argparse
import json
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


def _color_for(line: str) -> tuple[int, int, int]:
    s = line.strip()
    if line.startswith("===") or s.startswith("AIOC") or s.startswith("Day 10:"):
        return BLUE
    if (
        s.startswith("[1/4]")
        or s.startswith("[2/4]")
        or s.startswith("[3/4]")
        or s.startswith("[4/4]")
    ):
        return YELLOW
    if s.startswith("selected"):
        return GREEN
    if s.startswith("skipped"):
        return DIM
    if s.startswith("--"):
        return BLUE
    if s.startswith("claim ["):
        return CYAN
    if s.startswith(("intent", "failure_mode:", "affected:", "answer (", "status ", "trace ")):
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


def _load_font() -> ImageFont.FreeTypeFont:
    for name in ("consola.ttf", "CascadiaMono.ttf", "cour.ttf"):
        try:
            return ImageFont.truetype(name, FONT_SIZE)
        except OSError:
            continue
    print("no monospace TrueType font found; falling back to PIL default", file=sys.stderr)
    return ImageFont.load_default(FONT_SIZE)  # type: ignore[return-value]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[4])
    parser.add_argument("--run", required=True, help="run directory holding transcript.json")
    parser.add_argument("--out", default="docs/assets/day10-demo.gif")
    args = parser.parse_args(argv)

    transcript_path = Path(args.run) / "transcript.json"
    if not transcript_path.is_file():
        print(f"{transcript_path} does not exist - did the demo run record it?", file=sys.stderr)
        return 2
    entries = json.loads(transcript_path.read_text(encoding="utf-8"))

    font = _load_font()
    box = font.getbbox("M")
    char_w, line_h = box[2] - box[0], (box[3] - box[1]) + 7
    width = PAD_X * 2 + COLS * char_w
    height = PAD_TOP + ROWS * line_h + PAD_BOTTOM

    # The typed command opens the replay; everything after it is the recorded transcript.
    rows: list[tuple[str, tuple[int, int, int]]] = [
        ("$ uv run python scripts/demo_day10.py", GREEN)
    ]
    frames: list[Image.Image] = []
    durations: list[int] = []

    def snapshot(duration_ms: int) -> None:
        img = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, width, PAD_TOP - 14), fill=BAR)
        for i, dot in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
            draw.ellipse((PAD_X + i * 22, 9, PAD_X + 12 + i * 22, 21), fill=dot)
        draw.text((width // 2 - 90, 7), "AIOC - Day 10 demo", font=font, fill=TITLE)
        for row, (text, color) in enumerate(rows[-ROWS:]):
            draw.text((PAD_X, PAD_TOP + row * line_h), text, font=font, fill=color)
        frames.append(img)
        durations.append(duration_ms)

    snapshot(900)
    previous_t = 0.0
    for entry in entries:
        gap_ms = int((float(entry["t"]) - previous_t) * 1000)
        previous_t = float(entry["t"])
        color = _color_for(entry["line"])
        for wrapped in _wrap(entry["line"], COLS):
            rows.append((wrapped, color))
        # The whole answer section prints in one burst (gap ~0), which would flash past
        # unreadably - so a line holds for the larger of its real gap and its reading time.
        read_ms = MIN_FRAME_MS + READ_MS_PER_CHAR * len(entry["line"].strip())
        snapshot(min(MAX_FRAME_MS, max(gap_ms, read_ms)))
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
