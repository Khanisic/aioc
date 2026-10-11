"""Look for credentials in what this repository tracks, or in its whole history (Day 27).

    uv run python scripts/audit_secrets.py              # every tracked file, as it is now
    uv run python scripts/audit_secrets.py --history    # every diff on every ref git knows

Free and offline: it reads git, nothing else. Exit 1 when anything is found, 0 when not.

The patterns are the credential formats this project actually holds - Anthropic, Langfuse,
GitHub, Voyage - plus the generic ones that would be a disaster anywhere (cloud keys,
private key blocks, Slack and Google tokens) and any URL carrying a password. A match is
printed cut to its first characters, so the audit's own output never re-leaks what it found.

`ALLOWED` is every fixture the repository carries on purpose, by its exact text: a value
shaped like a key so a test can prove it is redacted or refused. A new fixture goes there
by name, never by loosening a pattern. `tests/test_secrets_audit.py` runs the tree scan in
the offline suite, so a pull request that adds a credential fails CI; the history scan is
for the audit itself (`docs/design-notes/secrets-audit.md`).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}")),
    ("langfuse", re.compile(r"\b[sp]k-lf-[0-9a-f-]{10,}")),
    ("github", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("voyage", re.compile(r"\bpa-[A-Za-z0-9_-]{30,}")),
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "url_password",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@\"'`]+:(?P<password>[^\s@/\"'`]+)@"),
    ),
)

# Fixtures shaped like credentials on purpose, each named where it lives.
ALLOWED: frozenset[str] = frozenset(
    {
        "sk-ant-test-not-a-key",  # test_baseline, test_eval_scripts, test_model_routing
        "sk-ant-not-for-a-tool-server",  # test_mcp_toolset: must never reach a tool server
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",  # test_github_tool: the patch redaction
    }
)
# Passwords a URL may carry: the local stack's dev-only one, and test values that point
# at nothing. Anything else in a URL's password slot is a finding.
ALLOWED_URL_PASSWORDS: frozenset[str] = frozenset(
    {"aioc_dev_only", "secret", "wrong", "x", "nothing", "password", "pass", "pw"}
)


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    where: str  # path:line, or commit:path for history
    redacted: str

    def render(self) -> str:
        return f"{self.where}: {self.kind} {self.redacted}"


def _redact(text: str) -> str:
    return text[:10] + "..." if len(text) > 10 else text[:3] + "..."


_INTERPOLATION = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*(?::?-(?P<default>[^}]*))?\}")
_UNSET = "UNSET"  # what a `${VAR}` with no default stands for: no value in the file


def _expand(line: str) -> str:
    """Compose and shell interpolation replaced by its default, so the value a reader of
    the file would get is the value judged: `${POSTGRES_PASSWORD:-aioc_dev_only}` is the
    dev password, and a real password as a default would still be found."""
    return _INTERPOLATION.sub(lambda m: m["default"] or _UNSET, line)


def _allowed(kind: str, match: re.Match[str]) -> bool:
    if kind == "url_password":
        return match["password"] in ALLOWED_URL_PASSWORDS | {_UNSET}
    return match.group(0) in ALLOWED


def scan_line(line: str) -> list[tuple[str, str]]:
    """Every credential-shaped string on one line that is not an allowed fixture, as
    ``(kind, redacted)``."""
    found = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(_expand(line) if kind == "url_password" else line):
            if not _allowed(kind, match):
                found.append((kind, _redact(match.group(0))))
    return found


def scan_lines(lines: Iterable[tuple[str, str]]) -> list[Finding]:
    """``lines`` is ``(where, text)`` pairs."""
    return [
        Finding(kind, where, redacted)
        for where, text in lines
        for kind, redacted in scan_line(text)
    ]


def _git(*args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise SystemExit("audit_secrets: git is not on PATH")
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [git, *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def tracked_lines() -> Iterator[tuple[str, str]]:
    """Every line of every tracked text file in the working tree. A file with a NUL byte in
    its first 8 KiB is binary (the GIFs, the PNG) and is skipped."""
    for name in _git("ls-files", "-z").split("\0"):
        path = ROOT / name
        if not name or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            yield f"{name}:{number}", line


def history_lines() -> Iterator[tuple[str, str]]:
    """Every added line of every diff on every ref, attributed to its commit and file."""
    commit, path = "?", "?"
    for line in _git("log", "--all", "-p", "--format=COMMIT %h").splitlines():
        if line.startswith("COMMIT "):
            commit = line.removeprefix("COMMIT ")
        elif line.startswith("+++ "):
            path = line.removeprefix("+++ ").removeprefix("b/")
        elif line.startswith("+"):
            yield f"{commit}:{path}", line[1:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--history", action="store_true", help="every diff on every ref, not the tree"
    )
    args = parser.parse_args(argv)
    if args.history:
        commits = len(_git("rev-list", "--all").split())
        findings = scan_lines(history_lines())
        scope = f"{commits} commits on every ref"
    else:
        files = len([n for n in _git("ls-files", "-z").split("\0") if n])
        findings = scan_lines(tracked_lines())
        scope = f"{files} tracked files"
    for finding in findings:
        print(finding.render())
    print(f"{len(findings)} finding(s) in {scope}.")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
