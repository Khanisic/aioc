"""Day 13: `analyze_logs` and `analyze_events` (contract sec 6, sec 7.5, sec 7.6).

Offline except the `integration`-marked tests at the end. The log store is faked through the
`LogStore` protocol (a list of lines in, a structured failure out), so the parsing, the
filters, the caps, the pattern detection, and every error class are exercised without
Docker; the compose reader itself is tested against a scripted subprocess. `analyze_events`
runs its pure parts here and its SQL against the seeded corpus under the marker.

The description tests are the case study's baseline guard: these two tools are the Domain 2
routing experiment's independent variable, and their v1.0.0 part 4 is *deliberately* weak.
A helpful edit that names the alternative would silently destroy the before/after
measurement, so the weakness is pinned by a test that Day 14's split is expected to flip.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import types

from aioc.contracts import TimelineEventKind
from aioc.tools.incident import analyze_server as a
from aioc.tools.incident import logs as lg
from aioc.tools.incident import patterns as pt

# ------------------------------------------------------------------------------- helpers

_WINDOW = {"start": "2026-01-22T15:00:00Z", "end": "2026-01-22T16:00:00Z"}
_T0 = datetime(2026, 1, 22, 15, 0, tzinfo=UTC)


def _payload(result: types.CallToolResult) -> dict[str, Any]:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return dict(json.loads(block.text))


def _line(seconds: int, level: str, message: str, line_no: int) -> lg.LogLine:
    return lg.LogLine(
        at=_T0 + timedelta(seconds=seconds), level=level, message=message, line_no=line_no
    )


class _FakeStore:
    """A `LogStore` that returns scripted lines or raises a scripted failure."""

    def __init__(
        self,
        lines: list[lg.LogLine] | None = None,
        *,
        failure: lg.StoreFailure | None = None,
        truncated: bool = False,
    ) -> None:
        self._lines = lines or []
        self._failure = failure
        self._truncated = truncated
        self.reads: list[tuple[str, datetime, datetime]] = []

    def read(self, service: str, start: datetime, end: datetime) -> tuple[list[lg.LogLine], bool]:
        self.reads.append((service, start, end))
        if self._failure is not None:
            raise lg.LogStoreError(self._failure, f"scripted {self._failure}")
        return list(self._lines), self._truncated


_ACCESS = [
    _line(0, "info", '10.0.0.1:5000 - "GET /process HTTP/1.1" 200 OK', 1),
    _line(2, "info", '10.0.0.1:5001 - "GET /process HTTP/1.1" 200 OK', 2),
    _line(4, "error", "Exception in ASGI application", 3),
    _line(4, "other", "Traceback (most recent call last):", 4),
    _line(4, "other", '  File "app.py", line 180, in process', 5),
    _line(5, "info", '10.0.0.1:5002 - "GET /process HTTP/1.1" 502 Bad Gateway', 6),
    _line(7, "warn", "downstream http://payments-api:8000 slow: 2100 ms", 7),
]


def _logs(store: _FakeStore, **args: Any) -> dict[str, Any]:
    return _payload(
        a.call("analyze_logs", {"service": "checkout-api", **_WINDOW, **args}, store=store)
    )


# ----------------------------------------------------------------------- the wire envelope


def test_success_carries_meta_and_the_contract_data_shape():
    p = _logs(_FakeStore(_ACCESS))
    assert p["ok"] is True
    assert set(p["data"]) == {"matches", "total_matched", "patterns_detected"}
    match = p["data"]["matches"][0]
    assert set(match) == {"at", "service", "level", "message", "source_ref"}
    assert match["at"].endswith("Z")
    meta = p["meta"]
    assert meta["returned"] == 7 and meta["truncated"] is False
    assert meta["total_available"] == 7 and meta["source"] == "docker_compose_logs"
    assert meta["token_estimate"] > 0 and meta["query_ms"] is not None


def test_an_empty_window_is_a_success_not_an_error():
    p = _logs(_FakeStore([]))
    assert p["ok"] is True
    assert p["data"]["matches"] == [] and p["data"]["total_matched"] == 0
    assert p["data"]["patterns_detected"] == []
    assert p["meta"]["returned"] == 0


# ------------------------------------------------------------------- filters, caps, patterns


def test_pattern_filters_messages_and_total_counts_every_match():
    p = _logs(_FakeStore(_ACCESS), pattern=r"GET /process")
    assert p["data"]["total_matched"] == 3
    assert [m["source_ref"] for m in p["data"]["matches"]] == [
        "compose:checkout-api:1",
        "compose:checkout-api:2",
        "compose:checkout-api:6",
    ]


def test_level_filter_keeps_only_that_level_and_tracebacks_are_other():
    errors = _logs(_FakeStore(_ACCESS), level="error")["data"]
    assert [m["message"] for m in errors["matches"]] == ["Exception in ASGI application"]
    others = _logs(_FakeStore(_ACCESS), level="other")["data"]
    assert others["total_matched"] == 2
    assert others["matches"][0]["message"].startswith("Traceback")


def test_max_matches_caps_the_list_and_reports_the_real_total():
    p = _logs(_FakeStore(_ACCESS), max_matches=2)
    assert p["meta"]["returned"] == 2
    assert p["data"]["total_matched"] == 7
    assert p["meta"]["truncated"] is True and p["meta"]["total_available"] == 7


def test_a_cut_scan_is_reported_as_truncated_even_when_every_match_is_returned():
    p = _logs(_FakeStore(_ACCESS, truncated=True))
    assert p["meta"]["returned"] == 7 and p["meta"]["truncated"] is True


def test_patterns_detected_groups_by_template_across_all_matches_not_just_returned():
    p = _logs(_FakeStore(_ACCESS), max_matches=1)
    patterns = p["data"]["patterns_detected"]
    assert patterns[0] == {
        "pattern": '<ip> - "GET /process HTTP/1.1" 200 OK',
        "count": 2,
        "first_at": "2026-01-22T15:00:00Z",
        "last_at": "2026-01-22T15:00:02Z",
    }
    assert {pat["pattern"] for pat in patterns} >= {
        '<ip> - "GET /process HTTP/1.1" 502 Bad Gateway',
        "downstream http://payments-api:<n> slow: <n> ms",
    }


def test_the_store_is_asked_for_exactly_the_validated_window():
    store = _FakeStore(_ACCESS)
    _logs(store)
    ((service, start, end),) = store.reads
    assert service == "checkout-api"
    assert start == _T0 and end == _T0 + timedelta(hours=1)


# ---------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("name", "args", "field", "code"),
    [
        ("analyze_logs", {"start": _WINDOW["start"]}, "service", "INVALID_INPUT"),
        ("analyze_logs", {"service": "checkout-api"}, "start", "INVALID_TIME_RANGE"),
        (
            "analyze_logs",
            {"service": "checkout-api", "start": _WINDOW["end"], "end": _WINDOW["start"]},
            "end",
            "INVALID_TIME_RANGE",
        ),
        (
            "analyze_logs",
            {
                "service": "checkout-api",
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-09T00:00:00Z",
            },
            "start",
            "INVALID_TIME_RANGE",
        ),
        (
            "analyze_logs",
            {"service": "checkout-api", **_WINDOW, "pattern": "("},
            "pattern",
            "INVALID_PATTERN",
        ),
        (
            "analyze_logs",
            {"service": "checkout-api", **_WINDOW, "pattern": ""},
            "pattern",
            "INVALID_PATTERN",
        ),
        (
            "analyze_logs",
            {"service": "checkout-api", **_WINDOW, "pattern": "x" * 201},
            "pattern",
            "INVALID_PATTERN",
        ),
        (
            "analyze_logs",
            {"service": "checkout-api", **_WINDOW, "level": "verbose"},
            "level",
            "INVALID_INPUT",
        ),
        (
            "analyze_logs",
            {"service": "checkout-api", **_WINDOW, "max_matches": 0},
            "max_matches",
            "INVALID_INPUT",
        ),
        (
            "analyze_logs",
            {"service": "checkout-api", **_WINDOW, "max_matches": 501},
            "max_matches",
            "INVALID_INPUT",
        ),
        (
            "analyze_logs",
            {"service": "checkout-api", **_WINDOW, "kind": "deploy"},
            "kind",
            "INVALID_INPUT",
        ),
        (
            "analyze_events",
            {"service": "checkout-api", **_WINDOW, "kind": "rollback"},
            "kind",
            "INVALID_INPUT",
        ),
        (
            "analyze_events",
            {"service": "checkout-api", **_WINDOW, "level": "error"},
            "level",
            "INVALID_INPUT",
        ),
        (
            "analyze_events",
            {"service": "checkout-api", **_WINDOW, "pattern": "[a-"},
            "pattern",
            "INVALID_PATTERN",
        ),
    ],
)
def test_invalid_input_is_a_structured_validation_error(
    name: str, args: dict[str, Any], field: str, code: str
):
    p = _payload(a.call(name, args, store=_FakeStore(), dsn="postgresql://x@127.0.0.1:9/x"))
    error = p["error"]
    assert error["class"] == "validation" and error["retryable"] is False
    assert error["code"] == code
    assert error["details"]["field"] == field and error["details"]["expected"]


def test_unknown_tool_is_a_validation_error():
    assert _payload(a.call("analyze_metrics", {}))["error"]["code"] == "UNKNOWN_TOOL"


def test_end_defaults_to_now():
    params = a._validate(
        "analyze_logs",
        {"service": "s", "start": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()},
    )
    assert datetime.now(UTC) - params["end"] < timedelta(seconds=5)


# --------------------------------------------------------------- the four error classes


def test_all_four_error_classes_return_distinctly():
    """One of each class from real code paths, structurally distinguishable."""
    good = {"service": "checkout-api", **_WINDOW}
    validation = _payload(a.call("analyze_logs", {**good, "pattern": "("}, store=_FakeStore()))
    business = _payload(a.call("analyze_logs", good, store=_FakeStore(failure="unknown_service")))
    transient = _payload(a.call("analyze_logs", good, store=_FakeStore(failure="unavailable")))
    permission = _payload(
        a.call("analyze_logs", {**good, "service": "chaos-knobs"}, store=_FakeStore())
    )

    assert validation["error"]["code"] == "INVALID_PATTERN"
    assert business["error"]["code"] == "UNKNOWN_SERVICE"
    assert transient["error"]["code"] == "LOG_STORE_UNAVAILABLE"
    assert permission["error"]["code"] == "CHAOS_SCOPE_REQUIRED"
    classes = {p["error"]["class"] for p in (validation, business, transient, permission)}
    assert classes == {"validation", "business", "transient", "permission"}
    # Only the transient one is retryable, and it says when.
    assert transient["error"]["retryable"] is True and transient["error"]["retry_after_ms"] == 2000
    for p in (validation, business, permission):
        assert p["error"]["retryable"] is False and p["error"]["retry_after_ms"] is None
    assert permission["error"]["details"]["required_scope"] == "eval:ground_truth"


def test_a_docker_timeout_is_transient_and_says_why():
    p = _payload(
        a.call(
            "analyze_logs",
            {"service": "checkout-api", **_WINDOW},
            store=_FakeStore(failure="timeout"),
        )
    )
    assert p["error"]["class"] == "transient" and p["error"]["code"] == "LOG_STORE_UNAVAILABLE"
    assert p["error"]["details"] == {"store": "docker_compose_logs", "reason": "timeout"}


def test_an_unreachable_event_store_is_transient_with_the_contract_code():
    # A refused connection on a closed port: the real psycopg path, no stack needed.
    p = _payload(
        a.call(
            "analyze_events",
            {"service": "checkout-api", **_WINDOW},
            dsn="postgresql://aioc:x@127.0.0.1:9/aioc?connect_timeout=1",
        )
    )
    assert p["error"]["class"] == "transient" and p["error"]["code"] == "EVENT_STORE_UNAVAILABLE"
    assert p["error"]["retry_after_ms"] == 2000 and p["error"]["details"] == {"store": "postgres"}


def test_the_chaos_gate_is_a_permission_error_on_both_tools():
    for name in a.TOOL_NAMES:
        p = _payload(a.call(name, {"service": "chaos-injector", **_WINDOW}, store=_FakeStore()))
        assert p["error"]["class"] == "permission" and p["error"]["code"] == "CHAOS_SCOPE_REQUIRED"


# ---------------------------------------------------------------------- the compose reader


def test_parse_line_reads_docker_timestamps_and_uvicorn_levels():
    raw = (
        '2026-09-13T21:17:42.472428856Z INFO:     127.0.0.1:35134 - "GET /process HTTP/1.1" 200 OK'
    )
    line = lg.parse_line(raw, 7)
    assert line is not None
    assert line.at == datetime(2026, 9, 13, 21, 17, 42, 472428, tzinfo=UTC)
    assert line.level == "info"
    assert line.message == '127.0.0.1:35134 - "GET /process HTTP/1.1" 200 OK'
    assert line.line_no == 7


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("DEBUG: x", "debug"),
        ("TRACE x", "debug"),
        ("INFO:     x", "info"),
        ("WARNING:  x", "warn"),
        ("WARN x", "warn"),
        ("ERROR:    x", "error"),
        ("CRITICAL: x", "fatal"),
        ("FATAL: x", "fatal"),
        ("Traceback (most recent call last):", "other"),
        ("INFORMATION about nothing", "other"),
    ],
)
def test_level_words_map_onto_the_contract_enum(text: str, level: str):
    line = lg.parse_line(f"2026-09-13T21:17:42Z {text}", 1)
    assert line is not None and line.level == level


def test_a_line_without_a_timestamp_is_skipped_not_guessed():
    assert lg.parse_line("INFO: no timestamp here", 1) is None


def _scripted_run(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run.argv = argv  # type: ignore[attr-defined]
        run.kwargs = kwargs  # type: ignore[attr-defined]
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)

    return run


def test_compose_reader_passes_the_window_and_parses_the_output(monkeypatch: pytest.MonkeyPatch):
    stdout = (
        '2026-01-22T15:00:00.000000001Z INFO:     10.0.0.1:1 - "GET /healthz HTTP/1.1" 200 OK\n'
        "2026-01-22T15:00:01Z ERROR:    boom\n"
        "2026-01-22T15:00:01.5Z Traceback (most recent call last):\n"
    )
    run = _scripted_run(stdout=stdout)
    monkeypatch.setattr(lg.subprocess, "run", run)
    store = lg.ComposeLogStore(docker="docker", compose_dir=Path("."), timeout_seconds=3)
    lines, truncated = store.read("checkout-api", _T0, _T0 + timedelta(hours=1))
    assert [line.level for line in lines] == ["info", "error", "other"]
    assert truncated is False
    argv = run.argv  # type: ignore[attr-defined]
    assert argv[:3] == ["docker", "compose", "logs"]
    assert "--since" in argv and argv[argv.index("--since") + 1] == "2026-01-22T15:00:00Z"
    assert "--until" in argv and argv[argv.index("--until") + 1] == "2026-01-22T16:00:00Z"
    assert argv[-1] == "checkout-api"
    assert run.kwargs["timeout"] == 3  # type: ignore[attr-defined]


def test_compose_reader_cuts_the_scan_at_the_limit_and_says_so(monkeypatch: pytest.MonkeyPatch):
    stdout = "".join(f"2026-01-22T15:00:{i % 60:02d}Z INFO: line {i}\n" for i in range(12))
    monkeypatch.setattr(lg.subprocess, "run", _scripted_run(stdout=stdout))
    store = lg.ComposeLogStore(docker="docker", scan_limit=10)
    lines, truncated = store.read("checkout-api", _T0, _T0 + timedelta(hours=1))
    assert len(lines) == 10 and truncated is True


def test_compose_reader_maps_docker_failures_onto_store_failures(monkeypatch: pytest.MonkeyPatch):
    store = lg.ComposeLogStore(docker="docker")
    window = (_T0, _T0 + timedelta(hours=1))

    monkeypatch.setattr(
        lg.subprocess, "run", _scripted_run(stderr="no such service: billing-api", returncode=1)
    )
    with pytest.raises(lg.LogStoreError) as unknown:
        store.read("billing-api", *window)
    assert unknown.value.failure == "unknown_service"

    monkeypatch.setattr(
        lg.subprocess,
        "run",
        _scripted_run(
            stderr="failed to connect to the docker API at npipe:////./pipe/x", returncode=1
        ),
    )
    with pytest.raises(lg.LogStoreError) as down:
        store.read("checkout-api", *window)
    assert down.value.failure == "unavailable"

    def timeout(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(lg.subprocess, "run", timeout)
    with pytest.raises(lg.LogStoreError) as slow:
        store.read("checkout-api", *window)
    assert slow.value.failure == "timeout"


def test_a_missing_docker_binary_is_unavailable_not_a_crash():
    store = lg.ComposeLogStore(docker="/definitely/not/docker")
    with pytest.raises(lg.LogStoreError) as missing:
        store.read("checkout-api", _T0, _T0 + timedelta(hours=1))
    assert missing.value.failure == "unavailable"


def test_log_store_settings_env_file_is_the_repo_root_dotenv():
    env_file = lg.LogStoreSettings.model_config["env_file"]
    assert isinstance(env_file, Path)
    assert (env_file.parent / "pyproject.toml").is_file()
    assert lg.REPO_ROOT == env_file.parent


# ------------------------------------------------------------------------- pattern maths


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('10.0.0.1:5000 - "GET /process HTTP/1.1" 200 OK', '<ip> - "GET /process HTTP/1.1" 200 OK'),
        ("Release 4a91c2e deployed to production", "Release <hex> deployed to production"),
        ("request 3f2504e0-4f89-11d3-9a0c-0305e82c3301 failed", "request <uuid> failed"),
        ("p99 crossed 2100 ms at 2026-01-22T15:12:30Z", "p99 crossed <n> ms at <ts>"),
        ("error_rate 0.31 over 1800s", "error_rate <n> over <n>s"),
        ("exit status 137", "exit status 137"),
        ("  spaced    out  ", "spaced out"),
    ],
)
def test_template_replaces_variable_parts_and_keeps_codes(text: str, expected: str):
    assert pt.template(text) == expected


def test_detect_patterns_counts_brackets_and_ranks_deterministically():
    items = [
        (_T0 + timedelta(seconds=3), "alert fired on 10.0.0.1"),
        (_T0 + timedelta(seconds=1), "restart 1"),
        (_T0 + timedelta(seconds=9), "alert fired on 10.0.0.2"),
        (_T0 + timedelta(seconds=5), "restart 2"),
        (_T0 + timedelta(seconds=7), "scale to 3"),
    ]
    found = pt.detect_patterns(items, limit=2)
    assert found == [
        {
            "pattern": "alert fired on <ip>",
            "count": 2,
            "first_at": "2026-01-22T15:00:03Z",
            "last_at": "2026-01-22T15:00:09Z",
        },
        {
            "pattern": "restart 1",
            "count": 1,
            "first_at": "2026-01-22T15:00:01Z",
            "last_at": "2026-01-22T15:00:01Z",
        },
    ]


def test_detect_patterns_on_nothing_is_empty():
    assert pt.detect_patterns([]) == []


# ------------------------------------------------- the descriptions: the case-study baseline


@pytest.mark.parametrize("name", a.TOOL_NAMES)
def test_descriptions_have_parts_one_to_three_in_order(name: str):
    d = a.DESCRIPTIONS[name]
    assert "Inputs:" in d and "RFC 3339" in d
    examples = [line for line in d.splitlines() if line.strip().startswith('- "')]
    assert len(examples) >= 3, f"template part 2 needs 3+ example queries, found {len(examples)}"
    assert "Edge cases and limits" in d
    assert "When to use this vs" in d
    assert (
        d.index("Example queries")
        < d.index("Edge cases and limits")
        < d.index("When to use this vs")
    )


@pytest.mark.parametrize("name", a.TOOL_NAMES)
def test_descriptions_state_what_an_empty_result_means(name: str):
    d = a.DESCRIPTIONS[name]
    assert "empty" in d.lower() and "NOT an error" in d


@pytest.mark.parametrize("name", a.TOOL_NAMES)
def test_v1_part_four_is_the_contract_sentence_and_names_no_alternative(name: str):
    """The case-study baseline. Part 4 must be the contract's v1.0.0 text verbatim and must
    NOT name the other tool or state the discriminator - that is the 'before' condition
    Day 13 measures. Day 14's pre-authorised 1.1.0 split is expected to make this test
    fail, and to replace it with the 'after' assertion; until then, sharpening these
    descriptions destroys the baseline."""
    part_four = a.DESCRIPTIONS[name].split("When to use this vs")[1]
    assert part_four.strip() == f"the alternative: {a.V1_PART_FOUR[name]}"
    other = "analyze_events" if name == "analyze_logs" else "analyze_logs"
    assert other not in part_four
    assert a.V1_PART_FOUR == {
        "analyze_logs": "Use this to analyze service output over a time window.",
        "analyze_events": "Use this to analyze service activity over a time window.",
    }


def test_the_two_descriptions_overlap_on_purpose_in_shape():
    # Same inputs apart from the level/kind filter, same output shape - the overlap the
    # experiment needs. The discriminator lives in parts 1 and 3 only.
    logs, events = a.LOGS_INPUT_SCHEMA["properties"], a.EVENTS_INPUT_SCHEMA["properties"]
    assert set(logs) - {"level"} == set(events) - {"kind"}
    assert (
        a.LOGS_INPUT_SCHEMA["required"] == a.EVENTS_INPUT_SCHEMA["required"] == ["service", "start"]
    )


# --------------------------------------------------------------------- server hygiene


@pytest.mark.parametrize("name", a.TOOL_NAMES)
def test_input_schemas_forbid_extra_properties(name: str):
    schema = a.INPUT_SCHEMAS[name]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["max_matches"]["maximum"] == a.HARD_MAX_MATCHES


def test_the_server_does_not_import_the_contract_models():
    import ast

    for module in (a, lg, pt):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        offending = [n for n in imported if n.startswith("aioc.contracts")]
        assert not offending, f"{module.__name__} imports {offending}"


def test_the_longhand_enum_copies_match_the_contract():
    assert set(a.EVENTS_INPUT_SCHEMA["properties"]["kind"]["enum"]) - {None} == {
        k.value for k in TimelineEventKind
    }
    # `level` has no Python enum: the contract's sec 7.5 list is the definition.
    assert lg.LOG_LEVELS == ("debug", "info", "warn", "error", "fatal", "other")
    assert set(a.LOGS_INPUT_SCHEMA["properties"]["level"]["enum"]) - {None} == set(lg.LOG_LEVELS)


@pytest.mark.asyncio
async def test_list_tools_exposes_both_tools_with_their_descriptions():
    tools = await a.list_tools()
    assert [t.name for t in tools] == list(a.TOOL_NAMES)
    assert all(t.description == a.DESCRIPTIONS[t.name] for t in tools)
    assert all(t.inputSchema == a.INPUT_SCHEMAS[t.name] for t in tools)


# ------------------------------------------------------------------------- integration

_INC_0002_WINDOW = {"start": "2026-01-20T00:00:00Z", "end": "2026-01-24T00:00:00Z"}


@pytest.fixture(autouse=True)
def _require_reachable_store(request: pytest.FixtureRequest) -> None:
    if "integration" not in request.keywords:
        return
    import psycopg

    from aioc.tools.incident.store import dsn

    try:
        with psycopg.connect(dsn(), connect_timeout=3):
            return
    except psycopg.OperationalError as exc:
        pytest.skip(
            f"incident corpus unreachable ({str(exc).splitlines()[0]}); see test_timeline_tool.py"
        )


@pytest.mark.integration
def test_events_read_the_seeded_corpus_in_order_with_patterns():
    p = _payload(a.call("analyze_events", {"service": "checkout-api", **_INC_0002_WINDOW}))
    assert p["ok"] is True, p
    matches = p["data"]["matches"]
    assert [m["at"] for m in matches] == sorted(m["at"] for m in matches)
    assert all(m["source_ref"].startswith("evt_") for m in matches)
    assert {m["kind"] for m in matches} >= {"deploy", "alert"}
    assert p["data"]["total_matched"] == len(matches) == p["meta"]["returned"]
    assert any(
        pat["pattern"] == "Release <hex> deployed to production"
        for pat in p["data"]["patterns_detected"]
    )


@pytest.mark.integration
def test_events_pattern_and_kind_filters_apply():
    p = _payload(
        a.call(
            "analyze_events",
            {
                "service": "checkout-api",
                **_INC_0002_WINDOW,
                "pattern": "^Release",
                "kind": "deploy",
            },
        )
    )
    assert p["ok"] is True
    assert [m["source_ref"] for m in p["data"]["matches"]] == ["evt_0002_1"]


@pytest.mark.integration
def test_events_report_an_unknown_service_as_business_and_a_quiet_window_as_empty():
    unknown = _payload(a.call("analyze_events", {"service": "billing-api", **_INC_0002_WINDOW}))
    assert unknown["error"]["code"] == "UNKNOWN_SERVICE"
    quiet = _payload(
        a.call(
            "analyze_events",
            {
                "service": "checkout-api",
                "start": "2026-02-01T00:00:00Z",
                "end": "2026-02-05T00:00:00Z",
            },
        )
    )
    assert quiet["ok"] is True and quiet["data"]["matches"] == []
