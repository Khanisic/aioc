"""The log store behind `analyze_logs` (Day 13): `docker compose logs` read as data.

There is no log collector in the stack - the demo services write to stdout and Docker keeps
it - so the honest source for "what did this service print" is the container's own log
buffer, read through ``docker compose logs`` with the window passed as ``--since`` and
``--until``. That is a real source with real limits (a recreated container starts with an
empty buffer; nothing older than the last recreate is visible), and both are stated in the
tool description rather than papered over. A synthetic log table would have been easier to
query and would have taught the agent to trust a store that does not exist in production.

Each line arrives as ``<RFC 3339 timestamp> <text>`` (``--timestamps --no-log-prefix``). The
demo services are uvicorn processes, so the text usually starts with a level word
(``INFO:``, ``WARNING:``, ``ERROR:``); a line that does not - a traceback body, a startup
banner - is level ``other``. That mapping is the whole of the parsing, and it is exact
about what it does not know.

Settings follow `aioc.tools.incident.store`: process environment first, the repo's ``.env``
second, resolved from this file rather than the working directory because an MCP client
launches the server from wherever it happens to be. ``AIOC_COMPOSE_DIR`` is where ``docker
compose`` runs (default: this repository), ``DOCKER_BIN`` overrides the binary on PATH, and
``DOCKER_LOGS_TIMEOUT_SECONDS`` bounds one read.

**No `aioc.contracts` import** - this is a tool-server module (contract sec 6).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[4]

# Longhand copy of the contract's `level` enum (sec 7.5). No Python enum exists for it - it
# is a tool-only vocabulary - so the tuple here is the definition and the description
# quotes it.
LOG_LEVELS = ("debug", "info", "warn", "error", "fatal", "other")

# How the level words a uvicorn / Python-logging process prints map onto the contract enum.
_LEVEL_ALIASES = {
    "TRACE": "debug",
    "DEBUG": "debug",
    "INFO": "info",
    "WARN": "warn",
    "WARNING": "warn",
    "ERROR": "error",
    "CRITICAL": "fatal",
    "FATAL": "fatal",
}

_TIMESTAMPED = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))\s?(?P<text>.*)$"
)
_LEVELLED = re.compile(
    r"^\s*(?P<level>TRACE|DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b:?\s*(?P<message>.*)$"
)

# A single read is bounded so a chatty service over a wide window cannot stall the server
# or flood the caller; the tool reports the cut through `meta.truncated`.
DEFAULT_SCAN_LIMIT = 50_000

StoreFailure = Literal["unavailable", "timeout", "unknown_service"]


@dataclass(frozen=True, slots=True)
class LogLine:
    """One parsed line. ``line_no`` is its 1-based position in the window's output and is
    what `source_ref` cites - the only stable handle a log buffer offers."""

    at: datetime
    level: str
    message: str
    line_no: int


class LogStoreError(Exception):
    """The store could not answer. ``failure`` decides the error class the tool returns:
    ``unknown_service`` is `business`, the other two are `transient`."""

    def __init__(self, failure: StoreFailure, message: str) -> None:
        super().__init__(message)
        self.failure: StoreFailure = failure


class LogStore(Protocol):
    """What `analyze_logs` reads from - the compose reader in production, a fake in tests."""

    def read(self, service: str, start: datetime, end: datetime) -> tuple[list[LogLine], bool]:
        """The window's lines in order, and whether the scan was cut at the line limit."""
        ...


class LogStoreSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    compose_dir: Path = Field(default=REPO_ROOT, validation_alias="AIOC_COMPOSE_DIR")
    docker_bin: str | None = Field(default=None, validation_alias="DOCKER_BIN")
    timeout_seconds: float = Field(default=15.0, validation_alias="DOCKER_LOGS_TIMEOUT_SECONDS")


def parse_timestamp(text: str) -> datetime:
    """RFC 3339 with up to nanosecond precision (Docker prints nine digits) to a UTC
    datetime; Python keeps six, so the tail is dropped rather than rejected."""
    body = text[:-1] + "+00:00" if text.endswith("Z") else text
    if "." in body:
        head, rest = body.split(".", 1)
        digits = ""
        for ch in rest:
            if ch.isdigit():
                digits += ch
            else:
                break
        offset = rest[len(digits) :]
        body = f"{head}.{digits[:6].ljust(6, '0')}{offset}"
    parsed = datetime.fromisoformat(body)
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_line(raw: str, line_no: int) -> LogLine | None:
    """A compose ``--timestamps`` line to a `LogLine`; ``None`` for a line with no timestamp
    prefix, which compose does not produce but a caller feeding this by hand might."""
    matched = _TIMESTAMPED.match(raw.rstrip("\r\n"))
    if matched is None:
        return None
    at = parse_timestamp(matched.group("ts"))
    text = matched.group("text")
    levelled = _LEVELLED.match(text)
    if levelled is None:
        return LogLine(at=at, level="other", message=text.strip(), line_no=line_no)
    return LogLine(
        at=at,
        level=_LEVEL_ALIASES[levelled.group("level")],
        message=levelled.group("message").strip(),
        line_no=line_no,
    )


class ComposeLogStore:
    """`docker compose logs` for one service over a window."""

    def __init__(
        self,
        *,
        docker: str | None = None,
        compose_dir: Path = REPO_ROOT,
        timeout_seconds: float = 15.0,
        scan_limit: int = DEFAULT_SCAN_LIMIT,
    ) -> None:
        self._docker = docker
        self._compose_dir = compose_dir
        self._timeout = timeout_seconds
        self._scan_limit = scan_limit

    @classmethod
    def from_settings(cls, settings: LogStoreSettings | None = None) -> ComposeLogStore:
        settings = settings or LogStoreSettings()
        return cls(
            docker=settings.docker_bin,
            compose_dir=settings.compose_dir,
            timeout_seconds=settings.timeout_seconds,
        )

    def read(self, service: str, start: datetime, end: datetime) -> tuple[list[LogLine], bool]:
        docker = self._docker or shutil.which("docker")
        if docker is None:
            raise LogStoreError("unavailable", "the docker CLI is not on PATH")
        argv = [
            docker,
            "compose",
            "logs",
            "--timestamps",
            "--no-log-prefix",
            "--no-color",
            "--since",
            _iso(start),
            "--until",
            _iso(end),
            service,
        ]
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, service is validated
                argv,
                cwd=self._compose_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise LogStoreError("unavailable", f"docker could not be started: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LogStoreError(
                "timeout", f"docker compose logs did not return within {self._timeout}s"
            ) from exc
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            if "no such service" in stderr.lower():
                raise LogStoreError("unknown_service", stderr.splitlines()[0])
            first = stderr.splitlines()[0] if stderr else f"exit status {proc.returncode}"
            raise LogStoreError("unavailable", first)
        raw_lines = proc.stdout.splitlines()
        truncated = len(raw_lines) > self._scan_limit
        parsed = [parse_line(raw, i + 1) for i, raw in enumerate(raw_lines[: self._scan_limit])]
        return [line for line in parsed if line is not None], truncated


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
