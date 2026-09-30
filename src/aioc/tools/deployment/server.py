"""The deployment tool server: `diff_release` and `check_rollout_health` over stdio (Day 12,
Platform Layer). These two ARE contract-named tools (CONTRACTS.md sec 7.3, 7.4), so their
input and output shapes are frozen; everything else here follows `tools/github/server.py`.

    uv run python -m aioc.tools.deployment.server        # stdio, for an MCP client

`diff_release` answers *what changed between two releases of a service*: the release
manifests (compose, `.env`-style files, Kubernetes manifests) are read at both ends of a
git comparison and diffed structurally by `release.py`, plus the commits between them.
`check_rollout_health` answers *how the deployed version is behaving right now*, from
Prometheus, by `health.py`. Part 4 of each description names the other as the
alternative - that line is the discriminator the Deployment agent routes on.

**Config values are never returned, at the source.** `release.py` hashes every value the
moment it is parsed; `config_keys_*` and `manifest_changes` are keys and paths. This is the
contract's sec 4.4 rule enforced where the data enters, which is why
`DeploymentFindings.changed_config_keys` can be rendered in a demo.

**Error mapping, stated because agents match `code` programmatically:**

- `diff_release`: `SAME_VERSION` (validation), `UNKNOWN_RELEASE_VERSION` (business, names
  the ref that does not exist), `REGISTRY_UNAVAILABLE` (transient, GitHub down or rate
  limited), `REGISTRY_SCOPE_MISSING` (permission, no or under-scoped token). The registry
  is GitHub: the repository is where releases are declared.
- `check_rollout_health`: `UNKNOWN_SERVICE` (business, lists what Prometheus does scrape),
  `VERSION_NOT_DEPLOYED` (business, never ran inside the window), `INVALID_LOOKBACK`
  (validation), `PROMETHEUS_TIMEOUT` (transient).
- Additive, pending their sec 0 entry: `PROMETHEUS_UNAVAILABLE` (transient - unreachable is
  not a timeout), `VERSION_NOT_OBSERVABLE` (business - the running image does not export
  `service_build_info`, so a requested version cannot be confirmed either way),
  `ENVIRONMENT_NOT_MONITORED` (business - this stack is one environment, and the health of
  `production` must not be answered with `development` numbers), and the shared
  `CHAOS_SCOPE_REQUIRED` (permission, `aioc.tools.policy`).

**No `aioc.contracts` import anywhere in this package** - the MCP boundary is JSON Schema
(contract sec 6), so the `Environment` and `RolloutStatus` enums are written out longhand
and `tests/test_deployment_tool.py` asserts the copies still match the Python enums.
Framework input validation is off (`validate_input=False`) for the same reason as every
other server: the framework returns plain text where the contract requires a structured
`validation` error with `details.field` and `details.expected`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from aioc.tools.deployment import health, release
from aioc.tools.envelope import Timer, err, ok
from aioc.tools.github.api import GitHubApi, GitHubApiError
from aioc.tools.github.commits import commit_headline
from aioc.tools.policy import ground_truth_denied, restricted_names

SERVER_NAME = "aioc-deployment"

DIFF_RELEASE = "diff_release"
CHECK_ROLLOUT_HEALTH = "check_rollout_health"
TOOL_NAMES = (DIFF_RELEASE, CHECK_ROLLOUT_HEALTH)

# Longhand copies of the contract enums. Deliberate duplication - module docstring.
ENVIRONMENTS = ("development", "staging", "production", "other")
ROLLOUT_STATUSES = (
    "healthy",
    "degraded",
    "failed",
    "in_progress",
    "rolled_back",
    "unknown",
    "other",
)
INCLUDE_OPTIONS = ("config", "images", "manifests", "commits", "all")

DEFAULT_LOOKBACK_MINUTES = 30
MIN_LOOKBACK_MINUTES = 5
MAX_LOOKBACK_MINUTES = 1440
MAX_RELEASE_FILES = 40  # manifests inspected per diff (two GitHub reads each)
MAX_COMMITS = 250  # GitHub's own cap on a comparison


class DeploymentSettings(BaseSettings):
    """`PROMETHEUS_URL`, `PROMETHEUS_JOB`, and `AIOC_ENV` - the environment this stack IS.
    Process environment first, the repo `.env` second, resolved from this file because an
    MCP client launches the server from wherever it happens to be."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[4] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    prometheus_url: str = Field(default="http://localhost:9090", validation_alias="PROMETHEUS_URL")
    prometheus_job: str = Field(default=health.DEFAULT_JOB, validation_alias="PROMETHEUS_JOB")
    environment: str = Field(default="development", validation_alias="AIOC_ENV")
    prometheus_timeout_seconds: float = Field(
        default=5.0, validation_alias="PROMETHEUS_TIMEOUT_SECONDS"
    )

    def monitored_environment(self) -> str:
        value = self.environment.strip().lower()
        return value if value in ENVIRONMENTS else "other"


