"""The GitHub tool server: three read-only tools over stdio (Day 11, Platform Layer).

    uv run python -m aioc.tools.github.server        # stdio, for an MCP client

`get_pull_request` (one PR with its files and commits), `list_commits` (recent history on a
ref, optionally windowed and path-filtered), and `diff_refs` (what changed between two git
refs). Together they are what the GitHub agent reads repositories, analyses PRs, and
explains diffs with. The Day 12 `diff_release` is a different question - what changed
between two *deployed releases* (config keys, images, rollout) - and part 4 of each
description below says so.

**No `aioc.contracts` import anywhere in this file** - the MCP boundary is JSON Schema
(contract sec 6), so the PR-state enum is written out longhand and
`tests/test_github_tool.py` asserts the copy still matches the Python enum.

Three things to know before changing this file:

**Config values are never returned, at the source.** A patch is the one place a GitHub tool
could leak a secret - `.env.example`, a compose file, a CI workflow. `redact_patch` rewrites
every added/removed `KEY=value` / `KEY: value` line to keep the key and drop the value, and
blanks anything shaped like a known token. Keys leak nothing; values leak connection
strings. This is the same rule `diff_release` enforces (contract sec 4.4) applied one layer
earlier, and the redaction is tested, not assumed.

**Output is bounded, and says so.** Every list is capped (`max_files`, `max_commits`),
patches are truncated per file at `MAX_PATCH_CHARS`, and `meta.truncated` is true whenever
anything was cut. An agent reading a bounded answer must know it is bounded, because "the
PR touched 8 files" and "the first 8 of 40 files" are different findings.

**Framework input validation is off** (`validate_input=False`), for the same reason as the
incident servers: the framework returns plain text where the contract requires a structured
`validation` error with `details.field` and `details.expected`.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from aioc.tools.envelope import Timer, err, ok
from aioc.tools.github.api import GitHubApi, GitHubApiError

SERVER_NAME = "aioc-github"

GET_PULL_REQUEST = "get_pull_request"
LIST_COMMITS = "list_commits"
DIFF_REFS = "diff_refs"
TOOL_NAMES = (GET_PULL_REQUEST, LIST_COMMITS, DIFF_REFS)

# Longhand copy of the contract's PullRequestState enum. Deliberate duplication - docstring.
PULL_REQUEST_STATES = ("open", "closed", "merged", "draft", "other")

DEFAULT_MAX_FILES = 50
MAX_MAX_FILES = 300
DEFAULT_MAX_COMMITS = 20
MAX_MAX_COMMITS = 100
MAX_PATCH_CHARS = 4000
MAX_BODY_CHARS = 2000
MAX_MESSAGE_CHARS = 1000

# ------------------------------------------------------------------------ redaction
#
# `KEY=value` and `KEY: value` where KEY looks like a configuration key (upper snake case,
# three or more characters). Only added/removed lines are rewritten; context lines of a
# unified diff are left as GitHub sent them except for token-shaped strings.
_ASSIGNMENT = re.compile(r"^([+-])(\s*(?:export\s+)?[A-Z][A-Z0-9_]{2,}\s*[=:]\s*)(\S.*)$")
# Shapes of credentials that appear in code and config regardless of the key name.
_TOKEN_SHAPES = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
    r"|pk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
REDACTED = "<redacted>"


def redact_patch(patch: str) -> tuple[str, int]:
    """Keys only. Returns the rewritten patch and how many lines were redacted."""
    out: list[str] = []
    count = 0
    for line in patch.splitlines():
        rewritten = line
        match = _ASSIGNMENT.match(line)
        if match and match.group(3).strip() not in ("", REDACTED):
            rewritten = f"{match.group(1)}{match.group(2)}{REDACTED}"
        rewritten, n = _TOKEN_SHAPES.subn(REDACTED, rewritten)
        if rewritten != line:
            count += 1
        out.append(rewritten)
    return "\n".join(out), count


# ------------------------------------------------------------------------ descriptions
#
# The four-part template (contract sec 6.5), in order: what it does + inputs, three example
# queries, edge cases and limits, when to use this vs. the named alternative.

GET_PULL_REQUEST_DESCRIPTION = """\
Fetches one pull request from the configured repository with its metadata, the files it \
touched, and the commits it carries. Returns `pull_request` (number, title, state as one of \
open/closed/merged/draft, merged_at, head_sha, base/head refs, author, files_changed, \
additions, deletions, touched_paths, a truncated body), `files` (one entry per path with \
status, additions, deletions, and the unified-diff `patch` when `include_patch` is true), and \
`commits` (sha, short_sha, message, authored_at). Inputs: `number` (integer, required), \
`include_patch` (boolean, default false - patches are large; ask only when you need to read \
the change itself), `max_files` (1-300, default 50).

