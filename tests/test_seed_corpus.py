"""Offline checks on the Day 5 incident corpus (`docker/postgres/init/`).

Postgres enforces the schema's own constraints, but only once the stack is up and only against
values it was given. What it cannot catch is *drift*: the `CHECK (... IN (...))` lists are hand-
transcribed from CONTRACTS.md sec 4.1, so adding an enum member in Python leaves the SQL silently
stale, and a mode with no seed rows silently becomes unscoreable in the Day 19 eval.

These tests parse the two `.sql` files as text and check them against the Python enums, so drift
fails `pytest` rather than surfacing days later as bad retrieval or a gap in eval coverage. Same
technique as the chaos injector's module-level `FailureMode` guard, for the same reason.

No database and no Docker: this reads files.
"""

from __future__ import annotations

import re
from pathlib import Path

from aioc.contracts import FailureMode, Severity, TimelineEventKind

_INIT = Path(__file__).resolve().parents[1] / "docker" / "postgres" / "init"
_SCHEMA_SQL = (_INIT / "02-incidents.sql").read_text(encoding="utf-8")
_SEED_SQL = (_INIT / "03-seed-incidents.sql").read_text(encoding="utf-8")

# The plan's Day 5 requirement, quoted: "Seed 15-20 synthetic historical incidents".
_MIN_INCIDENTS, _MAX_INCIDENTS = 15, 20


def _check_list(column: str) -> set[str]:
    """The value set from a `CHECK (<column> IN ('a','b',...))` clause in the schema."""
    match = re.search(
        rf"{column}\s+text\s+NOT NULL\s*\n\s*CHECK \({column} IN \(([^)]*)\)\)", _SCHEMA_SQL
    )
    assert match is not None, f"no CHECK ... IN (...) found for {column}"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _statements(sql: str) -> str:
    """`sql` with `--` comment lines stripped, so tests inspect code and not prose about it."""
    return "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))


# --------------------------------------------------------------- schema vs the contract enums


def test_schema_failure_mode_check_matches_the_contract_enum():
    assert _check_list("true_failure_mode") == {m.value for m in FailureMode}


def test_schema_severity_check_matches_the_contract_enum():
    assert _check_list("true_severity") == {s.value for s in Severity}


def test_schema_timeline_kind_check_matches_the_contract_enum():
    # This one is formatted across lines, so it needs its own extraction.
    match = re.search(r"kind\s+text\s+NOT NULL\s*\n\s*CHECK \(kind IN \(([^)]*)\)\)", _SCHEMA_SQL)
    assert match is not None
    assert set(re.findall(r"'([^']+)'", match.group(1))) == {k.value for k in TimelineEventKind}


# ------------------------------------------------------------------------- seed coverage


def _incident_ids() -> list[str]:
    return re.findall(r"^\('(inc_[a-z0-9_]+)',", _SEED_SQL, re.M)


def _seeded_failure_modes() -> list[str]:
    # The mode is the value immediately preceding true_failure_mode_detail on each row, and every
    # row spells its severity then its mode on one line: 'sev2', 'resource_exhaustion', NULL,
    return re.findall(r"^ '(?:sev[1-4]|other)', '([a-z_]+)',", _SEED_SQL, re.M)


def test_incident_count_is_within_the_plan_range():
    ids = _incident_ids()
    assert _MIN_INCIDENTS <= len(ids) <= _MAX_INCIDENTS, f"seeded {len(ids)}"


def test_incident_ids_are_unique_and_contract_prefixed():
    ids = _incident_ids()
    assert len(ids) == len(set(ids)), "duplicate incident id in the seed"
    # similar_incidents references these, so the prefix is part of the contract (sec 1).
    assert all(i.startswith("inc_") for i in ids)


def test_timeline_event_ids_are_unique_and_contract_prefixed():
    events = re.findall(r"^\('(evt_[a-z0-9_]+)',", _SEED_SQL, re.M)
    assert events, "no timeline events seeded"
    assert len(events) == len(set(events)), "duplicate timeline event id in the seed"


def test_every_failure_mode_has_seed_rows_so_the_eval_can_score_it():
    # A mode with no rows is not a thin spot in the corpus, it is a mode the Day 19 eval cannot
    # score at all - the agent could never be right or wrong about it.
    seeded = set(_seeded_failure_modes())
    missing = {m.value for m in FailureMode} - seeded
    assert not missing, f"no seeded incident for: {sorted(missing)}"


def test_failure_modes_are_valid_contract_members():
    valid = {m.value for m in FailureMode}
    assert set(_seeded_failure_modes()) <= valid


def test_every_seeded_failure_mode_is_represented_at_least_twice():
    # One example per mode makes an eval score a coin flip on that mode. Two is the floor for
    # the score to mean anything; the corpus currently carries four for the non-`other` modes.
    counts = {m: _seeded_failure_modes().count(m) for m in set(_seeded_failure_modes())}
    thin = {m: n for m, n in counts.items() if n < 2}
    assert not thin, f"too few seed rows to score: {thin}"


def test_referenced_services_exist_in_the_demo_stack():
    # An incident naming a service the agent cannot observe teaches it to invent topology.
    # The demo app's three services plus the two datastores in docker-compose.yml.
    known = {"checkout-api", "payments-api", "inventory-api", "postgres", "redis"}
    seeded = set(re.findall(r"'\{([a-z,\-]+)\}'", _SEED_SQL))
    referenced = {svc for group in seeded for svc in group.split(",")}
    assert referenced <= known, f"unknown service(s): {sorted(referenced - known)}"


def test_seed_is_idempotent_so_it_can_be_reapplied_to_hosted_postgres():
    # `init/` never runs against a hosted database (Day 24 applies these files by hand), so
    # re-running the seed must not fail on rows that already exist.
    assert _SEED_SQL.count("ON CONFLICT (id) DO NOTHING") == 2


def test_seed_timestamps_are_absolute_not_relative():
    # Deterministic corpus: two eval runs must score identical data, or the Day 24 comparison
    # against the Day 20 baseline measures the corpus drifting rather than the context work.
    # Comments are stripped first - 03-seed-incidents.sql explains this rule in prose, and the
    # prose mentions the very construct the rule forbids.
    statements = _statements(_SEED_SQL)
    assert "now()" not in statements
    assert "interval" not in statements.lower()


def test_other_failure_mode_rows_carry_a_detail_string():
    # The contract's `other`-plus-detail pattern, checked in the data rather than only in SQL:
    # a row using `other` must explain itself, and the corpus must exercise that path.
    other_rows = re.findall(r"'other', '([^']+)'", _SEED_SQL)
    assert other_rows, "no `other` failure mode seeded - the detail pattern is never exercised"
    assert all(len(detail) > 20 for detail in other_rows), "detail string is too thin to be useful"


# Deliberately NOT tested: that every timeline event falls inside its incident window.
# `evt_0002_1` is a deploy one minute before its incident's `started_at` - the trigger preceding
# the damage. That is the causal link the agent has to make, and the contract validates ascending
# order only (IncidentFindings._check), so containment is neither required nor desirable here.