# ------------------------------------------------------------------------ descriptions
#
# The four-part template (contract sec 6.5), in order: what it does + inputs, three example
# queries, edge cases and limits, when to use this vs. the named alternative.

DIFF_RELEASE_DESCRIPTION = """\
Reports the structural difference between two releases of a service: configuration keys \
added, removed, and changed; container image changes; changed manifest paths; and the \
commits between the two releases. A release is a git ref (tag, branch, or SHA) of the \
configured repository, and the release identity is read from its manifests at that ref - \
compose files (`services.<service>`: image, environment keys, other settings), `.env`-style \
files (shared configuration, counted for every service), and Kubernetes manifests under \
infrastructure/, k8s/, deploy/, or manifests/ that name the service. Inputs: `service` \
(string, required), `from_version` and `to_version` (strings, required, each a git ref), \
`include` (one of config, images, manifests, commits, all; default all - sections not \
included come back null, meaning not computed, not empty).

Example queries this tool answers:
- "What changed between v1.4.2 and v1.4.3 of checkout-api?"
- "Did the last deploy of payments-api change any configuration keys?"
- "Which commits went out with the release that is running now, compared to the previous one?"
- "Did the inventory-api image change between these two SHAs?"

Edge cases and limits: configuration VALUES are never returned - only key names - and \
manifest changes are dotted paths (`services.checkout-api.ports[0]`, `spec.replicas`), never \
values; the diff is computed on hashed values so a value cannot leak through this tool. Two \
identical refs are a SAME_VERSION validation error, not an empty diff. A ref that does not \
exist is an UNKNOWN_RELEASE_VERSION business error naming which one. Empty lists are a \
finding ("no config key changed"), not an error. Up to 40 release files are inspected per \
diff and GitHub caps a comparison at 250 commits; `meta.truncated` is true whenever either \
cap was hit. A commit's `message` is its subject line only. A service absent from every \
manifest yields empty config/image/manifest lists while the commits are still reported - \
the commits are repository-wide. The demo stack's \
deployed version is whatever `DEMO_GIT_SHA` was set to; a version like `baseline` is not \
a git ref and cannot be diffed.

When to use this vs. the alternative: use `diff_release` to answer WHAT CHANGED between two \
releases - the candidate causes. Use `check_rollout_health` instead to answer HOW THE \
CURRENT RELEASE IS BEHAVING - whether there is a problem at all. Diff tells you the cause \
candidates; health tells you whether there is a problem. Use `diff_refs` (the GitHub tool) \
when the question is about source code rather than deployed configuration, images, and \
manifests."""