Example queries this tool answers:
- "What did PR #12 change, and how risky does it look?"
- "Which files did pull request 9 touch, and was it merged?"
- "Show me the actual diff of #11 so I can tell whether it changed the pool size."
- "Who authored #7 and which commits are in it?"

Edge cases and limits: `state` is `merged` when the PR was merged (GitHub itself reports \
`closed` for those), `draft` for open drafts. Configuration VALUES are never returned: any \
added or removed `KEY=value` line in a patch comes back with the value replaced by \
`<redacted>` and the key kept, and `redacted_lines` counts them - the key being changed is the \
finding, the value is not yours to read. Patches are cut at 4000 characters per file and the \
file list at `max_files`; `meta.truncated` is true whenever anything was cut, so "N files" \
means "the first N" in that case. A PR number that does not exist is a NOT_FOUND business \
error, not an empty success. Binary files have no patch.

When to use this vs. the alternative: use `get_pull_request` when you have a PR NUMBER and \
want to know what that one change did. Use `list_commits` instead to find WHICH changes \
landed in a time window or on a path when you have no number yet. Use `diff_refs` to compare \
two arbitrary git refs (tags, SHAs, branches) rather than one PR. Use `diff_release` (the \
deployment tool) when the question is what changed between two DEPLOYED releases - config \
keys, images, rollout - rather than in the source."""

LIST_COMMITS_DESCRIPTION = """\
Lists recent commits on a ref of the configured repository, newest first, with optional time \
window and path filter. Each entry has sha, short_sha, message (first 1000 characters), \
authored_at (RFC 3339 UTC), html_url, and pull_request_number when the message carries a \
squash or merge marker like `(#12)` or `Merge pull request #12`. With `include_paths` true \
each commit also lists `touched_paths` (one extra GitHub call per commit - keep \
`max_commits` small when you ask for it). Inputs: `ref` (branch, tag, or SHA; default the \
repository's default branch), `since` / `until` (RFC 3339 UTC with explicit Z), `path` \
(only commits touching this file or directory), `max_commits` (1-100, default 20), \
`include_paths` (boolean, default false).

Example queries this tool answers:
- "What landed on main in the hour before the 14:00 latency spike?"
- "Which commits touched docker-compose.yml this month?"
- "List the last ten commits on the release-1.4 branch with the files they changed."
- "Is there a commit between 13:00Z and 13:30Z that could explain the deploy?"

Edge cases and limits: a window with no commits is a successful empty `commits` array, not an \
error - "nothing landed" is a finding. `pull_request_number` is parsed from the message \
text, so a commit merged without a marker has `null` there even if it came from a PR; \
confirm with `get_pull_request` before relying on it. Only the first page is returned \
(`max_commits`), newest first; `meta.truncated` is true when the window held more. An \
unknown `ref` is a NOT_FOUND business error. `touched_paths` is empty unless \
`include_paths` is true - empty there means "not asked", not "touched nothing".

When to use this vs. the alternative: use `list_commits` to DISCOVER candidate changes in a \
time window or on a path when you do not yet know which PR matters. Use `get_pull_request` \
once you have a number and want that change's full detail and diff. Use `diff_refs` when you \
know two refs (for example the SHA before and after a deploy) and want everything between \
them as one comparison. Use `get_incident_timeline` for deploy EVENTS as the platform \
recorded them; this tool sees git history, not rollouts."""

DIFF_REFS_DESCRIPTION = """\
Compares two git refs of the configured repository (`base...head`, the same three-dot \
comparison GitHub's compare view uses) and returns the commits between them plus the files \
that differ, with additions, deletions, status, and the unified-diff `patch` when \
`include_patch` is true. Also reports `ahead_by`, `behind_by`, and `total_commits`. Inputs: \
`base` and `head` (each a branch, tag, or SHA; required), `include_patch` (boolean, default \
false), `max_files` (1-300, default 50).

Example queries this tool answers:
- "What changed between v1.4.2 and v1.4.3?"
- "Diff the SHA that was running before the deploy against the one running after."
- "Compare main against the hotfix branch: which files differ?"
- "Between 884b0b6 and 0bb9532, did anything touch the database settings?"

Edge cases and limits: identical refs return an empty `files` array and `total_commits` 0 - \
a success meaning "no difference", not an error. An unknown ref is a NOT_FOUND business \
error. Configuration VALUES are never returned: added or removed `KEY=value` lines come back \
with the value replaced by `<redacted>` (the key is kept, `redacted_lines` counts them). \
Patches are cut at 4000 characters per file and the file list at `max_files`; \
`meta.truncated` is true whenever anything was cut. GitHub caps a comparison at 250 \
commits and 300 files; beyond that the answer is partial and flagged.

When to use this vs. the alternative: use `diff_refs` when you have TWO REFS and want the \
source-level difference between them. Use `get_pull_request` for one PR's change. Use \
`list_commits` to find which refs matter in the first place. Use `diff_release` (the \
deployment tool) when the comparison is between two DEPLOYED releases and the question is \
config keys, images, and rollout health rather than source."""


# ------------------------------------------------------------------------ input schemas

_RFC3339 = "RFC 3339 UTC with an explicit Z, e.g. 2026-08-22T13:40:00Z"

GET_PULL_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["number"],
    "properties": {
        "number": {"type": "integer", "minimum": 1, "description": "The pull request number."},
        "include_patch": {
            "type": "boolean",
            "default": False,
            "description": "Include each file's unified-diff patch (large; values redacted).",
        },
        "max_files": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_MAX_FILES,
            "default": DEFAULT_MAX_FILES,
            "description": f"Cap on files returned, 1-{MAX_MAX_FILES}.",
        },
    },
}

