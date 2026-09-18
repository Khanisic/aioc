"""Day 14: `search_container_logs` and `search_recorded_events` - the routing case study's
"after" (contract sec 7.5 / 7.6 at 1.1.0).

Offline. The v1 module (`analyze_server`) keeps its own suite, including the test that pins
its part 4 to the deliberately weak v1.0.0 sentence - that is the baseline and it must stay
reproducible. This file pins the other side of the experiment: the 1.1.0 server exposes the
same tools under names that carry the discriminator, parts 1-3 of each description are the
v1 text verbatim, part 4 names the alternative, and the implementation is the v1 one - so
the independent variable is exactly the name plus part 4, and nothing else moved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import types

from aioc.tools.incident import analyze_server as v1
from aioc.tools.incident import search_server as s
from tests.test_analyze_tools import _ACCESS, _WINDOW, _FakeStore

# ------------------------------------------------------------------------------- helpers


def _payload(result: types.CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return dict(json.loads(block.text))


# --------------------------------------------------------- the split: name + part 4 only


def test_the_two_tools_map_one_to_one_onto_the_v1_tools():
    assert s.TOOL_NAMES == ("search_container_logs", "search_recorded_events")
    assert set(s.V1_NAMES) == set(s.TOOL_NAMES)
    assert set(s.V1_NAMES.values()) == set(v1.TOOL_NAMES)
    assert s.SERVER_NAME != v1.SERVER_NAME


@pytest.mark.parametrize("name", s.TOOL_NAMES)
def test_parts_one_to_three_are_the_v1_text_verbatim(name: str):
    """Only the variable changed. If someone sharpens part 1 or 3 here, the before/after
    comparison no longer isolates the name and part 4."""
    assert s.parts_one_to_three(s.DESCRIPTIONS[name]) == s.parts_one_to_three(
        v1.DESCRIPTIONS[s.V1_NAMES[name]]
    )


@pytest.mark.parametrize("name", s.TOOL_NAMES)
def test_part_four_names_the_alternative_and_states_the_discriminator(name: str):
    """The 'after' assertion: the mirror image of the v1 test that pins part 4 weak."""
    part_four = s.DESCRIPTIONS[name].split("When to use this vs")[1]
    other = next(n for n in s.TOOL_NAMES if n != name)
    assert f"`{other}`" in part_four
    assert part_four.strip() == s.PART_FOUR[name].split("When to use this vs")[1].strip()
    # The v1 sentence is gone, and the discriminator is stated in both directions.
    assert v1.V1_PART_FOUR[s.V1_NAMES[name]] not in s.DESCRIPTIONS[name]
    assert "printed" in part_four or "wrote" in part_four
    assert "recorded" in part_four
    assert "not the word it uses" in part_four


@pytest.mark.parametrize("name", s.TOOL_NAMES)
def test_descriptions_follow_the_four_part_template_in_order(name: str):
    d = s.DESCRIPTIONS[name]
    assert "Inputs:" in d and "RFC 3339" in d
    examples = [line for line in d.splitlines() if line.strip().startswith('- "')]
    assert len(examples) >= 3
    assert (
        d.index("Example queries")
        < d.index("Edge cases and limits")
        < d.index("When to use this vs")
    )
    assert "empty" in d.lower() and "NOT an error" in d


@pytest.mark.parametrize("name", s.TOOL_NAMES)
def test_input_schemas_are_the_v1_schemas(name: str):
    assert s.INPUT_SCHEMAS[name] is v1.INPUT_SCHEMAS[s.V1_NAMES[name]]
    assert s.INPUT_SCHEMAS[name]["additionalProperties"] is False


# ------------------------------------------------------------- the implementation is v1's


def test_search_container_logs_returns_what_analyze_logs_returns():
    store_new, store_old = _FakeStore(_ACCESS), _FakeStore(_ACCESS)
    args = {"service": "checkout-api", **_WINDOW, "level": "error"}
    new = _payload(s.call("search_container_logs", args, store=store_new))
    old = _payload(v1.call("analyze_logs", args, store=store_old))
    assert new["data"] == old["data"]
    assert new["meta"]["returned"] == old["meta"]["returned"] == 1
    assert store_new.reads == store_old.reads


def test_an_unknown_service_remediation_names_the_1_1_0_sibling():
    store = _FakeStore(failure="unknown_service")
    p = _payload(s.call("search_container_logs", {"service": "ghost", **_WINDOW}, store=store))
    assert p["ok"] is False
    assert p["error"]["class"] == "business" and p["error"]["code"] == "UNKNOWN_SERVICE"
    assert "`search_recorded_events`" in p["error"]["remediation"]
    assert "analyze_events" not in p["error"]["remediation"]


def test_validation_errors_are_structured_with_the_contract_codes():
    bad_pattern = _payload(
        s.call("search_recorded_events", {"service": "checkout-api", **_WINDOW, "pattern": "("})
    )
    assert bad_pattern["error"]["class"] == "validation"
    assert bad_pattern["error"]["code"] == "INVALID_PATTERN"
    assert bad_pattern["error"]["details"]["field"] == "pattern"

    bad_level = _payload(
        s.call("search_container_logs", {"service": "checkout-api", **_WINDOW, "level": "loud"})
    )
    assert bad_level["error"]["code"] == "INVALID_INPUT"
    assert bad_level["error"]["details"]["field"] == "level"

    # The v1 names are not this server's names - a caller on the old contract is told so.
    unknown = _payload(s.call("analyze_logs", {"service": "checkout-api", **_WINDOW}))
    assert unknown["error"]["code"] == "UNKNOWN_TOOL"
    assert unknown["error"]["details"]["expected"] == list(s.TOOL_NAMES)


@pytest.mark.parametrize("name", s.TOOL_NAMES)
def test_the_chaos_gate_is_a_permission_error_on_both_tools(name: str):
    p = _payload(s.call(name, {"service": "chaos-injector", **_WINDOW}, store=_FakeStore()))
    assert p["ok"] is False and p["error"]["class"] == "permission"
    assert p["error"]["code"] == "CHAOS_SCOPE_REQUIRED"


# --------------------------------------------------------------------- server hygiene


def test_the_server_does_not_import_the_contract_models():
    import ast

    tree = ast.parse(Path(s.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [n for n in imported if n.startswith("aioc.contracts")]


@pytest.mark.asyncio
async def test_list_tools_exposes_both_tools_with_their_descriptions():
    tools = await s.list_tools()
    assert [t.name for t in tools] == list(s.TOOL_NAMES)
    assert all(t.description == s.DESCRIPTIONS[t.name] for t in tools)
    assert all(t.inputSchema == s.INPUT_SCHEMAS[t.name] for t in tools)
