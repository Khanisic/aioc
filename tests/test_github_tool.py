"""Tests for the GitHub tool server (Day 11): envelope, four-class error taxonomy, input
validation, keys-only redaction, output bounds, and the four-part description template -
all offline, with GitHub played by an `httpx.MockTransport`. No key, no network.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import types

from aioc.contracts import PullRequestState
from aioc.tools.github import api as gh_api
from aioc.tools.github import server as gs
from aioc.tools.github.api import GitHubApi, GitHubApiError, GitHubSettings

# --------------------------------------------------------------------------- helpers


def _payload(result: types.CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    payload = json.loads(block.text)
    assert payload["ok"] is (not result.isError)
    return payload


def _settings(token: str | None = "ghp_test", repo: str | None = "o/r") -> GitHubSettings:
    return GitHubSettings(token=token, repository=repo, api_url="https://api.test")  # type: ignore[arg-type]


def _api(handler: Any, **settings: Any) -> GitHubApi:
    client = httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return GitHubApi(_settings(**settings), client=client)


def _pr_json(number: int = 12, *, merged: bool = True, draft: bool = False) -> dict[str, Any]:
    return {
        "number": number,
        "title": "Day 10 demo",
        "state": "closed" if merged else "open",
        "draft": draft,
        "merged": merged,
        "merged_at": "2026-08-22T03:36:15Z" if merged else None,
        "created_at": "2026-08-22T03:00:00Z",
        "user": {"login": "m"},
        "head": {"sha": "37b6cc02d8" + "0" * 30, "ref": "d10"},
        "base": {"ref": "main"},
        "html_url": f"https://github.com/o/r/pull/{number}",
        "changed_files": 2,
        "additions": 10,
        "deletions": 3,
        "body": "Adds the demo.",
    }


_PATCH = (
    "@@ -1,3 +1,3 @@\n"
    "-DATABASE_URL=postgresql://aioc:secret@localhost:5432/aioc\n"
    "+DATABASE_URL=postgresql://aioc:secret@localhost:55432/aioc\n"
    " POSTGRES_USER=aioc\n"
    "+token = ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456\n"
    "+def f():\n"
)


def _files_json() -> list[dict[str, Any]]:
    return [
        {
            "filename": ".env.example",
            "status": "modified",
            "additions": 2,
            "deletions": 1,
            "patch": _PATCH,
        },
        {"filename": "docs/x.gif", "status": "added", "additions": 0, "deletions": 0},
    ]


def _commit_json(sha: str, message: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "html_url": f"https://github.com/o/r/commit/{sha}",
        "commit": {"message": message, "author": {"date": "2026-08-22T03:35:07Z"}},
    }


def _happy(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/repos/o/r/pulls/12":
        return httpx.Response(200, json=_pr_json())
    if path == "/repos/o/r/pulls/12/files":
        return httpx.Response(200, json=_files_json())
    if path == "/repos/o/r/pulls/12/commits":
        return httpx.Response(200, json=[_commit_json("a" * 40, "Day 10 demo (#12)")])
    if path == "/repos/o/r/commits":
        n = int(request.url.params.get("per_page", "30"))
        commits = [_commit_json(f"{i:x}" * 40, f"commit {i}") for i in range(n)]
        return httpx.Response(200, json=commits)
    if path.startswith("/repos/o/r/commits/"):
        return httpx.Response(200, json={"files": [{"filename": "a.py"}, {"filename": "b.py"}]})
    if path == "/repos/o/r/compare/v1...v2":
        return httpx.Response(
            200,
            json={
                "status": "ahead",
                "ahead_by": 1,
                "behind_by": 0,
                "total_commits": 1,
                "commits": [_commit_json("b" * 40, "Merge pull request #11 from x")],
                "files": _files_json(),
            },
        )
    return httpx.Response(404, json={"message": "Not Found"})


# ------------------------------------------------------------------- success shapes


def test_get_pull_request_returns_the_envelope_with_meta_and_mapped_state():
    p = _payload(gs.call("get_pull_request", {"number": 12}, api=_api(_happy)))
    pr = p["data"]["pull_request"]
    assert pr["number"] == 12 and pr["state"] == "merged"  # GitHub said closed + merged
    assert pr["head_sha"].startswith("37b6cc02d8") and pr["files_changed"] == 2
    assert pr["touched_paths"] == [".env.example", "docs/x.gif"]
    assert p["data"]["commits"][0]["pull_request_number"] == 12
    assert p["data"]["files"][0]["path"] == ".env.example"
    assert "patch" not in p["data"]["files"][0]  # not asked for
    meta = p["meta"]
    assert meta["source"] == "github" and meta["returned"] == 2 and meta["truncated"] is False
    assert meta["token_estimate"] > 0 and meta["query_ms"] is not None


@pytest.mark.parametrize(
    ("merged", "draft", "expected"),
    [(True, False, "merged"), (False, True, "draft"), (False, False, "open")],
)
def test_pull_request_state_mapping(merged: bool, draft: bool, expected: str):
    assert gs.pull_request_state(_pr_json(merged=merged, draft=draft)) == expected


def test_list_commits_is_bounded_and_reports_truncation_honestly():
    p = _payload(gs.call("list_commits", {"max_commits": 3}, api=_api(_happy)))
    assert len(p["data"]["commits"]) == 3
    assert p["meta"]["returned"] == 3 and p["meta"]["truncated"] is True  # a 4th existed
    assert p["data"]["paths_included"] is False
    assert p["data"]["commits"][0]["touched_paths"] == []  # "not asked", not "none"


def test_list_commits_include_paths_fetches_each_commit():
    p = _payload(
        gs.call("list_commits", {"max_commits": 2, "include_paths": True}, api=_api(_happy))
    )
    assert all(c["touched_paths"] == ["a.py", "b.py"] for c in p["data"]["commits"])


def test_diff_refs_returns_commits_and_files():
    p = _payload(gs.call("diff_refs", {"base": "v1", "head": "v2"}, api=_api(_happy)))
    assert p["data"]["ahead_by"] == 1 and p["data"]["total_commits"] == 1
    assert p["data"]["commits"][0]["pull_request_number"] == 11
    assert p["meta"]["returned"] == 2 and p["meta"]["truncated"] is False


def test_identical_refs_is_an_empty_success_not_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "identical",
                "ahead_by": 0,
                "behind_by": 0,
                "total_commits": 0,
                "commits": [],
                "files": [],
            },
        )

    p = _payload(gs.call("diff_refs", {"base": "v1", "head": "v1"}, api=_api(handler)))
    assert p["ok"] is True and p["data"]["files"] == [] and p["meta"]["returned"] == 0


# ------------------------------------------------------------------------ redaction


def test_config_values_are_redacted_keys_kept_and_counted():
    p = _payload(
        gs.call("get_pull_request", {"number": 12, "include_patch": True}, api=_api(_happy))
    )
    patch = p["data"]["files"][0]["patch"]
    assert "secret" not in patch and "55432" not in patch
    assert "-DATABASE_URL=<redacted>" in patch and "+DATABASE_URL=<redacted>" in patch
    assert " POSTGRES_USER=aioc" in patch  # context lines untouched
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456" not in patch and "<redacted>" in patch
    assert "+def f():" in patch
    assert p["data"]["redacted_lines"] == 3
    assert p["data"]["files"][1]["patch"] is None  # binary: no patch, not an error


def test_redact_patch_leaves_code_alone():
    text, n = gs.redact_patch("+x = 1\n+MAX_RETRIES = 3\n+    return value\n")
    assert n == 1 and "+x = 1" in text and "+MAX_RETRIES = <redacted>" in text


def test_patches_are_clipped_per_file_and_flagged():
    big = "+" + "a" * (gs.MAX_PATCH_CHARS + 100)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "f",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 0,
                        "patch": big,
                    }
                ],
            )
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=_pr_json())

    p = _payload(
        gs.call("get_pull_request", {"number": 12, "include_patch": True}, api=_api(handler))
    )
    assert p["data"]["files"][0]["patch_truncated"] is True and p["meta"]["truncated"] is True


# ---------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("name", "args", "field"),
    [
        ("get_pull_request", {}, "number"),
        ("get_pull_request", {"number": 0}, "number"),
        ("get_pull_request", {"number": 12, "bogus": 1}, "bogus"),
        ("get_pull_request", {"number": 12, "max_files": 1000}, "max_files"),
        ("get_pull_request", {"number": 12, "include_patch": "yes"}, "include_patch"),
        ("list_commits", {"since": "yesterday"}, "since"),
        (
            "list_commits",
            {"since": "2026-08-02T00:00:00Z", "until": "2026-08-01T00:00:00Z"},
            "until",
        ),
        ("list_commits", {"max_commits": 0}, "max_commits"),
        ("diff_refs", {"base": "v1"}, "head"),
        ("diff_refs", {"base": "", "head": "v2"}, "base"),
    ],
)
def test_invalid_input_is_a_structured_validation_error(
    name: str, args: dict[str, Any], field: str
):
    p = _payload(gs.call(name, args, api=_api(_happy)))
    err = p["error"]
    assert err["class"] == "validation" and err["retryable"] is False
    assert err["details"]["field"] == field and err["details"]["expected"]


def test_unknown_tool_is_a_validation_error():
    p = _payload(gs.call("nope", {}, api=_api(_happy)))
    assert p["error"]["code"] == "UNKNOWN_TOOL"


def test_bare_z_timestamps_are_accepted_and_forwarded():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.url.params)
        return httpx.Response(200, json=[])

    _payload(gs.call("list_commits", {"since": "2026-08-01T00:00:00Z"}, api=_api(handler)))
    assert seen["since"] == "2026-08-01T00:00:00Z"


def test_missing_repository_is_a_validation_error_naming_the_variable():
    p = _payload(gs.call("get_pull_request", {"number": 12}, api=_api(_happy, repo=None)))
    assert p["error"]["code"] == "REPOSITORY_NOT_CONFIGURED"
    assert p["error"]["details"]["field"] == "GITHUB_REPO"


# --------------------------------------------------------------- the four error classes


def test_all_four_error_classes_return_distinctly():
    """One error of each class from real code paths, structurally distinguishable."""
    validation = _payload(gs.call("get_pull_request", {}, api=_api(_happy)))
    permission = _payload(gs.call("get_pull_request", {"number": 12}, api=_api(_happy, token=None)))
    business = _payload(gs.call("get_pull_request", {"number": 404}, api=_api(_happy)))
    transient = _payload(
        gs.call(
            "get_pull_request",
            {"number": 12},
            api=_api(lambda r: httpx.Response(503, json={"message": "down"})),
        )
    )

    by_class = {p["error"]["class"]: p for p in (validation, permission, business, transient)}
    assert set(by_class) == {"validation", "permission", "business", "transient"}
    assert by_class["transient"]["error"]["retryable"] is True
    assert by_class["transient"]["error"]["retry_after_ms"] is not None
    for cls in ("validation", "permission", "business"):
        assert by_class[cls]["error"]["retryable"] is False
        assert by_class[cls]["error"]["retry_after_ms"] is None
    assert {"field", "expected"} <= set(by_class["validation"]["error"]["details"])
    assert by_class["permission"]["error"]["code"] == "GITHUB_SCOPE_MISSING"
    assert "required_scope" in by_class["permission"]["error"]["details"]
    assert by_class["business"]["error"]["code"] == "NOT_FOUND"
    assert by_class["business"]["error"]["remediation"]
    for p in by_class.values():
        assert p["ok"] is False


def test_a_blank_token_reads_as_missing_not_as_a_bad_credential():
    # `GITHUB_TOKEN=` exported empty must not become a 401 against GitHub.
    assert _settings(token="   ").token_value() is None
    with pytest.raises(GitHubApiError) as exc:
        _api(_happy, token="").pull_request("o/r", 12)
    assert exc.value.error_class == "permission"
    assert exc.value.details == {"required_scope": gh_api.REQUIRED_SCOPE, "reason": "token_missing"}


@pytest.mark.parametrize("status", [401, 403])
def test_refused_calls_are_permission_errors(status: int):
    with pytest.raises(GitHubApiError) as exc:
        _api(lambda r: httpx.Response(status, json={"message": "bad"})).pull_request("o/r", 1)
    assert exc.value.error_class == "permission" and exc.value.code == "GITHUB_SCOPE_MISSING"


def test_an_exhausted_rate_limit_is_transient_with_the_headers_wait():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, headers={"x-ratelimit-remaining": "0", "retry-after": "7"}, json={}
        )

    with pytest.raises(GitHubApiError) as exc:
        _api(handler).pull_request("o/r", 1)
    assert exc.value.error_class == "transient" and exc.value.code == "GITHUB_RATE_LIMITED"
    assert exc.value.retry_after_ms == 7000


def test_a_timeout_is_transient():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(GitHubApiError) as exc:
        _api(handler).pull_request("o/r", 1)
    assert exc.value.error_class == "transient" and exc.value.retry_after_ms == 2000


def test_a_422_is_a_validation_error_carrying_githubs_reason():
    with pytest.raises(GitHubApiError) as exc:
        _api(lambda r: httpx.Response(422, json={"message": "No commit found for SHA"})).compare(
            "o/r", "x", "y"
        )
    assert exc.value.error_class == "validation"
    assert exc.value.details == {"field": "input", "expected": "No commit found for SHA"}


def test_the_token_is_sent_as_a_bearer_with_the_pinned_api_version():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=_pr_json())

    _api(handler).pull_request("o/r", 12)
    assert seen["authorization"] == "Bearer ghp_test"
    assert seen["x-github-api-version"] == gh_api.API_VERSION


# ------------------------------------------------------------ descriptions and drift


@pytest.mark.parametrize("name", gs.TOOL_NAMES)
def test_descriptions_follow_the_four_part_template_in_order(name: str):
    text = gs.DESCRIPTIONS[name]
    examples = text.index("Example queries this tool answers:")
    edges = text.index("Edge cases and limits:")
    when = text.index("When to use this vs. the alternative:")
    assert 0 < examples < edges < when
    assert text[examples:edges].count('- "') >= 3
    # Part 4 names real alternatives by tool name, not "another tool".
    alternatives = {"get_pull_request", "list_commits", "diff_refs", "diff_release"} - {name}
    assert any(alt in text[when:] for alt in alternatives)


def test_every_schema_matches_its_tool_and_rejects_unknown_fields():
    for name in gs.TOOL_NAMES:
        assert gs.SCHEMAS[name]["additionalProperties"] is False


def test_the_server_does_not_import_the_contract_models():
    for module in (gs, gh_api):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[arg-type]
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not [n for n in imported if n.startswith("aioc.contracts")], module.__name__


def test_the_longhand_state_copy_matches_the_contract_enum():
    assert set(gs.PULL_REQUEST_STATES) == {m.value for m in PullRequestState}


def test_pull_request_number_is_parsed_from_squash_and_merge_markers():
    assert gs.pull_request_number_from_message("Fix thing (#12)\n\nbody") == 12
    assert gs.pull_request_number_from_message("Merge pull request #7 from x/y") == 7
    assert gs.pull_request_number_from_message("plain commit") is None
    assert gs.pull_request_number_from_message("first line\n(#9) on second") is None


def test_settings_env_file_is_the_repo_root_dotenv():
    # Same regression the retrieval settings pinned: one directory deeper than store.py, so
    # the parents[] index differs, and a wrong one reads as "token not set".
    env_file = GitHubSettings.model_config["env_file"]
    assert isinstance(env_file, Path)
    assert env_file.name == ".env"
    assert (env_file.parent / "pyproject.toml").is_file()


@pytest.mark.asyncio
async def test_list_tools_exposes_the_three_tools_with_their_descriptions():
    tools = await gs.list_tools()
    assert [t.name for t in tools] == list(gs.TOOL_NAMES)
    assert all(t.description == gs.DESCRIPTIONS[t.name] for t in tools)
