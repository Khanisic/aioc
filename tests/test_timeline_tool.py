"""Day 6: the `get_incident_timeline` MCP server (contract sec 6, sec 7.1).

Split by what needs what. Everything about the wire envelope, the four-class error taxonomy,
the input validation, and the description template is checkable offline and lives here
unmarked. The handful of tests that actually query the seeded corpus are marked `integration`,
so `make test` stays free and fast while a full run proves the SQL too.

The description-template tests deserve a word. Part 4 of the template ("when to use this vs.
the alternative") is the intervention the Day 13/14 routing case study *measures*, so a
description silently losing it would invalidate the case study's baseline. That makes it worth
a test rather than a review comment.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mcp import types

from aioc.contracts import Severity, TimelineEventKind
from aioc.tools.envelope import err, ok
from aioc.tools.incident import timeline_server as ts

# ------------------------------------------------------------------------------- helpers


def _payload(result: types.CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return dict(json.loads(block.text))


def _call(**args: Any) -> types.CallToolResult:
    """Drive the validation path without a server or a database.

    Callers below pass `end` explicitly even when testing an unrelated rule. Omitting it
    defaults `end` to *now*, and with a `start` in the seeded corpus's range that produces a
    months-long window, so `INVALID_TIME_RANGE` fires first and masks the rule under test.
    That is correct tool behaviour - the 7-day cap is in the contract - but it makes the
    default a trap for historical queries, which is why every fixture here is explicit.
    """
    try:
        ts._validate(args)
    except ts._Invalid as exc:
        return err(
            "validation",
            "INVALID_TIME_RANGE" if exc.field in {"start", "end"} else "INVALID_INPUT",
            str(exc),
            remediation=f"Correct `{exc.field}` to {exc.expected} and call again.",
            details={"field": exc.field, "expected": exc.expected},
        )
    return ok({"events": []}, returned=0, truncated=False)


# ----------------------------------------------------------- the contract's wire envelope


def test_success_and_error_always_agree_with_is_error():
    # `isError` disagreeing with `ok` is the failure the taxonomy exists to prevent: an agent
    # cannot retry, escalate, or record a gap against an error it cannot see.
    good = ok({"events": []}, returned=0, truncated=False)
    assert good.isError is False
    assert _payload(good)["ok"] is True

    bad = err("business", "UNKNOWN_SERVICE", "no such service", remediation="check the name")
    assert bad.isError is True
    assert _payload(bad)["ok"] is False


def test_meta_is_present_on_every_success_with_non_null_required_fields():
    # meta is what the Day 21 trimming and Day 24 token-reduction work are computed from.
    meta = _payload(ok({"events": []}, returned=0, truncated=False))["meta"]
    for required in ("truncated", "returned", "token_estimate"):
        assert meta[required] is not None, required
    # These four are explicitly allowed to be null.
    assert set(meta) >= {"total_available", "query_ms", "source", "as_of"}


def test_an_empty_result_is_a_success_not_an_error():
    # "Looked and found nothing" is an answer. A business error means the request cannot be
    # computed, which is a different thing the agent must handle differently.
    result = ok({"events": [], "window": {}, "services_covered": []}, returned=0, truncated=False)
    assert result.isError is False
    assert _payload(result)["meta"]["returned"] == 0


def test_only_transient_errors_are_retryable():
    assert (
        _payload(err("transient", "X", "m", retry_after_ms=1000, remediation="retry in 1s"))[
            "error"
        ]["retryable"]
        is True
    )
    for cls in ("validation", "business", "permission"):
        details = {"field": "f", "expected": "e"} if cls == "validation" else None
        if cls == "permission":
            details = {"required_scope": "repo:read"}
        payload = _payload(err(cls, "X", "m", remediation="do something else", details=details))  # type: ignore[arg-type]
        assert payload["error"]["retryable"] is False
        assert payload["error"]["retry_after_ms"] is None


def test_transient_without_a_retry_hint_is_refused_at_construction():
    # Better to fail building the response than to emit a retryable error with no delay, which
    # invites a hot retry loop against an upstream that is already struggling.
    with pytest.raises(ValueError, match="retry_after_ms"):
        err("transient", "X", "m", remediation="retry")


def test_validation_errors_must_name_the_field_and_the_expectation():
    with pytest.raises(ValueError, match=r"details\."):
        err("validation", "X", "m", remediation="fix it")


def test_permission_errors_must_name_the_required_scope():
    with pytest.raises(ValueError, match="required_scope"):
        err("permission", "X", "m", remediation="grant the scope")


def test_every_error_needs_a_remediation():
    with pytest.raises(ValueError, match="remediation"):
        err("business", "X", "m", remediation="   ")


# ------------------------------------------------------------------- input validation


def test_end_before_start_is_a_validation_error_naming_the_field():
    payload = _payload(
        _call(service="checkout-api", start="2026-03-02T19:00:00Z", end="2026-03-02T18:00:00Z")
    )
    assert payload["error"]["class"] == "validation"
    assert payload["error"]["code"] == "INVALID_TIME_RANGE"
    assert payload["error"]["details"]["field"] == "end"
    assert payload["error"]["retryable"] is False


def test_a_window_longer_than_seven_days_is_rejected():
    payload = _payload(
        _call(service="checkout-api", start="2026-01-01T00:00:00Z", end="2026-02-01T00:00:00Z")
    )
    assert payload["error"]["code"] == "INVALID_TIME_RANGE"
    assert "7" in payload["error"]["details"]["expected"]


def test_a_missing_required_field_is_rejected():
    assert _payload(_call(service="checkout-api"))["error"]["details"]["field"] == "start"
    assert _payload(_call(start="2026-03-02T18:00:00Z"))["error"]["details"]["field"] == "service"


def test_an_unknown_input_field_is_rejected_rather_than_ignored():
    # Silently dropping an unrecognised field lets a caller believe a filter applied when it
    # did not, which is worse than an error - the result looks plausible and is wrong.
    payload = _payload(_call(service="checkout-api", start="2026-03-02T18:00:00Z", severity="sev1"))
    assert payload["error"]["details"]["field"] == "severity"


def test_an_unknown_event_kind_is_rejected():
    payload = _payload(
        _call(
            service="checkout-api",
            start="2026-03-02T18:00:00Z",
            end="2026-03-02T20:00:00Z",
            event_kinds=["deploy", "explosion"],
        )
    )
    assert payload["error"]["class"] == "validation"
    assert "explosion" in payload["error"]["message"]


def test_max_events_out_of_range_is_rejected():
    for value in (0, 201):
        payload = _payload(
            _call(
                service="checkout-api",
                start="2026-03-02T18:00:00Z",
                end="2026-03-02T20:00:00Z",
                max_events=value,
            )
        )
        assert payload["error"]["details"]["field"] == "max_events"


def test_a_bare_z_timestamp_is_accepted():
    # The contract requires RFC 3339 with an explicit Z, and `fromisoformat` has historically
    # not accepted it - so the normalisation has to be tested, not assumed.
    params = ts._validate(
        {"service": "checkout-api", "start": "2026-03-02T18:00:00Z", "end": "2026-03-02T20:00:00Z"}
    )
    assert params["start"].tzinfo is not None
    assert params["start"].isoformat().endswith("+00:00")


def test_end_defaults_to_now_when_omitted():
    # Only usable when `start` is inside the 7-day cap, since the default is *now*.
    recent = (
        (datetime.now(UTC) - timedelta(hours=1))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    params = ts._validate({"service": "checkout-api", "start": recent})
    assert params["end"] > params["start"]


def test_omitting_end_on_a_historical_start_hits_the_window_cap():
    # Documented consequence rather than a bug: `end` defaults to now and the contract caps
    # the window at 7 days, so a historical query MUST pass `end`. The corpus spans January
    # to June, so this is the common caller mistake and it fails loudly with the field named.
    payload = _payload(_call(service="checkout-api", start="2026-01-14T12:00:00Z"))
    assert payload["error"]["code"] == "INVALID_TIME_RANGE"
    assert payload["error"]["details"]["field"] == "start"


# --------------------------------------------------------- no contract import, no drift


def test_the_server_does_not_import_the_contract_models():
    # The MCP boundary is JSON Schema, not Pydantic (contract sec 6). A tool server importing
    # the reasoning layer's models couples the two halves the wire format keeps separate.
    #
    # Checked against the parsed import statements, not the raw text: the module docstring
    # explains this rule and therefore contains the very string a substring search looks for.
    import ast
    from pathlib import Path

    for module in (ts, __import__("aioc.tools.envelope", fromlist=["x"])):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        offending = [name for name in imported if name.startswith("aioc.contracts")]
        assert not offending, f"{module.__name__} imports {offending}"


def test_the_longhand_enum_copies_match_the_contract():
    # The duplication is deliberate, so it needs a guard: without this, adding an enum member
    # in Python leaves the tool silently rejecting valid input.
    assert set(ts.TIMELINE_EVENT_KINDS) == {k.value for k in TimelineEventKind}
    assert set(ts.SEVERITIES) == {s.value for s in Severity if s is not Severity.OTHER}


def test_severity_ranking_is_ordinal_and_complete():
    assert set(ts._SEVERITY_RANK) == set(ts.SEVERITIES)
    # sev1 is most severe, so it must sort lowest for a `<=` min_severity filter to work.
    assert ts._SEVERITY_RANK["sev1"] < ts._SEVERITY_RANK["sev4"]


# ------------------------------------------------------- the four-part description template


def test_description_has_all_four_template_parts_in_order():
    d = ts.DESCRIPTION
    # 1: what it does + input formats
    assert "Inputs:" in d and "RFC 3339" in d
    # 2: at least three example queries
    example_lines = [line for line in d.splitlines() if line.strip().startswith('- "')]
    assert len(example_lines) >= 3, (
        f"template part 2 needs 3+ example queries, found {len(example_lines)}"
    )
    # 3: edge cases and limits
    assert "Edge cases and limits" in d
    # 4: when to use vs the alternative
    assert "When to use this vs" in d
    # ...and in that order
    assert (
        d.index("Example queries")
        < d.index("Edge cases and limits")
        < d.index("When to use this vs")
    )


def test_description_part_four_names_the_competing_tool_explicitly():
    # Part 4 is the intervention the Day 13/14 routing case study measures. A part 4 that does
    # not name its alternative is the "before" condition, not the "after" one.
    part_four = ts.DESCRIPTION.split("When to use this vs")[1]
    assert "correlate_events" in part_four
    assert "get_incident_timeline" in part_four


def test_description_states_what_an_empty_result_means():
    # The contract calls this out specifically: an empty result is a success, and the agent
    # must not read it as proof that nothing happened.
    assert "empty" in ts.DESCRIPTION.lower()
    assert "NOT an error" in ts.DESCRIPTION


def test_input_schema_forbids_extra_properties_and_declares_required():
    assert ts.INPUT_SCHEMA["additionalProperties"] is False
    assert set(ts.INPUT_SCHEMA["required"]) == {"service", "start"}


# ------------------------------------------------------------------------- integration


# The corpus spans January to June, and the contract caps a window at 7 days, so an
# integration fixture has to bracket a known incident rather than sweep the whole corpus.
# inc_0002 is a checkout-api regression on 2026-01-22 with four recorded events.
_INC_0002_WINDOW = {"start": "2026-01-20T00:00:00Z", "end": "2026-01-24T00:00:00Z"}


@pytest.fixture(autouse=True)
def _require_reachable_store(request: pytest.FixtureRequest) -> None:
    """Skip an integration test when the corpus is unreachable, and say why.

    Not laziness about a red suite: the tool correctly reports an unreachable store as a
    `transient` error, so a connectivity problem otherwise surfaces as assertion failures
    about a missing `meta` key - which says nothing about the actual cause. The diagnosis
    below is the one that cost real time to find the first time.
    """
    if "integration" not in request.keywords:
        return

    import psycopg

    from aioc.tools.incident.timeline_server import _dsn

    try:
        with psycopg.connect(_dsn(), connect_timeout=3):
            return
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"incident corpus unreachable ({str(exc).splitlines()[0]}). Checks, in order: is "
            "the stack up (`docker compose up -d --wait`)? Then - is another Postgres already "
            "listening on the host port? A native Windows/macOS install on 5432 wins the race "
            "against Docker's proxy and rejects the `aioc` role, which looks exactly like a "
            "wrong password even though every credential matches. Two LISTENING pids in "
            "`netstat -ano | grep :5432` confirm it; fix by setting POSTGRES_PORT and "
            "DATABASE_URL to a free port in .env."
        )


@pytest.mark.integration
def test_fetch_returns_ordered_events_from_the_seeded_corpus():
    result = ts.fetch_timeline(ts._validate({"service": "checkout-api", **_INC_0002_WINDOW}))
    payload = _payload(result)
    assert payload["ok"] is True, payload
    ats = [e["at"] for e in payload["data"]["events"]]
    assert ats == sorted(ats), "the contract rejects a timeline that is not ascending"
    assert payload["meta"]["source"] == "postgres"


@pytest.mark.integration
def test_fetch_reports_an_unknown_service_as_a_business_error():
    result = ts.fetch_timeline(ts._validate({"service": "billing-api", **_INC_0002_WINDOW}))
    payload = _payload(result)
    assert payload["error"]["class"] == "business"
    assert payload["error"]["code"] == "UNKNOWN_SERVICE"
    assert payload["error"]["retryable"] is False


@pytest.mark.integration
def test_fetch_returns_empty_success_for_a_known_service_with_no_events_in_window():
    # The distinction that matters: same shape as above but a *success*, because the service
    # exists and simply has nothing recorded in this window.
    result = ts.fetch_timeline(
        ts._validate(
            {
                "service": "checkout-api",
                "start": "2026-02-01T00:00:00Z",
                "end": "2026-02-05T00:00:00Z",
            }
        )
    )
    payload = _payload(result)
    assert payload["ok"] is True
    assert payload["data"]["events"] == []
    assert payload["meta"]["returned"] == 0


@pytest.mark.integration
def test_fetch_truncates_and_reports_the_real_total():
    result = ts.fetch_timeline(
        ts._validate({"service": "checkout-api", **_INC_0002_WINDOW, "max_events": 1})
    )
    payload = _payload(result)
    assert payload["meta"]["returned"] == 1
    if payload["meta"]["total_available"] > 1:
        assert payload["meta"]["truncated"] is True