CHECK_ROLLOUT_HEALTH_DESCRIPTION = """\
Reports the current health of a deployed version of a service from Prometheus: `status` \
(one of healthy, degraded, failed, in_progress, rolled_back, unknown), `replicas` (desired, \
ready, updated, unavailable), `signals` over the lookback window (error_rate as a 0-1 \
ratio, p50 and p99 latency in milliseconds, restart_count, probe_failures as failed \
scrapes), and `compared_to_baseline` (the previous version seen in the window with the \
error-rate and p99 deltas against it, or null when there was no previous version). Inputs: \
`service` (string, required), `environment` (one of development, staging, production, \
other; required), `version` (string, optional - defaults to the version currently \
deployed; when given, the status is about THAT version), `lookback_minutes` (integer, \
5-1440, default 30).

Example queries this tool answers:
- "Is the current checkout-api rollout healthy?"
- "How is payments-api behaving in the last 15 minutes compared with before the deploy?"
- "Did version 3f2a9c1 of inventory-api ever go out, and is it still running?"
- "Are there restarts or failed probes on checkout-api since the release?"

Edge cases and limits: any signal that could not be measured is null, never 0 - no \
traffic in the window means error_rate and latencies are null, while traffic with no 5xx \
is an error_rate of 0.0. The status rule is deterministic, in this order: `failed` when no \
instance is up; a requested version that never ran inside the window is a \
VERSION_NOT_DEPLOYED business error, and one that ran and was then replaced is \
`rolled_back`; `in_progress` when the version runs on some ready instances but not all; \
`degraded` when the error rate is at or above 5%, any instance restarted, any scrape \
failed, or fewer instances are ready than desired; `unknown` when instances are up but \
nothing could be measured; `healthy` otherwise. The version identity comes from the \
`service_build_info` metric each service exports; an image that does not export it makes \
a requested version unverifiable (VERSION_NOT_OBSERVABLE). A service Prometheus does not \
scrape is UNKNOWN_SERVICE with the known services in the remediation. This stack is a single \
environment; asking about another is ENVIRONMENT_NOT_MONITORED rather than a silent answer \
from the wrong one. The chaos namespace (any `chaos*` service) is the eval harness's \
injected ground truth and returns a permission error. Prometheus keeps 7 days; `lookback_minutes` \
cannot exceed 1440 and a longer baseline is not available.

When to use this vs. the alternative: use `check_rollout_health` for IS THE CURRENT ROLLOUT \
HEALTHY RIGHT NOW. Use `diff_release` when health is already known to be bad and you need \
the CANDIDATE CAUSE - what changed between the healthy release and this one. Use \
`get_incident_timeline` (the incident tool) for the recorded deploy and alert events of a \
past incident rather than live metrics."""


# ------------------------------------------------------------------------ input schemas

DIFF_RELEASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["service", "from_version", "to_version"],
    "properties": {
        "service": {"type": "string", "description": "The service, e.g. checkout-api."},
        "from_version": {
            "type": "string",
            "description": "The older release: a git tag, branch, or SHA.",
        },
        "to_version": {
            "type": "string",
            "description": "The newer release: a git tag, branch, or SHA.",
        },
        "include": {
            "type": "string",
            "enum": list(INCLUDE_OPTIONS),
            "default": "all",
            "description": "Which section to compute; the others come back null.",
        },
    },
}

CHECK_ROLLOUT_HEALTH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["service", "environment"],
    "properties": {
        "service": {"type": "string", "description": "The service, e.g. checkout-api."},
        "environment": {
            "type": "string",
            "enum": list(ENVIRONMENTS),
            "description": "The environment the service is deployed in.",
        },
        "version": {
            "type": ["string", "null"],
            "default": None,
            "description": "The version to assess. Omit for the currently deployed one.",
        },
        "lookback_minutes": {
            "type": "integer",
            "minimum": MIN_LOOKBACK_MINUTES,
            "maximum": MAX_LOOKBACK_MINUTES,
            "default": DEFAULT_LOOKBACK_MINUTES,
            "description": (
                f"Window for the signals, {MIN_LOOKBACK_MINUTES}-{MAX_LOOKBACK_MINUTES} minutes."
            ),
        },
    },
}

SCHEMAS = {DIFF_RELEASE: DIFF_RELEASE_SCHEMA, CHECK_ROLLOUT_HEALTH: CHECK_ROLLOUT_HEALTH_SCHEMA}
DESCRIPTIONS = {
    DIFF_RELEASE: DIFF_RELEASE_DESCRIPTION,
    CHECK_ROLLOUT_HEALTH: CHECK_ROLLOUT_HEALTH_DESCRIPTION,
}


# ------------------------------------------------------------------------- validation


class _Invalid(Exception):
    """A validation failure carrying the field and expectation the contract requires."""

    def __init__(
        self, field: str, expected: str, message: str, code: str = "INVALID_INPUT"
    ) -> None:
        super().__init__(message)
        self.field = field
        self.expected = expected
        self.code = code


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


