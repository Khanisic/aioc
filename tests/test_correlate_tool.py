"""Day 7: the `correlate_events` MCP server (contract sec 6, sec 7.2).

Same split as the timeline tests: the wire envelope, the four-class error taxonomy, the
validation, the correlation math, and the description template are checkable offline and
live here unmarked; the handful that query the seeded corpus are marked `integration`.

The day's done-when - *all four error classes return distinctly* - has a single direct test
(`test_all_four_error_classes_return_distinctly`) that produces one error of each class from
this server's real code paths and asserts the four payloads are structurally distinguishable,
not just differently worded.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mcp import types

from aioc.contracts import TimelineEventKind
from aioc.tools import policy
from aioc.tools.incident import correlate_server as cs

# ------------------------------------------------------------------------------- helpers


def _payload(result: types.CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return dict(json.loads(block.text))


def _call(**args: Any) -> types.CallToolResult:
    """Drive the full handler (validation + policy) without a database, via the real
    `call_tool` entry point. Only used for inputs that fail before data access."""
    return asyncio.run(cs.call_tool(cs.TOOL_NAME, args))


_ANCHOR_AT = datetime(2026, 1, 22, 15, 12, 30, tzinfo=UTC)


def _event(
    event_id: str, offset_seconds: int, *, service: str = "payments-api", kind: str = "alert"
) -> dict[str, Any]:
    return {
        "id": event_id,
        "at": _ANCHOR_AT + timedelta(seconds=offset_seconds),
        "service": service,
        "kind": kind,
    }


# ----------------------------------------------------------------------- input validation


def test_supplying_both_anchor_forms_is_ambiguous():
    payload = _payload(
        _call(anchor_event_id="evt_1", anchor_at="2026-01-22T15:12:30Z", anchor_service="x")
    )
    assert payload["error"]["class"] == "validation"
    assert payload["error"]["code"] == "AMBIGUOUS_ANCHOR"
    assert payload["error"]["retryable"] is False


def test_supplying_neither_anchor_form_is_ambiguous():
    payload = _payload(_call(window_seconds=900))
    assert payload["error"]["code"] == "AMBIGUOUS_ANCHOR"
    assert "neither" in payload["error"]["message"]


def test_anchor_at_without_anchor_service_names_the_missing_field():
    payload = _payload(_call(anchor_at="2026-01-22T15:12:30Z"))
    assert payload["error"]["class"] == "validation"
    assert payload["error"]["details"]["field"] == "anchor_service"


def test_anchor_service_alongside_anchor_event_id_is_rejected():
    # The event carries its own service; a conflicting anchor_service would be silently
    # ignored otherwise, and a silently ignored input is worse than an error.
    payload = _payload(_call(anchor_event_id="evt_1", anchor_service="checkout-api"))
    assert payload["error"]["details"]["field"] == "anchor_service"


def test_a_non_evt_anchor_id_is_rejected():
    payload = _payload(_call(anchor_event_id="inc_0002"))
    assert payload["error"]["class"] == "validation"
    assert "evt_" in payload["error"]["details"]["expected"]


def test_window_seconds_out_of_range_is_rejected():
    for value in (59, 21601):
        payload = _payload(
            _call(anchor_at="2026-01-22T15:12:30Z", anchor_service="x", window_seconds=value)
        )
        assert payload["error"]["details"]["field"] == "window_seconds"


def test_unknown_signal_type_is_rejected():
    payload = _payload(_call(anchor_event_id="evt_1", signal_types=["metric", "vibes"]))
    assert "vibes" in payload["error"]["message"]


def test_min_correlation_out_of_range_is_rejected():
    payload = _payload(_call(anchor_event_id="evt_1", min_correlation=1.5))
    assert payload["error"]["details"]["field"] == "min_correlation"


def test_an_unknown_input_field_is_rejected_rather_than_ignored():
    payload = _payload(_call(anchor_event_id="evt_1", correlation_floor=0.5))
    assert payload["error"]["details"]["field"] == "correlation_floor"


# ----------------------------------------------------------------- the permission boundary


def test_chaos_services_return_a_permission_error_not_data():
    # The injector's signals are the Day 19 eval's injected ground truth. The class is
    # `permission` - the signals exist, the caller lacks the scope - and the payload names
    # the scope, so an agent can record an insufficient_permission Gap instead of retrying.
    payload = _payload(_call(anchor_event_id="evt_1", services=["payments-api", "chaos-injector"]))
    assert payload["error"]["class"] == "permission"
    assert payload["error"]["code"] == "CHAOS_SCOPE_REQUIRED"
    assert payload["error"]["details"]["required_scope"] == policy.REQUIRED_SCOPE
    assert payload["error"]["retryable"] is False


def test_a_chaos_anchor_service_is_equally_denied():
    payload = _payload(_call(anchor_at="2026-01-22T15:12:30Z", anchor_service="chaos-injector"))
    assert payload["error"]["class"] == "permission"


def test_the_timeline_server_enforces_the_same_policy():
    # One namespace, one answer, both tools - a boundary that only one tool enforces is a
    # boundary an agent can route around.
    from aioc.tools.incident import timeline_server as ts

    args = {
        "service": "chaos-injector",
        "start": "2026-03-02T18:00:00Z",
        "end": "2026-03-02T20:00:00Z",
    }
    result = asyncio.run(ts.call_tool(ts.TOOL_NAME, args))
    payload = _payload(result)
    assert payload["error"]["class"] == "permission"
    assert payload["error"]["code"] == "CHAOS_SCOPE_REQUIRED"


# ------------------------------------------------------- the done-when: four distinct classes


def test_all_four_error_classes_return_distinctly():
    """One error of each class from this server's real paths, structurally distinguishable."""
    validation = _payload(_call())  # no anchor at all
    permission = _payload(_call(anchor_event_id="evt_1", services=["chaos-injector"]))
    business = _payload(
        cs.fetch_correlations(cs._validate({"anchor_event_id": "evt_1", "window_seconds": 300}))
    )  # 300s window -> 5 aligned bins, refused before any data access
    transient = _payload(
        cs.fetch_correlations(
            cs._validate({"anchor_event_id": "evt_1"}),
            # An unresolvable host fails in milliseconds; a closed port would sit out the
            # full connect timeout on Windows and slow the whole offline suite.
            dsn="postgresql://aioc:wrong@nonexistent.invalid/aioc",
        )
    )

    by_class = {p["error"]["class"]: p for p in (validation, permission, business, transient)}
    assert set(by_class) == {"validation", "permission", "business", "transient"}

    # Structurally distinct, not just differently worded (contract sec 6.4):
    assert by_class["transient"]["error"]["retryable"] is True
    assert by_class["transient"]["error"]["retry_after_ms"] is not None
    for cls in ("validation", "permission", "business"):
        assert by_class[cls]["error"]["retryable"] is False
        assert by_class[cls]["error"]["retry_after_ms"] is None
    assert {"field", "expected"} <= set(by_class["validation"]["error"]["details"])
    assert "required_scope" in by_class["permission"]["error"]["details"]
    assert by_class["business"]["error"]["remediation"]  # an alternative, not a shrug
    # And isError always agrees with ok.
    for p in by_class.values():
        assert p["ok"] is False


