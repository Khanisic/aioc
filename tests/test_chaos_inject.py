"""Unit tests for the chaos injector's plan mapping (Day 4, Engineer B).

The HTTP side needs the running stack (that is the ``make chaos-<mode>`` integration path); what
is checkable offline is the part that matters for correctness and for the Day 19 eval: that the
failure-mode -> knob mapping is complete, internally consistent, and unambiguous.

``inject.py`` is a standalone script under ``demo-app/`` rather than an installed package, so it
is loaded by path. Importing it also exercises its module-level ``FailureMode`` 1:1 guard.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from aioc.contracts import FailureMode

_INJECT = Path(__file__).resolve().parents[1] / "demo-app" / "chaos" / "inject.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("chaos_inject", _INJECT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves cls.__module__ via sys.modules, and a path-loaded
    # module isn't added there automatically.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


inject = _load()


def test_plans_cover_exactly_the_non_other_failure_modes():
    # Every FailureMode except `other` has a plan; `other` has none by design (no chaos for it).
    expected = {m for m in FailureMode if m is not FailureMode.OTHER}
    assert set(inject.PLANS) == expected


def test_every_plan_is_internally_consistent():
    for mode, plan in inject.PLANS.items():
        assert plan.mode is mode  # keyed by its own mode
        assert plan.knobs, f"{mode.value} sets no knobs"
        for service, knobs in plan.knobs.items():
            assert service in inject._SERVICES, f"{mode.value} targets unknown service {service}"
            assert set(knobs) <= set(inject.KNOBS), f"{mode.value} uses an unknown knob"
            # A fault always pushes a knob above the healthy baseline of 0.
            assert all(value > 0 for value in knobs.values())


def test_fingerprints_are_unique_so_ground_truth_is_unambiguous():
    # Each mode must own a distinct (service, knob) pair; otherwise the Day 19 eval can't tell
    # from the chaos_knob_value gauges which mode was injected.
    fingerprints = [
        (service, knob)
        for plan in inject.PLANS.values()
        for service, knobs in plan.knobs.items()
        for knob in knobs
    ]
    assert len(fingerprints) == len(set(fingerprints))


def test_inventory_api_is_never_touched():
    # It is the healthy control the incident agent localises against.
    for plan in inject.PLANS.values():
        assert "inventory-api" not in plan.knobs
