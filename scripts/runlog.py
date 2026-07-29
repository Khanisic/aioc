"""Structured run logging - every test or check, recorded as queryable JSON.

Two ways in:

*As a library*, for anything that produces its own results (the pytest plugin in
``tests/conftest.py``, the live LLM harness, a chaos smoke)::

    from runlog import RunRecorder

    with RunRecorder(kind="llm", name="structured-output") as run:
        run.event("diagnose", outcome="passed", duration_ms=1840, data={"model": "..."})

*As a CLI wrapper*, for any command whose exit code is the result::

    uv run python scripts/runlog.py --kind lint --name ruff -- uv run ruff check .

Layout it writes (``test-results/`` - see that directory's README for the record schema)::

    test-results/
      index.jsonl                     one line per run, newest appended - the query surface
      runs/2026-07-29/151230Z__llm__structured-output/
        run.json                      the run summary
        events.jsonl                  one line per test / step
        stdout.log, stderr.log        only for CLI-wrapped commands

Everything under ``runs/`` and the index are gitignored: they are machine-local evidence, not
source. Promote a run to ``evaluations/`` if it ever needs to be committed.

Why JSONL for events and JSON for the summary: events are appended one at a time and must
survive a crash mid-run (a truncated JSONL still parses line-by-line; a truncated JSON array
does not), while the summary is written once at the end and is easier to read as an object.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = REPO_ROOT / "test-results"
INDEX_FILE = RESULTS_ROOT / "index.jsonl"

# The outcome vocabulary is closed on purpose: an open string set makes the index unqueryable
# after a few weeks. `error` is distinct from `failed` - a failed assertion is a result, an
# error is the run not reaching a result (the same distinction pytest draws).
Outcome = Literal["passed", "failed", "error", "skipped"]

_SCHEMA_VERSION = "1.0.0"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    """RFC 3339 with an explicit Z, matching the contract's timestamp primitive (sec 1)."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _git_context() -> dict[str, Any]:
    """Best-effort git provenance. A run is worthless as evidence if you cannot tell which
    commit produced it - and `dirty` is the difference between reproducible and anecdotal."""

    def _run(*args: str) -> str | None:
        git = shutil.which("git")
        if git is None:
            return None
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [git, *args],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    status = _run("status", "--porcelain")
    return {
        "branch": _run("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": _run("rev-parse", "HEAD"),
        "dirty": None if status is None else bool(status),
    }


def _env_context() -> dict[str, Any]:
    """The knobs that plausibly change a result. Values are recorded, never secrets - the model
    name matters for reproducing an LLM run, the API key must never reach disk."""
    return {
        "python": platform.python_version(),
        "platform": sys.platform,
        "aioc_model": os.environ.get("AIOC_MODEL"),
        "aioc_llm_effort": os.environ.get("AIOC_LLM_EFFORT"),
        # Presence only, never the value. Named for what it actually measures: a key loaded from
        # .env by pydantic-settings is not in os.environ, so `false` here does not mean "no key".
        "anthropic_api_key_in_shell_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }


class RunRecorder:
    """One run: a directory holding a summary, an event stream, and optional raw output.

    Use it as a context manager. The summary is written on exit even if the body raised, so a
    crashed run still leaves evidence - which is exactly the run you most want to inspect.
    """

    def __init__(
        self,
        *,
        kind: str,
        name: str,
        command: str | None = None,
        metadata: dict[str, Any] | None = None,
        results_root: Path | None = None,
    ) -> None:
        self.kind = kind
        self.name = name
        self.command = command
        self.metadata = dict(metadata or {})
        self._root = results_root or RESULTS_ROOT

        self.started = _utcnow()
        self._monotonic_start = time.monotonic()
        self.run_id = f"{self.started.strftime('%Y%m%dT%H%M%SZ')}__{kind}__{_slug(name)}"

        self.dir = self._root / "runs" / self.started.strftime("%Y-%m-%d") / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"

        self._seq = 0
        self._counts: dict[str, int] = {}
        self._outcome: Outcome | None = None
        self._exit_code: int | None = None

    # -- recording ---------------------------------------------------------------------

    def event(
        self,
        name: str,
        *,
        outcome: Outcome,
        duration_ms: float | None = None,
        type: str = "step",
        data: dict[str, Any] | None = None,
        message: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Append one record. ``detail`` is for the long form (a traceback, a full response);
        ``message`` is the one-line version that stays readable in a terminal."""
        self._seq += 1
        self._counts[outcome] = self._counts.get(outcome, 0) + 1
        record = {
            "ts": _stamp(_utcnow()),
            "seq": self._seq,
            "type": type,
            "name": name,
            "outcome": outcome,
            "duration_ms": None if duration_ms is None else round(duration_ms, 3),
            "message": message,
            "detail": detail,
            "data": data or {},
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def artifact(self, filename: str, content: str) -> Path:
        """Drop raw output next to the run (a captured stdout, a full JSON response body)."""
        path = self.dir / filename
        path.write_text(content, encoding="utf-8")
        return path

    def set_outcome(self, outcome: Outcome, *, exit_code: int | None = None) -> None:
        """Force the run-level verdict. Otherwise it is derived from the recorded events."""
        self._outcome = outcome
        self._exit_code = exit_code

    # -- lifecycle ---------------------------------------------------------------------

    def __enter__(self) -> RunRecorder:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        if exc is not None:
            self.event(
                "run-aborted",
                outcome="error",
                type="exception",
                message=f"{type(exc).__name__}: {exc}",
            )
            self._outcome = "error"
        self.finish()
        return False  # never swallow - the caller still needs to see the failure

    def finish(self) -> Path:
        finished = _utcnow()
        summary = {
            "schema_version": _SCHEMA_VERSION,
            "run_id": self.run_id,
            "kind": self.kind,
            "name": self.name,
            "outcome": self._outcome or self._derive_outcome(),
            "exit_code": self._exit_code,
            "started_at": _stamp(self.started),
            "finished_at": _stamp(finished),
            "duration_ms": round((time.monotonic() - self._monotonic_start) * 1000, 3),
            "command": self.command,
            "totals": {"events": self._seq, **self._counts},
            "git": _git_context(),
            "env": _env_context(),
            "metadata": self.metadata,
            "events_file": self.events_path.name if self._seq else None,
        }
        (self.dir / "run.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n",
            encoding="utf-8",
        )
        self._append_index(summary)
        return self.dir

    def _derive_outcome(self) -> Outcome:
        if self._counts.get("error"):
            return "error"
        if self._counts.get("failed"):
            return "failed"
        return "passed"

    def _append_index(self, summary: dict[str, Any]) -> None:
        """One line per run in a single file, so `where is the last failure` is one grep."""
        index = self._root / "index.jsonl"
        index.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            key: summary[key]
            for key in ("run_id", "kind", "name", "outcome", "started_at", "duration_ms", "totals")
        }
        entry["path"] = str(self.dir.relative_to(self._root)).replace("\\", "/")
        entry["commit"] = summary["git"]["commit"]
        entry["model"] = summary["env"]["aioc_model"]
        with index.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def _slug(value: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in value.strip().lower()]
    return "".join(keep).strip("-") or "run"


# ---------------------------------------------------------------------- CLI wrapper


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run a command and record its result under test-results/.",
        epilog="Example: uv run python scripts/runlog.py --kind lint --name ruff -- ruff check .",
    )
    parser.add_argument("--kind", default="command", help="run family: pytest, lint, chaos, llm...")
    parser.add_argument("--name", required=True, help="what this run is, e.g. 'unit' or 'ruff'")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command to run")
    args = parser.parse_args(argv)

    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        parser.error("no command given - put it after `--`")

    with RunRecorder(kind=args.kind, name=args.name, command=" ".join(command)) as run:
        start = time.monotonic()
        try:
            done = subprocess.run(  # noqa: S603 - argv from the operator's own command line
                command, cwd=REPO_ROOT, capture_output=True, text=True, check=False
            )
        except OSError as exc:
            run.event("spawn", outcome="error", message=f"{type(exc).__name__}: {exc}")
            run.set_outcome("error", exit_code=None)
            print(f"ERROR: could not run {command[0]!r}: {exc}", file=sys.stderr)
            return 1

        duration_ms = (time.monotonic() - start) * 1000
        if done.stdout:
            run.artifact("stdout.log", done.stdout)
        if done.stderr:
            run.artifact("stderr.log", done.stderr)

        outcome: Outcome = "passed" if done.returncode == 0 else "failed"
        run.event(
            args.name,
            outcome=outcome,
            duration_ms=duration_ms,
            type="command",
            data={"exit_code": done.returncode, "argv": command},
            message=_last_line(done.stdout) or _last_line(done.stderr),
        )
        run.set_outcome(outcome, exit_code=done.returncode)

        sys.stdout.write(done.stdout)
        sys.stderr.write(done.stderr)
        print(f"\n[runlog] {outcome} -> {run.dir.relative_to(REPO_ROOT)}", file=sys.stderr)
        return done.returncode


def _last_line(text: str) -> str | None:
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:500] if lines else None


if __name__ == "__main__":
    raise SystemExit(main())
