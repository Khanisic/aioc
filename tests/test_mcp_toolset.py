"""The MCP client seam (Day 11), over the real stdio wire.

These tests launch the actual `aioc-github` server as a subprocess and talk MCP to it,
so the wire - initialisation, tool listing, the call round trip, `isError` propagation -
is what is under test. They stay offline by blanking `GITHUB_TOKEN` in the child's
environment: the server's first action on any call is the token check, which returns a
structured `permission` error before any HTTP request could be made.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from aioc.llm import McpStdioToolset, McpToolsetError, ToolResult
from aioc.tools.deployment import server as ds
from aioc.tools.github import server as gs

_OFFLINE_ENV = {
    **os.environ,
    "GITHUB_TOKEN": "",  # blank reads as missing (tested in test_github_tool)
    "GITHUB_REPO": "offline/test",
    "GITHUB_API_URL": "http://127.0.0.1:9",  # never reached; belt and braces
}


@pytest.fixture(scope="module")
def toolset():
    with McpStdioToolset.for_module(gs.__name__, env=_OFFLINE_ENV) as ts:
        yield ts


def test_the_server_initialises_and_lists_its_tools_over_stdio(toolset: McpStdioToolset):
    assert toolset.server_name == gs.SERVER_NAME
    names = [t.name for t in toolset.tools]
    assert names == list(gs.TOOL_NAMES)
    # The descriptions travel the wire intact - part 4 of the template included.
    by_name = {t.name: t for t in toolset.tools}
    assert by_name["get_pull_request"].description == gs.GET_PULL_REQUEST_DESCRIPTION
    assert by_name["diff_refs"].input_schema["required"] == ["base", "head"]


def test_a_tool_error_arrives_as_a_structured_envelope_with_is_error(toolset: McpStdioToolset):
    result = toolset.call("get_pull_request", {"number": 12})
    assert isinstance(result, ToolResult) and result.is_error is True
    payload = json.loads(result.content)
    assert payload["ok"] is False
    assert payload["error"]["class"] == "permission"
    assert payload["error"]["code"] == "GITHUB_SCOPE_MISSING"


def test_the_tool_spec_handler_is_the_wire_call(toolset: McpStdioToolset):
    spec = next(t for t in toolset.tools if t.name == "list_commits")
    out = spec.handler({"max_commits": 1})
    assert isinstance(out, ToolResult) and out.is_error is True
    assert json.loads(out.content)["error"]["class"] == "permission"


def test_validation_errors_come_back_structured_not_as_framework_text(toolset: McpStdioToolset):
    result = toolset.call("get_pull_request", {"number": "twelve"})
    payload = json.loads(result.content)
    assert result.is_error and payload["error"]["class"] == "validation"
    assert payload["error"]["details"]["field"] == "number"


def test_calls_can_be_made_from_another_thread(toolset: McpStdioToolset):
    # The executor's parallel group runs agents on worker threads; the toolset's loop
    # thread must serve them regardless of which thread asks.
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: toolset.call("diff_refs", {"base": "a", "head": "b"}), range(2))
        )
    assert all(json.loads(r.content)["error"]["class"] == "permission" for r in results)


# ------------------------------------------------- Day 12: the deployment server, same wire


@pytest.fixture(scope="module")
def deployment_toolset():
    env = {
        **_OFFLINE_ENV,
        "PROMETHEUS_URL": "http://127.0.0.1:9",
        "PROMETHEUS_TIMEOUT_SECONDS": "1",
    }
    with McpStdioToolset.for_module(ds.__name__, env=env) as ts:
        yield ts


def test_the_deployment_server_lists_both_contract_tools_over_stdio(
    deployment_toolset: McpStdioToolset,
):
    assert deployment_toolset.server_name == ds.SERVER_NAME
    by_name = {t.name: t for t in deployment_toolset.tools}
    assert list(by_name) == list(ds.TOOL_NAMES)
    assert by_name["diff_release"].description == ds.DIFF_RELEASE_DESCRIPTION
    assert by_name["check_rollout_health"].input_schema["required"] == ["service", "environment"]


def test_deployment_errors_arrive_structured_over_the_wire(deployment_toolset: McpStdioToolset):
    # No token: diff_release is a permission error under its own sec 7.3 code.
    diff = deployment_toolset.call(
        "diff_release", {"service": "s", "from_version": "a", "to_version": "b"}
    )
    assert diff.is_error is True
    assert json.loads(diff.content)["error"]["code"] == "REGISTRY_SCOPE_MISSING"
    # No Prometheus: check_rollout_health is transient, with a retry hint, not framework text.
    health = deployment_toolset.call(
        "check_rollout_health", {"service": "checkout-api", "environment": "development"}
    )
    assert health.is_error is True
    error = json.loads(health.content)["error"]
    assert error["class"] == "transient" and error["retry_after_ms"] is not None
    # And validation is structured too.
    bad = deployment_toolset.call("check_rollout_health", {"service": "s"})
    assert json.loads(bad.content)["error"]["details"]["field"] == "environment"


def test_a_server_that_cannot_start_raises_clearly():
    with pytest.raises(McpToolsetError, match="failed to start|did not initialise"):
        with McpStdioToolset(
            sys.executable, ["-c", "import sys; sys.exit(3)"], startup_timeout_s=10
        ):
            pass


def test_a_closed_toolset_refuses_calls():
    with McpStdioToolset.for_module(gs.__name__, env=_OFFLINE_ENV) as ts:
        pass
    with pytest.raises(McpToolsetError, match="not open"):
        ts.call("list_commits", {})
