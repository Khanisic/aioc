"""Chaos injector (Day 4, Engineer B).

Injects one of four named failure modes into the running demo app, or resets it to a healthy
baseline. The Makefile ``chaos-<mode>`` targets are thin wrappers over this script:

    uv run python demo-app/chaos/inject.py --mode downstream_latency
    uv run python demo-app/chaos/inject.py --reset
    uv run python demo-app/chaos/inject.py --status

Each failure mode is a *composition of the three ``/_chaos`` knobs* the Day 3 demo app exposes
(``extra_latency_ms``, ``error_rate``, ``leak_mb_per_request``), applied to specific services.
That was the Day 3 design: the app ships the knobs healthy-by-default, and Day 4 names the
compositions. Nothing here restarts a container or rebuilds the stack, so every mode clears with
a single ``--reset`` (the platform rule: an injected fault must survive - and un-inject within -
a live demo).

The ``--mode`` strings are the exact ``FailureMode`` members from CONTRACTS.md sec 4.1, imported
below so the 1:1 mapping is structural: this file cannot drift from the enum without failing at
import. That mapping is what lets the Day 19 eval score the agent's ``failure_mode`` against
injected ground truth - which is also readable straight from Prometheus, since the app publishes
every knob as ``chaos_knob_value{service,knob}`` the moment it changes.

Each mode targets a distinct ``(service, knob)`` fingerprint, and inventory-api is deliberately
left healthy as a control, so the injected truth is unambiguous and the agent has a clean signal
to localise against.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass

import httpx

from aioc.contracts import FailureMode

# Must match the demo app's ``_HEALTHY`` knob names (demo-app/services/app.py). The app rejects
# unknown knobs with a 400, so a drift here fails loudly at injection rather than silently.
KNOBS = ("extra_latency_ms", "error_rate", "leak_mb_per_request")

# Host-mapped ports from docker-compose.yml; override via the same env vars compose reads.
_HOST = os.environ.get("CHAOS_HOST", "127.0.0.1")
_SERVICES: dict[str, int] = {
    "checkout-api": int(os.environ.get("CHECKOUT_PORT", "8001")),
    "payments-api": int(os.environ.get("PAYMENTS_PORT", "8002")),
    "inventory-api": int(os.environ.get("INVENTORY_PORT", "8003")),
}
FRONT = "checkout-api"  # the only service that self-generates traffic and fans out
_TIMEOUT = 10.0


def _url(service: str) -> str:
    return f"http://{_HOST}:{_SERVICES[service]}"


@dataclass(frozen=True)
class ChaosPlan:
    """One failure mode: which knobs to set on which services, plus how to see it land."""

    mode: FailureMode
    headline: str
    knobs: dict[str, dict[str, float]]  # service -> {knob: value}
    observe: str


PLANS: dict[FailureMode, ChaosPlan] = {
    FailureMode.RESOURCE_EXHAUSTION: ChaosPlan(
        mode=FailureMode.RESOURCE_EXHAUSTION,
        headline="checkout-api leaks memory each request; RSS climbs toward exhaustion.",
        knobs={FRONT: {"leak_mb_per_request": 10.0}},
        observe="checkout-api process_resident_memory_bytes climbs steadily, no plateau.",
    ),
    FailureMode.DOWNSTREAM_LATENCY: ChaosPlan(
        mode=FailureMode.DOWNSTREAM_LATENCY,
        headline="payments-api runs slow; checkout-api waits on it and its p99 spikes.",
        knobs={"payments-api": {"extra_latency_ms": 800.0}},
        observe="checkout-api p99 up; the slow origin is payments-api, not checkout.",
    ),
    FailureMode.CODE_REGRESSION: ChaosPlan(
        mode=FailureMode.CODE_REGRESSION,
        headline="checkout-api throws 500s directly, as if a bad commit shipped.",
        knobs={FRONT: {"error_rate": 0.35}},
        observe="checkout-api status=500 spike; the 500 body carries the git_sha.",
    ),
    FailureMode.BAD_CONFIG_DEPLOY: ChaosPlan(
        mode=FailureMode.BAD_CONFIG_DEPLOY,
        headline="payments-api rejects requests (bad config); checkout-api returns 502s.",
        knobs={"payments-api": {"error_rate": 0.5}},
        observe="payments-api 500s cascade into checkout-api 502s (a downstream fault).",
    ),
}

# Structural 1:1 with the contract enum (minus ``other``, which has no chaos mode by design).
_missing = (set(FailureMode) - {FailureMode.OTHER}) - set(PLANS)
if _missing:
    raise RuntimeError(
        f"chaos plans out of sync with FailureMode: missing {sorted(m.value for m in _missing)}"
    )


def _fmt(state: dict[str, float]) -> str:
    active = {knob: value for knob, value in state.items() if value}
    return "healthy" if not active else " ".join(f"{k}={v:g}" for k, v in active.items())


def _post_chaos(client: httpx.Client, service: str, body: dict[str, object]) -> dict[str, float]:
    resp = client.post(f"{_url(service)}/_chaos", json=body)
    resp.raise_for_status()
    return dict(resp.json())


def reset_all(client: httpx.Client) -> None:
    for service in _SERVICES:
        state = _post_chaos(client, service, {"reset": True})
        print(f"  reset {service:<13} -> {_fmt(state)}")


def apply_plan(client: httpx.Client, plan: ChaosPlan) -> None:
    # Reset every service first so switching modes never leaves a stale knob behind.
    for service in _SERVICES:
        _post_chaos(client, service, {"reset": True})
    for service, knobs in plan.knobs.items():
        state = _post_chaos(client, service, dict(knobs))
        for knob, want in knobs.items():
            got = state.get(knob)
            if got != want:
                raise RuntimeError(
                    f"injection did not take on {service}: {knob}={got!r}, wanted {want!r}"
                )
        print(f"  set   {service:<13} -> {_fmt(state)}")


def show_status(client: httpx.Client) -> None:
    for service in _SERVICES:
        resp = client.get(f"{_url(service)}/_chaos")
        resp.raise_for_status()
        print(f"  {service:<13} {_fmt(dict(resp.json()))}")


def probe_front(client: httpx.Client, samples: int = 8) -> None:
    """Hit checkout-api a few times so the user-visible impact of the injection is immediate.

    Best-effort: a memory leak won't show here (it builds over time), but the error and latency
    modes surface right away in the status mix and mean latency.
    """
    statuses: dict[str, int] = {}
    total_seconds = 0.0
    for _ in range(samples):
        start = time.monotonic()
        try:
            code: str = str(client.get(f"{_url(FRONT)}/process").status_code)
        except httpx.HTTPError as exc:
            code = type(exc).__name__
        total_seconds += time.monotonic() - start
        statuses[code] = statuses.get(code, 0) + 1
    mean_ms = total_seconds / samples * 1000
    print(f"  {FRONT}/process x{samples}: statuses={statuses} mean={mean_ms:.0f}ms")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inject a named failure mode into the demo app.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--mode", choices=[m.value for m in PLANS], help="failure mode to inject")
    action.add_argument("--reset", action="store_true", help="return all services to healthy")
    action.add_argument("--status", action="store_true", help="print current knob state")
    parser.add_argument(
        "--no-probe", action="store_true", help="skip the post-injection probe of checkout-api"
    )
    args = parser.parse_args(argv)

    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            if args.status:
                print("Chaos knob state:")
                show_status(client)
                return 0
            if args.reset:
                print("Resetting all services to a healthy baseline...")
                reset_all(client)
                return 0
            plan = PLANS[FailureMode(args.mode)]
            print(f"Injecting {plan.mode.value}: {plan.headline}")
            apply_plan(client, plan)
            print(f"  observe: {plan.observe}")
            if not args.no_probe:
                print("Probing checkout-api for user-visible impact...")
                probe_front(client)
    except httpx.ConnectError:
        print(
            "ERROR: cannot reach the demo app - is the stack up?  ->  make up",
            file=sys.stderr,
        )
        return 1
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