# --------------------------------------------------------------------- correlation (pure)


def test_a_signal_clustered_at_the_anchor_is_coincident_with_high_correlation():
    events = [
        _event("evt_a", 5),
        _event("evt_b", 20),
        _event("evt_c", -900, kind="deploy"),  # noise well before the anchor
    ]
    correlations, _ = cs.correlate(
        events, anchor_at=_ANCHOR_AT, window_seconds=1800, min_correlation=0.5
    )
    top = correlations[0]
    assert (top["service"], top["signal"]) == ("payments-api", "alert")
    assert top["direction"] == "coincident"
    assert top["lag_seconds"] == 0
    assert top["correlation"] > 0.9
    assert top["sample_size"] == 2


def test_a_signal_peaking_before_the_anchor_leads_with_negative_lag():
    events = [_event("evt_d", -300, kind="deploy", service="checkout-api")]
    correlations, _ = cs.correlate(
        events, anchor_at=_ANCHOR_AT, window_seconds=1800, min_correlation=0.5
    )
    (only,) = correlations
    assert only["signal"] == "deploy"
    assert only["direction"] == "leads"
    assert only["lag_seconds"] == -300


def test_the_anchor_event_itself_is_excluded():
    # An event always aligns perfectly with its own occurrence; reporting that tautology
    # would put a fake 1.0 at the top of every anchored query.
    events = [_event("evt_anchor", 0)]
    correlations, candidates = cs.correlate(
        events,
        anchor_at=_ANCHOR_AT,
        window_seconds=1800,
        anchor_event_id="evt_anchor",
        min_correlation=0.0,
    )
    assert correlations == [] and candidates == 0