def _req_str(args: dict[str, Any], field: str) -> str:
    value = args.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _Invalid(field, "a non-empty string", f"{field} must be a non-empty string")
    return value.strip()


def _opt_str(args: dict[str, Any], field: str) -> str | None:
    if args.get(field) is None:
        return None
    return _req_str(args, field)


def _choice(args: dict[str, Any], field: str, options: tuple[str, ...], default: str | None) -> str:
    value = args.get(field, default)
    if not isinstance(value, str) or value not in options:
        raise _Invalid(field, f"one of {list(options)}", f"{field} must be one of {list(options)}")
    return value


def _validate(name: str, args: dict[str, Any]) -> dict[str, Any]:
    schema = SCHEMAS[name]
    _reject_unknown(args, schema)
    service = _req_str(args, "service")
    if name == DIFF_RELEASE:
        from_version = _req_str(args, "from_version")
        to_version = _req_str(args, "to_version")
        if from_version == to_version:
            raise _Invalid(
                "to_version",
                "a release different from from_version",
                f"from_version and to_version are both {to_version!r}; there is nothing to diff",
                code="SAME_VERSION",
            )
        return {
            "service": service,
            "from_version": from_version,
            "to_version": to_version,
            "include": _choice(args, "include", INCLUDE_OPTIONS, "all"),
        }
    lookback = args.get("lookback_minutes", DEFAULT_LOOKBACK_MINUTES)
    if (
        isinstance(lookback, bool)
        or not isinstance(lookback, int)
        or not MIN_LOOKBACK_MINUTES <= lookback <= MAX_LOOKBACK_MINUTES
    ):
        raise _Invalid(
            "lookback_minutes",
            f"an integer {MIN_LOOKBACK_MINUTES}-{MAX_LOOKBACK_MINUTES}",
            f"lookback_minutes must be an integer {MIN_LOOKBACK_MINUTES}-{MAX_LOOKBACK_MINUTES}, "
            f"got {lookback!r}",
            code="INVALID_LOOKBACK",
        )
    return {
        "service": service,
        "environment": _choice(args, "environment", ENVIRONMENTS, None),
        "version": _opt_str(args, "version"),
        "lookback_minutes": lookback,
    }


# ------------------------------------------------------------------------- diff_release


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _registry_error(exc: GitHubApiError, *, version: str | None = None) -> types.CallToolResult:
    """The GitHub client's four classes, renamed to the contract's sec 7.3 codes."""
    if exc.error_class == "permission":
        return err(
            "permission",
            "REGISTRY_SCOPE_MISSING",
            exc.message,
            remediation=exc.remediation,
            details=exc.details,
        )
    if exc.error_class == "transient":
        return err(
            "transient",
            "REGISTRY_UNAVAILABLE",
            exc.message,
            remediation=exc.remediation,
            details=exc.details,
            retry_after_ms=exc.retry_after_ms or 2000,
        )
    if exc.error_class == "business" and exc.code == "NOT_FOUND" and version is not None:
        return err(
            "business",
            "UNKNOWN_RELEASE_VERSION",
            f"Release {version!r} does not exist in the repository.",
            remediation=(
                "Use a git tag, branch, or SHA that exists; `list_commits` (the GitHub tool) "
                "shows recent SHAs, and `check_rollout_health` reports the deployed version."
            ),
            details={"version": version, **(exc.details or {})},
        )
    return err(
        exc.error_class,
        exc.code,
        exc.message,
        remediation=exc.remediation,
        details=exc.details,
        retry_after_ms=exc.retry_after_ms,
    )


def _no_repository() -> types.CallToolResult:
    return err(
        "validation",
        "REPOSITORY_NOT_CONFIGURED",
        "No release repository is configured.",
        remediation="Set GITHUB_REPO=owner/name in .env and restart the server.",
        details={"field": "GITHUB_REPO", "expected": "owner/name"},
    )