LIST_COMMITS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "ref": {
            "type": ["string", "null"],
            "default": None,
            "description": "Branch, tag, or SHA to list from. Omit for the default branch.",
        },
        "since": {
            "type": ["string", "null"],
            "default": None,
            "description": f"Only commits authored at or after this moment ({_RFC3339}).",
        },
        "until": {
            "type": ["string", "null"],
            "default": None,
            "description": f"Only commits authored at or before this moment ({_RFC3339}).",
        },
        "path": {
            "type": ["string", "null"],
            "default": None,
            "description": "Only commits touching this file or directory path.",
        },
        "max_commits": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_MAX_COMMITS,
            "default": DEFAULT_MAX_COMMITS,
            "description": f"Cap on commits returned, 1-{MAX_MAX_COMMITS}, newest first.",
        },
        "include_paths": {
            "type": "boolean",
            "default": False,
            "description": "Fetch each commit's touched paths (one extra call per commit).",
        },
    },
}

DIFF_REFS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["base", "head"],
    "properties": {
        "base": {"type": "string", "description": "The older ref: branch, tag, or SHA."},
        "head": {"type": "string", "description": "The newer ref: branch, tag, or SHA."},
        "include_patch": {
            "type": "boolean",
            "default": False,
            "description": "Include each file's unified-diff patch (large; values redacted).",
        },
        "max_files": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_MAX_FILES,
            "default": DEFAULT_MAX_FILES,
            "description": f"Cap on files returned, 1-{MAX_MAX_FILES}.",
        },
    },
}

SCHEMAS = {
    GET_PULL_REQUEST: GET_PULL_REQUEST_SCHEMA,
    LIST_COMMITS: LIST_COMMITS_SCHEMA,
    DIFF_REFS: DIFF_REFS_SCHEMA,
}
DESCRIPTIONS = {
    GET_PULL_REQUEST: GET_PULL_REQUEST_DESCRIPTION,
    LIST_COMMITS: LIST_COMMITS_DESCRIPTION,
    DIFF_REFS: DIFF_REFS_DESCRIPTION,
}


# ------------------------------------------------------------------------- validation


