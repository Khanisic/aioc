"""Read-only GitHub REST client for the GitHub tool server (Day 11).

One deliberately thin layer: HTTP in, parsed JSON out, and every failure translated into
the contract's four-class taxonomy (sec 6.4) *here*, once, so the server's tools share one
honest error mapping instead of three drifting copies.

The mapping, stated because `code` and `class` are matched programmatically by agents:

- no usable token, 401, or a 403 that is not a rate limit -> `permission`
  (`GITHUB_SCOPE_MISSING`). The token is a fine-grained PAT scoped to read-only
  Contents / Pull requests / Metadata; this class is what an over-scoped token would never
  exercise, which is why `.env.example` insists on read-only.
- 404 -> `business` (`NOT_FOUND`). GitHub returns 404, not 403, for a repository a
  fine-grained token cannot see, so the remediation names both causes.
- 403/429 with the rate limit exhausted -> `transient` (`GITHUB_RATE_LIMITED`) with
  `retry_after_ms` from the `Retry-After` / `X-RateLimit-Reset` headers.
- 5xx, timeouts, connection failures -> `transient` (`GITHUB_UNAVAILABLE`).
- 422 -> `validation` (`GITHUB_REJECTED_INPUT`): the request was well-formed to us and
  GitHub still refused it, so the caller must change the request.

**No `aioc.contracts` import** - the MCP boundary is JSON Schema (contract sec 6).
Settings read the process environment first and the repo `.env` second, the same
precedence every other settings class in the project uses; the `.env` path is resolved
from this file because an MCP client launches the server from wherever it happens to be.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
REQUIRED_SCOPE = "contents:read, pull_requests:read, metadata:read"

ErrorClass = Literal["transient", "validation", "business", "permission"]


class GitHubSettings(BaseSettings):
    """`GITHUB_TOKEN`, `GITHUB_REPO` (owner/name), optional `GITHUB_API_URL`."""

    # parents[4] from src/aioc/tools/github/api.py is the repo root (one level deeper than
    # tools/incident/store.py). A regression test pins the resolved path to the directory
    # pyproject.toml lives in, because a silently wrong path reads as "token not set".
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[4] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    token: SecretStr | None = Field(default=None, validation_alias="GITHUB_TOKEN")
    repository: str | None = Field(default=None, validation_alias="GITHUB_REPO")
    api_url: str = Field(default=DEFAULT_API_URL, validation_alias="GITHUB_API_URL")

    def token_value(self) -> str | None:
        """The token, or ``None`` when unset or blank - an exported empty `GITHUB_TOKEN=`
        must read as missing, not as a token that fails with 401."""
        if self.token is None:
            return None
        value = self.token.get_secret_value().strip()
        return value or None


class GitHubApiError(Exception):
    """A failed GitHub call, already classified. The server turns it into the envelope."""

    def __init__(
        self,
        error_class: ErrorClass,
        code: str,
        message: str,
        *,
        remediation: str,
        details: dict[str, Any] | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.code = code
        self.message = message
        self.remediation = remediation
        self.details = details
        self.retry_after_ms = retry_after_ms


def _retry_after_ms(resp: httpx.Response) -> int:
    """What the headers say, bounded to something a tool loop can actually wait for."""
    retry_after = resp.headers.get("retry-after")
    if retry_after and retry_after.isdigit():
        return max(1000, min(int(retry_after) * 1000, 60_000))
    reset = resp.headers.get("x-ratelimit-reset")
    if reset and reset.isdigit():
        wait = int(reset) - int(datetime.now(UTC).timestamp())
        return max(1000, min(wait * 1000, 60_000))
    return 30_000


def _is_rate_limited(resp: httpx.Response) -> bool:
    if resp.status_code == 429:
        return True
    return resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0"


class GitHubApi:
    """The handful of read endpoints the tools need. ``client`` is injectable for tests
    (an `httpx.Client` with a `MockTransport`), the same pattern as the Voyage client."""

    def __init__(
        self,
        settings: GitHubSettings | None = None,
        *,
        client: httpx.Client | None = None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self._settings = settings or GitHubSettings()
        self._client = client
        self._timeout = timeout_seconds

    @property
    def repository(self) -> str | None:
        return self._settings.repository

    # ------------------------------------------------------------------ endpoints

    def pull_request(self, repo: str, number: int) -> dict[str, Any]:
        return self._get_object(f"/repos/{repo}/pulls/{number}", what=f"pull request #{number}")

    def pull_request_files(self, repo: str, number: int, *, limit: int) -> list[dict[str, Any]]:
        return self._get_list(
            f"/repos/{repo}/pulls/{number}/files", what=f"pull request #{number}", limit=limit
        )

    def pull_request_commits(self, repo: str, number: int, *, limit: int) -> list[dict[str, Any]]:
        return self._get_list(
            f"/repos/{repo}/pulls/{number}/commits", what=f"pull request #{number}", limit=limit
        )

    def commits(
        self,
        repo: str,
        *,
        ref: str | None,
        since: str | None,
        until: str | None,
        path: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if ref:
            params["sha"] = ref
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        if path:
            params["path"] = path
        return self._get_list(
            f"/repos/{repo}/commits", what=f"ref {ref or 'default branch'}", limit=limit, **params
        )

    def commit(self, repo: str, sha: str) -> dict[str, Any]:
        return self._get_object(f"/repos/{repo}/commits/{sha}", what=f"commit {sha}")

    def compare(self, repo: str, base: str, head: str) -> dict[str, Any]:
        return self._get_object(
            f"/repos/{repo}/compare/{base}...{head}", what=f"compare {base}...{head}"
        )

    # ------------------------------------------------------------------ plumbing

    def _headers(self) -> dict[str, str]:
        token = self._settings.token_value()
        if token is None:
            raise GitHubApiError(
                "permission",
                "GITHUB_SCOPE_MISSING",
                "No GITHUB_TOKEN is configured, so GitHub cannot be read.",
                remediation=(
                    "Set GITHUB_TOKEN in .env to a fine-grained personal access token with "
                    f"read-only {REQUIRED_SCOPE} on the target repository."
                ),
                details={"required_scope": REQUIRED_SCOPE, "reason": "token_missing"},
            )
        python = f"{sys.version_info.major}.{sys.version_info.minor}"
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": f"aioc-github-tools (python {python})",
        }

    def _request(self, path: str, *, what: str, **params: Any) -> httpx.Response:
        headers = self._headers()
        try:
            if self._client is not None:
                resp = self._client.get(path, params=params, headers=headers)
            else:
                with httpx.Client(base_url=self._settings.api_url, timeout=self._timeout) as client:
                    resp = client.get(path, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise GitHubApiError(
                "transient",
                "GITHUB_UNAVAILABLE",
                f"GitHub did not answer in time for {what} ({type(exc).__name__}).",
                remediation="Retry after 2s; if it persists, GitHub or the network is down.",
                retry_after_ms=2000,
            ) from exc
        except httpx.HTTPError as exc:
            raise GitHubApiError(
                "transient",
                "GITHUB_UNAVAILABLE",
                f"Could not reach GitHub for {what} ({type(exc).__name__}).",
                remediation="Retry after 2s; check network access to api.github.com.",
                retry_after_ms=2000,
            ) from exc
        self._raise_for_status(resp, what)
        return resp

    def _get_object(self, path: str, *, what: str, **params: Any) -> dict[str, Any]:
        payload = self._request(path, what=what, **params).json()
        if not isinstance(payload, dict):
            raise _unexpected_shape(what)
        return payload

    def _get_list(self, path: str, *, what: str, limit: int, **params: Any) -> list[dict[str, Any]]:
        # One page, capped: these tools feed an agent's context window, not a mirror.
        page = min(max(limit, 1), 100)
        payload = self._request(path, what=what, per_page=page, **params).json()
        if not isinstance(payload, list):
            raise _unexpected_shape(what)
        return [item for item in payload if isinstance(item, dict)][:limit]

    @staticmethod
    def _raise_for_status(resp: httpx.Response, what: str) -> None:
        status = resp.status_code
        if status < 400:
            return
        if _is_rate_limited(resp):
            wait = _retry_after_ms(resp)
            raise GitHubApiError(
                "transient",
                "GITHUB_RATE_LIMITED",
                f"GitHub rate limit exhausted while fetching {what}.",
                remediation=f"Retry after {wait // 1000}s; the limit resets on a rolling hour.",
                retry_after_ms=wait,
                details={"status": status},
            )
        if status in (401, 403):
            raise GitHubApiError(
                "permission",
                "GITHUB_SCOPE_MISSING",
                f"GitHub refused {what} ({status}): the token lacks the required scope or "
                "is invalid.",
                remediation=(
                    "Re-issue the fine-grained token with read-only "
                    f"{REQUIRED_SCOPE} on this repository; no write scope is needed."
                ),
                details={"required_scope": REQUIRED_SCOPE, "status": status},
            )
        if status == 404:
            raise GitHubApiError(
                "business",
                "NOT_FOUND",
                f"GitHub has no {what} (404).",
                remediation=(
                    "Check the number/ref and GITHUB_REPO. A fine-grained token answers 404 "
                    "(not 403) for a repository it was not granted, so also confirm the "
                    "token's repository access."
                ),
                details={"status": status},
            )
        if status == 422:
            detail = ""
            try:
                detail = str(resp.json().get("message", ""))
            except ValueError:
                pass
            raise GitHubApiError(
                "validation",
                "GITHUB_REJECTED_INPUT",
                f"GitHub rejected the request for {what}: {detail or 'unprocessable'}.",
                remediation="Change the request - GitHub will refuse it identically on retry.",
                details={"field": "input", "expected": detail or "a request GitHub accepts"},
            )
        if status >= 500:
            raise GitHubApiError(
                "transient",
                "GITHUB_UNAVAILABLE",
                f"GitHub returned {status} for {what}.",
                remediation="Retry after 2s.",
                retry_after_ms=2000,
                details={"status": status},
            )
        raise GitHubApiError(
            "business",
            "GITHUB_ERROR",
            f"GitHub returned {status} for {what}.",
            remediation="Inspect the status; this is not a retryable condition.",
            details={"status": status},
        )


def _unexpected_shape(what: str) -> GitHubApiError:
    return GitHubApiError(
        "transient",
        "GITHUB_UNAVAILABLE",
        f"GitHub returned an unexpected shape for {what}.",
        remediation="Retry once; a repeat is a bug in the tool, not the request.",
        retry_after_ms=1000,
    )
