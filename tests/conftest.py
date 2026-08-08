"""Test bootstrap: make `src/aioc` importable, and record every run under `test-results/`.

The path insert keeps `python -m pytest` working without an install. The recorder hooks below
mean a plain `uv run pytest` leaves a structured, queryable record of what ran and what failed -
see `test-results/README.md` for the schema. Set `AIOC_RUNLOG=0` to switch recording off.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# runlog lives in scripts/ rather than the shipped package - it is dev tooling, not product code.
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from runlog import RunRecorder  # noqa: E402 - must follow the sys.path insert above

_RECORDER: RunRecorder | None = None

# pytest reports three phases per test; `call` is the actual test body. A failure in `setup` or
# `teardown` never reaches `call`, so those are recorded too or a broken fixture would look like
# a run in which nothing happened.
_PHASES = ("setup", "call", "teardown")


def _enabled() -> bool:
    return os.environ.get("AIOC_RUNLOG", "1") not in {"0", "false", "no"}


def pytest_sessionstart(session: pytest.Session) -> None:
    global _RECORDER
    if not _enabled():
        return
    # `-m 'not integration'` and friends change what a run means, so the selection is part of
    # the record; without it two runs with different totals look like a regression.
    name = str(session.config.getoption("-m") or "all") or "all"
    _RECORDER = RunRecorder(
        kind="pytest",
        name=name,
        command="pytest " + " ".join(session.config.invocation_params.args),
        metadata={"markexpr": session.config.getoption("-m") or None, "rootdir": str(_ROOT)},
    )


def pytest_runtest_logreport(report: Any) -> None:
    if _RECORDER is None or report.when not in _PHASES:
        return
    # A passing setup/teardown is noise; only the call phase is worth a record on success.
    if report.when != "call" and report.passed:
        return

    outcome = "passed" if report.passed else "skipped" if report.skipped else "failed"
    if report.when != "call" and report.failed:
        outcome = "error"  # a fixture blew up, the test never ran

    path, lineno, _ = report.location
    _RECORDER.event(
        report.nodeid,
        outcome=outcome,  # type: ignore[arg-type]
        duration_ms=report.duration * 1000,
        type="test",
        data={
            "phase": report.when,
            "file": path,
            "line": None if lineno is None else lineno + 1,
            "markers": sorted(_markers(report)),
        },
        message=_first_line(report.longreprtext) if not report.passed else None,
        detail=report.longreprtext or None,
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _RECORDER is None:
        return
    _RECORDER.set_outcome(
        "passed" if exitstatus == 0 else "failed",
        exit_code=int(exitstatus),
    )
    where = _RECORDER.finish()
    print(f"\n[runlog] {where.relative_to(_ROOT)}")


def _markers(report: Any) -> set[str]:
    # report.keywords holds the nodeid parts as well as markers; the parts are noisy, so keep
    # only the markers this project actually declares.
    declared = {"integration"}
    return {key for key in getattr(report, "keywords", {}) if key in declared}


def _first_line(text: str | None) -> str | None:
    if not text:
        return None
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:500]
    return None