def fetch_diff(params: dict[str, Any], api: GitHubApi) -> types.CallToolResult:
    repo = api.repository
    if repo is None:
        return _no_repository()
    service, from_version, to_version = (
        params["service"],
        params["from_version"],
        params["to_version"],
    )
    include = params["include"]
    want = {
        section: include in ("all", section)
        for section in ("config", "images", "manifests", "commits")
    }
    need_files = want["config"] or want["images"] or want["manifests"]

    try:
        with Timer() as timer:
            # Resolve each ref on its own so an unknown one is named, not guessed.
            for version in (from_version, to_version):
                try:
                    api.commit(repo, version)
                except GitHubApiError as exc:
                    return _registry_error(exc, version=version)
            comparison = api.compare(repo, from_version, to_version)
            changed = [
                str(f.get("filename"))
                for f in comparison.get("files") or []
                if isinstance(f, dict) and release.classify_path(str(f.get("filename") or ""))
            ]
            truncated = len(changed) > MAX_RELEASE_FILES
            inspected = changed[:MAX_RELEASE_FILES]
            before = release.ReleaseSnapshot()
            after = release.ReleaseSnapshot()
            if need_files:
                before = release.snapshot_release(
                    ((p, api.file_content(repo, p, ref=from_version)) for p in inspected), service
                )
                after = release.snapshot_release(
                    ((p, api.file_content(repo, p, ref=to_version)) for p in inspected), service
                )
    except GitHubApiError as exc:
        return _registry_error(exc)

    diff = release.diff_snapshots(before, after)
    raw_commits = [c for c in comparison.get("commits") or [] if isinstance(c, dict)]
    total_commits = int(comparison.get("total_commits") or len(raw_commits))
    truncated = truncated or total_commits > len(raw_commits)
    commits = [_shape_commit(c) for c in raw_commits[:MAX_COMMITS]]

    data: dict[str, Any] = {
        "service": service,
        "from_version": from_version,
        "to_version": to_version,
        "config_keys_added": diff["config_keys_added"] if want["config"] else None,
        "config_keys_removed": diff["config_keys_removed"] if want["config"] else None,
        "config_keys_changed": diff["config_keys_changed"] if want["config"] else None,
        "image_changes": diff["image_changes"] if want["images"] else None,
        "commits": commits if want["commits"] else None,
        "manifest_changes": diff["manifest_changes"] if want["manifests"] else None,
    }
    returned = sum(len(v) for v in data.values() if isinstance(v, list))
    return ok(
        data,
        returned=returned,
        truncated=truncated,
        total_available=len(changed) if need_files else total_commits,
        query_ms=timer.ms,
        source="github",
        as_of=_stamp(datetime.now(UTC)),
    )


def _shape_commit(raw: dict[str, Any]) -> dict[str, Any]:
    # The subject line only (Day 21): the contract's commit is {sha, message, authored_at},
    # and a message body is prose re-read every round, not a release fact. Part 3 of the
    # description says so, and `diff_refs` is where the rest of a commit is.
    commit = raw.get("commit") or {}
    subject, _, _ = commit_headline(str(commit.get("message") or ""))
    return {
        "sha": str(raw.get("sha") or ""),
        "message": subject,
        "authored_at": (commit.get("author") or {}).get("date"),
    }


# ------------------------------------------------------------------ check_rollout_health