class _Invalid(Exception):
    """A validation failure carrying the field and expectation the contract requires."""

    def __init__(self, field: str, expected: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.expected = expected


def _reject_unknown(args: dict[str, Any], schema: dict[str, Any]) -> None:
    unknown = set(args) - set(schema["properties"])
    if unknown:
        raise _Invalid(
            sorted(unknown)[0],
            f"one of {sorted(schema['properties'])}",
            f"unknown input field(s): {sorted(unknown)}",
        )
    for field in schema.get("required", []):
        if field not in args:
            raise _Invalid(field, "present (required)", f"{field} is required")


def _int_in(args: dict[str, Any], field: str, lo: int, hi: int, default: int) -> int:
    value = args.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Invalid(field, f"an integer {lo}-{hi}", f"{field} must be an integer")
    if not lo <= value <= hi:
        raise _Invalid(field, f"an integer {lo}-{hi}", f"{field} out of range: {value}")
    return value


def _bool(args: dict[str, Any], field: str) -> bool:
    value = args.get(field, False)
    if not isinstance(value, bool):
        raise _Invalid(field, "true or false", f"{field} must be a boolean")
    return value


def _opt_str(args: dict[str, Any], field: str) -> str | None:
    value = args.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _Invalid(field, "a non-empty string or null", f"{field} must be a non-empty string")
    return value.strip()


def _req_str(args: dict[str, Any], field: str) -> str:
    value = _opt_str(args, field)
    if value is None:
        raise _Invalid(field, "a non-empty string", f"{field} is required")
    return value


def _timestamp(args: dict[str, Any], field: str) -> str | None:
    raw = _opt_str(args, field)
    if raw is None:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _Invalid(field, _RFC3339, f"{field} is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return _stamp(parsed)


def _validate(name: str, args: dict[str, Any]) -> dict[str, Any]:
    schema = SCHEMAS[name]
    _reject_unknown(args, schema)
    if name == GET_PULL_REQUEST:
        return {
            "number": _int_in(args, "number", 1, 10**9, 0),
            "include_patch": _bool(args, "include_patch"),
            "max_files": _int_in(args, "max_files", 1, MAX_MAX_FILES, DEFAULT_MAX_FILES),
        }
    if name == LIST_COMMITS:
        since, until = _timestamp(args, "since"), _timestamp(args, "until")
        if since and until and since > until:
            raise _Invalid("until", "a moment at or after `since`", "until is before since")
        return {
            "ref": _opt_str(args, "ref"),
            "since": since,
            "until": until,
            "path": _opt_str(args, "path"),
            "max_commits": _int_in(args, "max_commits", 1, MAX_MAX_COMMITS, DEFAULT_MAX_COMMITS),
            "include_paths": _bool(args, "include_paths"),
        }
    return {
        "base": _req_str(args, "base"),
        "head": _req_str(args, "head"),
        "include_patch": _bool(args, "include_patch"),
        "max_files": _int_in(args, "max_files", 1, MAX_MAX_FILES, DEFAULT_MAX_FILES),
    }


# --------------------------------------------------------------------------- shaping

_PR_MARKER = re.compile(r"\(#(\d+)\)|Merge pull request #(\d+)")


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clip(text: str | None, limit: int) -> tuple[str, bool]:
    text = text or ""
    return (text[:limit], True) if len(text) > limit else (text, False)


def pull_request_state(pr: dict[str, Any]) -> str:
    """GitHub's own `state` is only open/closed; the contract wants merged and draft too."""
    if pr.get("merged") or pr.get("merged_at"):
        return "merged"
    if pr.get("draft"):
        return "draft"
    state = str(pr.get("state", ""))
    return state if state in ("open", "closed") else "other"


def pull_request_number_from_message(message: str) -> int | None:
    match = _PR_MARKER.search(message.splitlines()[0] if message else "")
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _shape_commit(raw: dict[str, Any], *, touched_paths: list[str] | None = None) -> dict[str, Any]:
    commit = raw.get("commit") or {}
    message, _ = _clip(str(commit.get("message") or ""), MAX_MESSAGE_CHARS)
    sha = str(raw.get("sha") or "")
    author = (commit.get("author") or {}).get("date")
    return {
        "sha": sha,
        "short_sha": sha[:7],
        "message": message,
        "authored_at": author,
        "html_url": raw.get("html_url"),
        "pull_request_number": pull_request_number_from_message(message),
        "touched_paths": list(touched_paths) if touched_paths is not None else [],
    }


def _shape_files(
    raw_files: list[dict[str, Any]], *, include_patch: bool, max_files: int
) -> tuple[list[dict[str, Any]], bool, int]:
    """The bounded, redacted file list. Returns (files, truncated, redacted_lines)."""
    truncated = len(raw_files) > max_files
    files: list[dict[str, Any]] = []
    redacted_total = 0
    for raw in raw_files[:max_files]:
        entry: dict[str, Any] = {
            "path": raw.get("filename"),
            "status": raw.get("status"),
            "additions": raw.get("additions", 0),
            "deletions": raw.get("deletions", 0),
            "previous_path": raw.get("previous_filename"),
        }
        if include_patch:
            patch = raw.get("patch")
            if patch is None:
                entry["patch"] = None  # binary or too large for GitHub to render
                entry["patch_truncated"] = False
            else:
                redacted, n = redact_patch(str(patch))
                redacted_total += n
                clipped, cut = _clip(redacted, MAX_PATCH_CHARS)
                entry["patch"] = clipped
                entry["patch_truncated"] = cut
                truncated = truncated or cut
        files.append(entry)
    return files, truncated, redacted_total


# ------------------------------------------------------------------------- the tools


def fetch_pull_request(params: dict[str, Any], api: GitHubApi) -> types.CallToolResult:
    repo = api.repository
    if repo is None:
        return _no_repository()
    try:
        with Timer() as timer:
            pr = api.pull_request(repo, params["number"])
            raw_files = api.pull_request_files(repo, params["number"], limit=MAX_MAX_FILES)
            raw_commits = api.pull_request_commits(repo, params["number"], limit=MAX_MAX_COMMITS)
    except GitHubApiError as exc:
        return _api_error(exc)

    files, truncated, redacted = _shape_files(
        raw_files, include_patch=params["include_patch"], max_files=params["max_files"]
    )
    body, body_cut = _clip(pr.get("body"), MAX_BODY_CHARS)
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    data = {
        "repository": repo,
        "pull_request": {
            "number": pr.get("number"),
            "title": pr.get("title"),
            "state": pull_request_state(pr),
            "draft": bool(pr.get("draft")),
            "merged_at": pr.get("merged_at"),
            "created_at": pr.get("created_at"),
            "author": (pr.get("user") or {}).get("login"),
            "head_sha": head.get("sha"),
            "head_ref": head.get("ref"),
            "base_ref": base.get("ref"),
            "html_url": pr.get("html_url"),
            "files_changed": pr.get("changed_files", len(raw_files)),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "touched_paths": [str(f.get("filename")) for f in raw_files],
            "body": body,
            "body_truncated": body_cut,
        },
        "files": files,
        "commits": [_shape_commit(c) for c in raw_commits],
        "redacted_lines": redacted,
    }
    return ok(
        data,
        returned=len(files),
        truncated=truncated or body_cut,
        total_available=len(raw_files),
        query_ms=timer.ms,
        source="github",
        as_of=_stamp(datetime.now(UTC)),
    )


def fetch_commits(params: dict[str, Any], api: GitHubApi) -> types.CallToolResult:
    repo = api.repository
    if repo is None:
        return _no_repository()
    limit = params["max_commits"]
    try:
        with Timer() as timer:
            # Ask for one more than the cap so `truncated` is a fact, not a guess.
            raw = api.commits(
                repo,
                ref=params["ref"],
                since=params["since"],
                until=params["until"],
                path=params["path"],
                limit=min(limit + 1, 100),
            )
            truncated = len(raw) > limit
            raw = raw[:limit]
            paths: dict[str, list[str]] = {}
            if params["include_paths"]:
                for item in raw:
                    detail = api.commit(repo, str(item.get("sha")))
                    paths[str(item.get("sha"))] = [
                        str(f.get("filename")) for f in detail.get("files") or []
                    ]
    except GitHubApiError as exc:
        return _api_error(exc)

    commits = [_shape_commit(c, touched_paths=paths.get(str(c.get("sha")))) for c in raw]
    return ok(
        {
            "repository": repo,
            "ref": params["ref"],
            "window": {"since": params["since"], "until": params["until"]},
            "path": params["path"],
            "commits": commits,
            "paths_included": params["include_paths"],
        },
        returned=len(commits),
        truncated=truncated,
        query_ms=timer.ms,
        source="github",
        as_of=_stamp(datetime.now(UTC)),
    )


def fetch_diff(params: dict[str, Any], api: GitHubApi) -> types.CallToolResult:
    repo = api.repository
    if repo is None:
        return _no_repository()
    try:
        with Timer() as timer:
            comparison = api.compare(repo, params["base"], params["head"])
    except GitHubApiError as exc:
        return _api_error(exc)

    raw_files = [f for f in comparison.get("files") or [] if isinstance(f, dict)]
    files, truncated, redacted = _shape_files(
        raw_files, include_patch=params["include_patch"], max_files=params["max_files"]
    )
    raw_commits = [c for c in comparison.get("commits") or [] if isinstance(c, dict)]
    total_commits = int(comparison.get("total_commits") or len(raw_commits))
    return ok(
        {
            "repository": repo,
            "base": params["base"],
            "head": params["head"],
            "status": comparison.get("status"),
            "ahead_by": comparison.get("ahead_by"),
            "behind_by": comparison.get("behind_by"),
            "total_commits": total_commits,
            "commits": [_shape_commit(c) for c in raw_commits],
            "files": files,
            "redacted_lines": redacted,
        },
        returned=len(files),
        # GitHub itself caps the comparison at 250 commits; past that it is partial.
        truncated=truncated or total_commits > len(raw_commits),
        total_available=len(raw_files),
        query_ms=timer.ms,
        source="github",
        as_of=_stamp(datetime.now(UTC)),
    )


def _no_repository() -> types.CallToolResult:
    return err(
        "validation",
        "REPOSITORY_NOT_CONFIGURED",
        "No target repository is configured.",
        remediation="Set GITHUB_REPO=owner/name in .env and restart the server.",
        details={"field": "GITHUB_REPO", "expected": "owner/name"},
    )


def _api_error(exc: GitHubApiError) -> types.CallToolResult:
    return err(
        exc.error_class,
        exc.code,
        exc.message,
        remediation=exc.remediation,
        details=exc.details,
        retry_after_ms=exc.retry_after_ms,
    )


def call(
    name: str, arguments: dict[str, Any], *, api: GitHubApi | None = None
) -> types.CallToolResult:
    """Dispatch one call: validate, then fetch. Synchronous, so it is testable without a
    server and so the MCP handler is a one-liner around it."""
    if name not in TOOL_NAMES:
        return err(
            "validation",
            "UNKNOWN_TOOL",
            f"This server exposes only {list(TOOL_NAMES)}.",
            remediation=f"Call one of {list(TOOL_NAMES)} instead.",
            details={"field": "name", "expected": list(TOOL_NAMES), "received": name},
        )
    try:
        params = _validate(name, arguments or {})
    except _Invalid as exc:
        return err(
            "validation",
            "INVALID_INPUT",
            str(exc),
            remediation=f"Correct `{exc.field}` to {exc.expected} and call again.",
            details={"field": exc.field, "expected": exc.expected},
        )
    api = api or GitHubApi()
    if name == GET_PULL_REQUEST:
        return fetch_pull_request(params, api)
    if name == LIST_COMMITS:
        return fetch_commits(params, api)
    return fetch_diff(params, api)


# ---------------------------------------------------------------------------- MCP plumbing

server = Server(SERVER_NAME)


# The `type: ignore`s are the mcp library's decorators being untyped, not looseness here.
@server.list_tools()  # type: ignore[no-untyped-call, untyped-decorator]
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(name=name, description=DESCRIPTIONS[name], inputSchema=SCHEMAS[name])
        for name in TOOL_NAMES
    ]


# validate_input=False on purpose - the module docstring says why.
@server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    # httpx is used synchronously; run it off the event loop.
    return await asyncio.to_thread(call, name, arguments)


async def _serve() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
