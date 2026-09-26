"""Day 16 - schemas everywhere: the four agents' structured-output schemas, side by side.

Each agent's own test file pins its schema. This file pins what must hold for all four at
once, because the Day 16 audit found the one way they can drift apart unnoticed: guidance
copied into four modules, where one copy (the `Gap` fields) disagreed with the validator it
describes. A model that followed that guidance - `suggested_agent` set, `suggested_query`
null because the gap was "not resolvable" - produced a report the contract rejects.

What is pinned, for every agent's forced emit tool:
  - every object forbids extra properties (the Day 15 live Docs refusal was an invented
    field; a schema that says so up front is the cheapest defence);
  - every `*_detail` field carries the `other`-pairing guidance;
  - the `Gap` guidance states the `suggested_agent` -> `suggested_query` rule the
    validator enforces, and the Day 16 roster rule about siblings' parts.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from aioc.agents import deployment, docs, github, incident

SCHEMAS: dict[str, dict[str, Any]] = {
    "incident": incident._EMIT_SCHEMA,
    "docs": docs._EMIT_SCHEMA,
    "github": github._EMIT_SCHEMA,
    "deployment": deployment._EMIT_SCHEMA,
}


def _objects(schema: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    yield "ROOT", schema
    for name, definition in schema.get("$defs", {}).items():
        if definition.get("type") == "object":
            yield name, definition


@pytest.mark.parametrize("agent", SCHEMAS)
def test_every_object_forbids_extra_properties(agent: str):
    for name, obj in _objects(SCHEMAS[agent]):
        assert obj.get("additionalProperties") is False, f"{agent}: {name} admits extras"


@pytest.mark.parametrize("agent", SCHEMAS)
def test_every_detail_field_states_the_other_pairing(agent: str):
    unguided = [
        f"{name}.{field}"
        for name, obj in _objects(SCHEMAS[agent])
        for field, prop in obj.get("properties", {}).items()
        if field.endswith("_detail") and "other" not in prop.get("description", "")
    ]
    assert unguided == [], f"{agent}: detail fields without the other-pairing rule"


@pytest.mark.parametrize("agent", SCHEMAS)
def test_gap_guidance_matches_the_validator_and_the_roster_rule(agent: str):
    gap = SCHEMAS[agent]["$defs"]["Gap"]["properties"]
    query = gap["suggested_query"]["description"]
    assert "Required whenever `suggested_agent` is set" in query
    # The guidance that disagreed with the validator is gone.
    assert "when `resolvable` is true" not in query
    who = gap["suggested_agent"]["description"]
    assert "already on this request" in who


def test_the_four_agents_give_identical_gap_guidance():
    texts = {
        agent: {
            field: SCHEMAS[agent]["$defs"]["Gap"]["properties"][field]["description"]
            for field in ("suggested_agent", "suggested_query")
        }
        for agent in SCHEMAS
    }
    assert len({repr(t) for t in texts.values()}) == 1, texts