def fetch_health(
    params: dict[str, Any],
    reader: health.PrometheusReader,
    settings: DeploymentSettings,
    *,
    now: datetime | None = None,
) -> types.CallToolResult:
    service, environment = params["service"], params["environment"]
    restricted = restricted_names([service])
    if restricted:
        return ground_truth_denied("service", restricted)
    monitored = settings.monitored_environment()
    if environment != monitored:
        return err(
            "business",
            "ENVIRONMENT_NOT_MONITORED",
            f"This stack is the {monitored!r} environment; it has no {environment!r} deployment "
            "to report on.",
            remediation=(
                f"Ask about environment {monitored!r}, the only one this Prometheus scrapes."
            ),
            details={"environment": environment, "monitored": monitored},
        )

    moment = now or datetime.now(UTC)
    lookback = params["lookback_minutes"]
    try:
        with Timer() as timer:
            reading = health.read_rollout(
                reader,
                service=service,
                lookback_minutes=lookback,
                job=settings.prometheus_job,
                now=moment,
            )
            if reading.desired == 0:
                return err(
                    "business",
                    "UNKNOWN_SERVICE",
                    f"Prometheus scrapes no instance of {service!r}.",
                    remediation=(
                        f"Known services: {reading.known_services or 'none - is the stack up?'}. "
                        "Ask about one of those."
                    ),
                    details={"service": service, "known_services": reading.known_services},
                )
            verdict = health.judge(
                reading, requested_version=params["version"], now_epoch=moment.timestamp()
            )
            if verdict.problem == "VERSION_NOT_DEPLOYED":
                return err(
                    "business",
                    "VERSION_NOT_DEPLOYED",
                    f"Version {params['version']!r} of {service!r} did not run at any point in "
                    f"the last {lookback} minutes.",
                    remediation=(
                        "Omit `version` to assess what is deployed now, or widen "
                        "`lookback_minutes` (up to 1440) if the version ran earlier. Running "
                        f"now: {sorted(set(reading.current_versions.values())) or 'none exported'}."
                    ),
                    details={
                        "service": service,
                        "version": params["version"],
                        "lookback_minutes": lookback,
                        "seen_in_window": sorted(reading.seen_versions),
                    },
                )
            if verdict.problem == "VERSION_NOT_OBSERVABLE":
                return err(
                    "business",
                    "VERSION_NOT_OBSERVABLE",
                    f"{service!r} is up but exports no `service_build_info`, so version "
                    f"{params['version']!r} can be neither confirmed nor ruled out.",
                    remediation=(
                        "Omit `version` to assess the running instances without a version "
                        "identity, or rebuild the demo image (`docker compose build`) so the "
                        "gauge is exported."
                    ),
                    details={"service": service, "version": params["version"]},
                )
            baseline = None
            if verdict.baseline_version is not None and verdict.baseline_at is not None:
                base_error, base_p99 = health.read_baseline(
                    reader, service=service, lookback_minutes=lookback, at_epoch=verdict.baseline_at
                )
                baseline = {
                    "baseline_version": verdict.baseline_version,
                    "error_rate_delta": _delta(reading.error_rate, base_error),
                    "p99_delta_ms": _delta(reading.p99_ms, base_p99),
                }
    except health.PrometheusFailure as exc:
        return err(
            "transient",
            exc.code,
            exc.message,
            remediation=f"Retry after {exc.retry_after_ms // 1000}s; is the stack up (`make up`)?",
            retry_after_ms=exc.retry_after_ms,
            details={"prometheus_url": reader.base_url},
        )

    data = {
        "service": service,
        "environment": environment,
        "version": verdict.version,
        "status": verdict.status,
        "replicas": {
            "desired": reading.desired,
            "ready": reading.ready,
            "updated": verdict.updated,
            "unavailable": reading.desired - reading.ready,
        },
        "signals": {
            "error_rate": _round(reading.error_rate, 6),
            "p50_latency_ms": _round(reading.p50_ms, 3),
            "p99_latency_ms": _round(reading.p99_ms, 3),
            "restart_count": reading.restart_count,
            "probe_failures": reading.probe_failures,
        },
        "compared_to_baseline": baseline,
    }
    return ok(
        data,
        returned=1,
        truncated=False,
        query_ms=timer.ms,
        source="prometheus",
        as_of=_stamp(moment),
    )


def _round(value: float | None, places: int) -> float | None:
    """Float noise (13.900000000000045) is not a measurement; the agent quotes these."""
    return None if value is None else round(value, places)


def _delta(now: float | None, before: float | None) -> float | None:
    if now is None or before is None:
        return None
    return round(now - before, 6)


# --------------------------------------------------------------------------- dispatch


def call(
    name: str,
    arguments: dict[str, Any],
    *,
    api: GitHubApi | None = None,
    reader: health.PrometheusReader | None = None,
    settings: DeploymentSettings | None = None,
    now: datetime | None = None,
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
            exc.code,
            str(exc),
            remediation=f"Correct `{exc.field}` to {exc.expected} and call again.",
            details={"field": exc.field, "expected": exc.expected},
        )
    if name == DIFF_RELEASE:
        return fetch_diff(params, api or GitHubApi())
    settings = settings or DeploymentSettings()
    reader = reader or health.PrometheusReader(
        settings.prometheus_url, timeout_seconds=settings.prometheus_timeout_seconds
    )
    return fetch_health(params, reader, settings, now=now)


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