def test_signal_types_filter_uses_the_kind_mapping():
    events = [
        _event("evt_e", 10, kind="deploy"),
        _event("evt_f", 10, kind="log_pattern"),
    ]
    correlations, _ = cs.correlate(
        events,
        anchor_at=_ANCHOR_AT,
        window_seconds=1800,
        signal_types=["log_rate"],
        min_correlation=0.0,
    )
    assert [c["signal"] for c in correlations] == ["log_pattern"]


def test_min_correlation_filters_but_total_available_reports_all_candidates():
    # A concentrated signal aligns perfectly at some lag; a signal smeared evenly across the
    # window aligns with nothing. min_correlation separates the two, and the smeared one
    # still counts in total_available so the caller knows what the filter dropped.
    events = [_event("evt_g", 0)] + [
        _event(f"evt_h{i}", offset, kind="deploy")
        for i, offset in enumerate(range(-840, 841, 240))  # spread across 8 bins
    ]
    kept, candidates = cs.correlate(
        events, anchor_at=_ANCHOR_AT, window_seconds=1800, min_correlation=0.9
    )
    assert candidates == 2
    assert [c["signal"] for c in kept] == ["alert"]


def test_results_are_deterministically_ordered_best_first():
    events = [
        _event("evt_i", 0, service="payments-api"),
        _event("evt_j", 400, service="checkout-api", kind="restart"),
        _event("evt_k", 0, service="inventory-api"),
    ]
    kept, _ = cs.correlate(events, anchor_at=_ANCHOR_AT, window_seconds=1800, min_correlation=0.0)
    scores = [c["correlation"] for c in kept]
    assert scores == sorted(scores, reverse=True)
    # All three are single-spike signals scoring 1.0, so ordering rests entirely on the
    # service-then-signal tie-break - repeat runs cannot reorder.
    assert [(c["service"], c["signal"]) for c in kept] == [
        ("checkout-api", "restart"),
        ("inventory-api", "alert"),
        ("payments-api", "alert"),
    ]


# --------------------------------------------------------- no contract import, no drift


def test_the_server_does_not_import_the_contract_models():
    # The MCP boundary is JSON Schema, not Pydantic (contract sec 6). Checked on parsed
    # imports because the docstrings mention the forbidden module by name.
    import ast
    from pathlib import Path

    from aioc.tools.incident import store

    for module in (cs, policy, store):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        offending = [name for name in imported if name.startswith("aioc.contracts")]
        assert not offending, f"{module.__name__} imports {offending}"


def test_the_longhand_signal_types_match_the_contract():
    # Contract sec 7.2: metric, log_rate, event, deploy, other.
    assert cs.SIGNAL_TYPES == ("metric", "log_rate", "event", "deploy", "other")


def test_every_event_kind_maps_to_a_signal_type():
    # An unmapped kind would silently vanish from results - absence reading as "nothing
    # moved" when the truth is "the tool dropped it".
    assert set(cs.KIND_TO_SIGNAL_TYPE) == {k.value for k in TimelineEventKind}
    assert set(cs.KIND_TO_SIGNAL_TYPE.values()) <= set(cs.SIGNAL_TYPES)


