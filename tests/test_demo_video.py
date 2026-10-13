"""Day 28 demo video, offline: the trace read into a timeline, the facts every caption states,
the captions' claims checked against what the agents said, the reading holds, and a storyboard
built end to end from a synthetic run directory. Nothing is encoded - ffmpeg is the script's,
and CI does not have it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageFont
from render_demo_gif import FONT_SIZE, frame_size, load_font, terminal_frames
from render_demo_video import (
    CAPTION_LINES,
    CAPTION_MIN_MS,
    CAPTION_MS_PER_CHAR,
    CHAPTERS,
    CONTENT_H,
    CONTENT_W,
    TERMINAL_COLS,
    TERMINAL_FONT,
    TERMINAL_LEADING,
    TERMINAL_ROWS,
    H,
    Shot,
    W,
    build_storyboard,
    caption_for,
    compose,
    draw_timeline,
    hold_for_reading,
    main,
    read_facts,
    read_timeline,
    terminal_font,
)

T0 = "2026-10-10T03:56:39.704Z"


def _obs(
    oid: str,
    name: str,
    start: str,
    end: str | None,
    *,
    kind: str = "SPAN",
    parent: str | None = "root",
    **meta: Any,
) -> dict[str, Any]:
    return {
        "id": oid,
        "name": name,
        "type": kind,
        "startTime": start,
        "endTime": end,
        "parentObservationId": parent,
        "metadata": meta,
    }


def _trace() -> dict[str, Any]:
    return {
        "id": "ac21f86e1a369f336f3727510e9b4d05",
        "observations": [
            _obs(
                "root",
                "coordinator_request",
                T0,
                "2026-10-10T03:58:27.781Z",
                kind="CHAIN",
                parent=None,
            ),
            _obs(
                "p",
                "plan",
                "2026-10-10T03:56:39.705Z",
                "2026-10-10T03:56:53.515Z",
                input_tokens=4947,
                output_tokens=1508,
            ),
            _obs(
                "g",
                "agent:github",
                "2026-10-10T03:56:53.517Z",
                "2026-10-10T03:57:42.079Z",
                kind="AGENT",
                invocation_id="inv_github",
                input_tokens=85391,
                output_tokens=4399,
            ),
            _obs(
                "i",
                "agent:incident",
                "2026-10-10T03:56:53.519Z",
                "2026-10-10T03:57:37.171Z",
                kind="AGENT",
                invocation_id="inv_incident",
                input_tokens=19712,
                output_tokens=5688,
            ),
            _obs(
                "d",
                "agent:deployment",
                "2026-10-10T03:57:42.081Z",
                "2026-10-10T03:58:27.727Z",
                kind="AGENT",
                invocation_id="inv_deployment",
                input_tokens=42666,
                output_tokens=4410,
            ),
            # Recorded when the agent returned; the call itself ran at `started_at`.
            _obs(
                "t1",
                "tool:get_pull_request",
                "2026-10-10T03:57:42.078Z",
                None,
                kind="EVENT",
                parent="g",
                started_at="2026-10-10T03:56:57.301019+00:00",
                duration_ms=1669,
                ok=True,
            ),
            _obs(
                "t2",
                "tool:diff_release",
                "2026-10-10T03:58:27.725Z",
                None,
                kind="EVENT",
                parent="d",
                started_at="2026-10-10T03:57:46.708711+00:00",
                duration_ms=2439,
                ok=False,
            ),
            _obs(
                "h",
                "hitl_gate",
                "2026-10-10T03:58:27.729Z",
                "2026-10-10T03:58:27.781Z",
                input_tokens=0,
                output_tokens=0,
            ),
        ],
    }


TRANSCRIPT = [
    "[1/6] The release under test: PR #11, 884b0b645f -> c729c7220d",
    "[2/6] Breaking production: injecting downstream_latency",
    "      set   payments-api  -> extra_latency_ms=800",
    "    Probing checkout-api for user-visible impact...",
    "      checkout-api/process x8: statuses={'200': 7, '502': 1} mean=814ms",
    "      waiting 45s for Prometheus rate() windows to fill...",
    "[3/6] Live metrics + what the on-call knows -> the situation block",
    "[4/6] POST http://localhost:8080/v1/requests  (mode live)",
    "        0.0s  running",
    "      111.4s  succeeded",
    "[5/6] The coordinator's answer, as the service returned it",
    "  selected    github      parallel",
    "  -- incident agent (partial, confidence 0.55) tools: none",
    "     payments-api is the latency source.",
    "  -- docs agent (partial, confidence 0.55) tools: search_corpus[ok]",
    "  answer (0.85): PR #11 is an observability fix.",
    "  gate        denied        incident: restart payments-api",
    "[6/6] Reading the trace back from Langfuse",
    "--- a real fault, four agents, the deployed service: PASS ---",
]


def _response(*, incident: str = "payments-api is the latency source.") -> dict[str, Any]:
    def inv(agent: str, deps: list[str]) -> dict[str, Any]:
        return {"invocation_id": f"inv_{agent}", "agent": agent, "depends_on": deps, "round": 0}

    return {
        "received_at": "2026-10-10T03:56:39Z",
        "selected_agents": [
            inv("github", []),
            inv("deployment", ["inv_github"]),
            inv("incident", []),
            inv("docs", []),
            # A refinement round is not the plan and never reaches the captions.
            {**inv("docs", []), "invocation_id": "inv_docs_r1", "round": 1},
        ],
        "agent_responses": [
            {"agent": "github", "summary": "PR #11 is an observability fix."},
            {"agent": "incident", "summary": incident},
        ],
        "answer": {"value": "PR #11 is an observability fix.", "confidence": 0.85},
        "cost": {
            "input_tokens": 161164,
            "output_tokens": 19268,
            "cache_read_tokens": 90451,
            "cache_write_tokens": 62176,
        },
    }


def _job(decisions: list[str]) -> dict[str, Any]:
    return {"approvals": [{"decision": d} for d in decisions]}


def _facts(**overrides: Any) -> Any:
    response = overrides.pop("response", _response())
    job = overrides.pop("job", _job(["denied", "denied", "denied"]))
    model = overrides.pop("model", "claude-sonnet-5")
    return read_facts(TRANSCRIPT, job, response, {"service": "checkout-api", "pr": 11}, model)


# ---------------------------------------------------------------------- the timeline


def test_spans_are_seconds_from_the_request_start_in_start_order() -> None:
    tl = read_timeline(_trace())
    assert tl.total_s == pytest.approx(108.077)
    assert [s.label for s in tl.spans] == ["plan", "github", "incident", "deployment", "hitl_gate"]
    github = tl.span("github")
    assert github.start_s == pytest.approx(13.813)
    assert (github.input_tokens, github.output_tokens) == (85391, 4399)
    assert github.invocation_id == "inv_github"


def test_a_tool_call_sits_where_it_ran_not_where_it_was_recorded() -> None:
    tl = read_timeline(_trace())
    pr = next(t for t in tl.tools if t.name == "get_pull_request")
    assert pr.span == "github"
    assert pr.start_s == pytest.approx(17.597, abs=1e-3)  # not 62.374, its event's timestamp
    assert pr.duration_s == pytest.approx(1.669)
    assert next(t for t in tl.tools if t.name == "diff_release").ok is False


def test_a_trace_without_one_root_is_refused() -> None:
    trace = _trace()
    trace["observations"][1]["parentObservationId"] = None
    with pytest.raises(ValueError, match="one root"):
        read_timeline(trace)


# ---------------------------------------------------------------------- the facts


def test_facts_are_read_from_the_run() -> None:
    f = _facts()
    assert (f.fault, f.fault_service, f.fault_knob) == (
        "downstream_latency",
        "payments-api",
        "extra_latency_ms=800",
    )
    assert f.probe == "7 of 8 calls succeed, averaging 814 ms"
    assert f.job_s == pytest.approx(111.4)
    assert f.parallel == ("github", "incident", "docs")
    assert f.sequential == (("deployment", ("github",)),)
    assert f.adopted == "github"
    assert f.decisions == {"denied": 3}


def test_the_bill_is_priced_from_the_response_tokens() -> None:
    f = _facts()
    # 8,537 uncached at $2, 62,176 written at 1.25x, 90,451 read at $0.20, 19,268 out at $10.
    expected = (8537 * 2 + 62176 * 2.5 + 90451 * 0.2 + 19268 * 10) / 1e6
    assert f.usd == pytest.approx(expected)
    assert f.usd_uncached == pytest.approx((161164 * 2 + 19268 * 10) / 1e6)
    assert _facts(model="some-unlisted-model").usd is None


def test_a_transcript_of_another_shape_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="fault injection"):
        read_facts(["nothing here"], _job([]), _response(), {"service": "x", "pr": 1}, "m")


# ---------------------------------------------------------------------- the captions


def _caption(line: str, facts: Any) -> str:
    return caption_for(line, facts, "unchanged")


def test_captions_change_on_their_cue_lines_and_hold_otherwise() -> None:
    f = _facts()
    assert "800 ms of extra latency" in _caption(TRANSCRIPT[1], f)
    assert "7 of 8 calls succeed" in _caption(TRANSCRIPT[3], f)
    assert "111 s" in _caption(TRANSCRIPT[7], f)
    assert _caption("      checkout-api is running c729c7220d", f) == "unchanged"


def test_the_selection_caption_states_the_plan() -> None:
    text = _caption(TRANSCRIPT[11], _facts())
    assert "GitHub, Incident and Docs run in parallel" in text
    assert "Deployment runs after GitHub" in text


def test_incident_is_credited_with_the_injected_service_only_when_it_named_it() -> None:
    assert "names payments-api" in _caption(TRANSCRIPT[12], _facts())
    missed = _facts(response=_response(incident="checkout-api is slow; cause unknown."))
    assert "payments-api" not in _caption(TRANSCRIPT[12], missed)


def test_the_answer_caption_names_the_adopted_report_only_when_there_is_one() -> None:
    assert "here GitHub's" in _caption(TRANSCRIPT[15], _facts())
    response = _response()
    response["answer"]["value"] = "A combined answer no agent wrote."
    assert "GitHub" not in _caption(TRANSCRIPT[15], _facts(response=response))


def test_the_gate_caption_says_all_denied_only_when_all_were() -> None:
    assert "denies all three" in _caption(TRANSCRIPT[16], _facts())
    mixed = _facts(job=_job(["denied", "not_required"]))
    assert "denies all" not in _caption(TRANSCRIPT[16], mixed)


def _shot(caption: str, ms: int) -> Shot:
    return Shot(chapter=0, caption=caption, ms=ms, draw=lambda: Image.new("RGB", (10, 10)))


def test_a_caption_holds_for_its_reading_time_and_only_its_last_shot_grows() -> None:
    caption = "x" * 40
    need = CAPTION_MIN_MS + CAPTION_MS_PER_CHAR * 40
    shots = [_shot(caption, 100), _shot(caption, 100), _shot("", 50), _shot("long enough", 99_999)]
    hold_for_reading(shots)
    assert [s.ms for s in shots] == [100, need - 100, 50, 99_999]


# ---------------------------------------------------------------------- drawing


def test_terminal_frames_at_the_gif_defaults_match_the_gif_geometry() -> None:
    entries = [{"t": 0.0, "line": "hello"}, {"t": 1.5, "line": "world"}]
    frames, durations = terminal_frames(entries, title="t", command="c", font=load_font())
    assert len(frames) == 3 and durations[0] == 900 and durations[2] == 1500
    # The GIF's own size: COLS of Consolas at FONT_SIZE plus padding; a larger font scales it.
    big, _ = terminal_frames(entries, title="t", command="c", font=load_font(FONT_SIZE * 2))
    assert big[0].width > frames[0].width * 1.8


def test_frame_size_is_the_size_terminal_frames_draws() -> None:
    entries = [{"t": 0.0, "line": "x" * 300}]
    for font in (load_font(), load_font(TERMINAL_FONT), ImageFont.load_default(30)):
        frames, _ = terminal_frames(
            entries, title="t", command="c", font=font, cols=90, rows=12, leading=9
        )  # type: ignore[arg-type]
        assert frames[0].size == frame_size(font, cols=90, rows=12, leading=9)  # type: ignore[arg-type]


def _fits(font: ImageFont.FreeTypeFont) -> bool:
    w, h = frame_size(font, cols=TERMINAL_COLS, rows=TERMINAL_ROWS, leading=TERMINAL_LEADING)
    return w <= CONTENT_W and h <= CONTENT_H


def test_the_terminal_font_fits_whatever_monospace_face_the_machine_has(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    font = terminal_font()
    assert _fits(font) and font.size <= TERMINAL_FONT
    # A runner with no monospace font at all (CI's first Day 28 run): PIL's default face is
    # wider than Consolas, and the terminal shot overflowed the frame until it was stepped down.
    monkeypatch.setattr(
        "render_demo_gif.load_font", lambda size=FONT_SIZE: ImageFont.load_default(size)
    )
    monkeypatch.setattr(
        "render_demo_video.load_font", lambda size=FONT_SIZE: ImageFont.load_default(size)
    )
    wide = terminal_font()
    assert _fits(wide) and wide.size < TERMINAL_FONT
    assert not _fits(ImageFont.load_default(wide.size + 1))  # type: ignore[arg-type]


def test_a_frame_is_1080p_and_refuses_a_caption_that_does_not_fit() -> None:
    content = Image.new("RGB", (CONTENT_W, CONTENT_H))
    assert compose(content, 0, "A caption.", 0.5).size == (W, H)
    with pytest.raises(ValueError, match=f"at most {CAPTION_LINES}"):
        compose(content, 0, "word " * 200, 0.5)
    with pytest.raises(ValueError, match="does not fit"):
        compose(Image.new("RGB", (CONTENT_W + 1, 10)), 0, "", 0.0)


def test_the_timeline_draws_only_what_has_happened_by_now() -> None:
    tl = read_timeline(_trace())
    f = _facts()
    early = draw_timeline(tl, f, 1.0, show_cost=False)
    late = draw_timeline(tl, f, tl.total_s + 1, show_cost=True)
    assert early.size == late.size == (CONTENT_W, CONTENT_H)
    assert early.tobytes() != late.tobytes()


# ---------------------------------------------------------------------- end to end


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    run.mkdir()
    lines = ["=" * 72, *TRANSCRIPT]
    (run / "transcript.json").write_text(
        json.dumps([{"t": i * 0.5, "line": line} for i, line in enumerate(lines)]), "utf-8"
    )
    (run / "run.json").write_text(
        json.dumps(
            {
                "started_at": "2026-10-10T03:58:31Z",
                "metadata": {"service": "checkout-api", "pr": 11},
            }
        ),
        "utf-8",
    )
    (run / "job.json").write_text(json.dumps(_job(["denied"])), "utf-8")
    (run / "response.json").write_text(json.dumps(_response()), "utf-8")
    (run / "trace.json").write_text(json.dumps(_trace()), "utf-8")
    (run / "events.jsonl").write_text(
        json.dumps({"data": {"model": "claude-sonnet-5"}}) + "\n", "utf-8"
    )
    return run


def _diagram(tmp_path: Path) -> Path:
    path = tmp_path / "architecture.png"
    Image.new("RGB", (1384, 1469), (255, 255, 255)).save(path)
    return path


def test_the_storyboard_runs_the_four_chapters_in_order_and_every_shot_composes(
    tmp_path: Path,
) -> None:
    board = build_storyboard(_run_dir(tmp_path), _diagram(tmp_path), transcript=None)
    chapters = [s.chapter for s in board.shots]
    assert chapters == sorted(chapters)
    assert set(chapters) == set(range(len(CHAPTERS)))
    assert len(board.chapter_starts_ms()) == len(CHAPTERS)
    # The terminal splits at the request: step 4 opens the second chapter.
    first_of_two = next(s for s in board.shots if s.chapter == 1)
    assert "One request to the deployed service" in first_of_two.caption
    for shot in board.shots[:: max(1, len(board.shots) // 12)] + board.shots[-3:]:
        assert compose(shot.draw(), shot.chapter, shot.caption, 0.0).size == (W, H)


def test_the_plan_flag_prints_timings_and_encodes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "demo.mp4"
    code = main(
        [
            "--run",
            str(_run_dir(tmp_path)),
            "--diagram",
            str(_diagram(tmp_path)),
            "--out",
            str(out),
            "--plan",
        ]
    )
    assert code == 0 and not out.exists()
    printed = capsys.readouterr().out
    assert all(name in printed for name in CHAPTERS)


def test_a_missing_record_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = _run_dir(tmp_path)
    (run / "trace.json").unlink()
    assert main(["--run", str(run), "--diagram", str(_diagram(tmp_path)), "--plan"]) == 2
    assert "trace.json" in capsys.readouterr().err