# ------------------------------------------------------- the four-part description template


def test_description_has_all_four_template_parts_in_order():
    d = cs.DESCRIPTION
    assert "Inputs:" in d and "RFC 3339" in d
    example_lines = [line for line in d.splitlines() if line.strip().startswith('- "')]
    assert len(example_lines) >= 3
    assert "Edge cases and limits" in d
    assert "When to use this vs" in d
    assert (
        d.index("Example queries")
        < d.index("Edge cases and limits")
        < d.index("When to use this vs")
    )


def test_description_part_four_names_both_alternatives():
    # Part 4 is the routing case study's intervention: it must name the competitors and the
    # discriminator, and sec 7.2's part 4 names two of them.
    part_four = cs.DESCRIPTION.split("When to use this vs")[1]
    assert "get_incident_timeline" in part_four
    assert "diff_release" in part_four


def test_description_says_correlation_is_not_causation():
    # Required by sec 7.2 in as many words: agents that read alignment as causality produce
    # confidently wrong root causes.
    assert "NOT CAUSATION" in cs.DESCRIPTION


def test_description_states_what_an_empty_result_means():
    assert "empty" in cs.DESCRIPTION.lower()
    assert "NOT an error" in cs.DESCRIPTION


def test_input_schema_forbids_extra_properties():
    assert cs.INPUT_SCHEMA["additionalProperties"] is False


# ------------------------------------------------------------------------- integration
#
# The corpus spans January to June; inc_0002 (checkout-api, 2026-01-22) has four recorded
# events, so a 6-hour window centred inside it is the densest bracket the seed offers.


@pytest.fixture(autouse=True)
def _require_reachable_store(request: pytest.FixtureRequest) -> None:
    """Skip integration tests when the corpus is unreachable - same diagnosis as the
    timeline suite, which is where the port-collision story is written down."""
    if "integration" not in request.keywords:
        return

    import psycopg

    from aioc.tools.incident.store import dsn

    try:
        with psycopg.connect(dsn(), connect_timeout=3):
            return
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"incident corpus unreachable ({str(exc).splitlines()[0]}); see "
            "tests/test_timeline_tool.py for the port-collision diagnosis"
        )


@pytest.mark.integration
def test_anchoring_on_a_seeded_event_returns_success_with_the_anchor_echoed():
    result = cs.fetch_correlations(
        cs._validate({"anchor_event_id": "evt_0002_2", "window_seconds": 21600})
    )
    payload = _payload(result)
    assert payload["ok"] is True, payload
    assert payload["data"]["anchor"]["event_id"] == "evt_0002_2"
    assert payload["data"]["anchor"]["service"] == "checkout-api"
    assert payload["data"]["method"] == cs.METHOD
    assert payload["meta"]["source"] == "postgres"
    # inc_0002's deploy (evt_0002_1, 90s before the alert) must move with the anchor.
    deploys = [c for c in payload["data"]["correlations"] if c["signal"] == "deploy"]
    assert deploys, payload["data"]["correlations"]


@pytest.mark.integration
def test_an_unknown_anchor_event_is_a_business_error():
    result = cs.fetch_correlations(cs._validate({"anchor_event_id": "evt_never_recorded"}))
    payload = _payload(result)
    assert payload["error"]["class"] == "business"
    assert payload["error"]["code"] == "UNKNOWN_EVENT"
    assert payload["error"]["retryable"] is False


@pytest.mark.integration
def test_a_quiet_window_is_an_empty_success_not_an_error():
    # 2026-02-03 sits between seeded incidents: the corpus records nothing near it, so the
    # honest answer is "looked, nothing moved" - ok: true, returned: 0.
    result = cs.fetch_correlations(
        cs._validate(
            {
                "anchor_at": "2026-02-03T12:00:00Z",
                "anchor_service": "checkout-api",
                "window_seconds": 3600,
            }
        )
    )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["correlations"] == []
    assert payload["meta"]["returned"] == 0
