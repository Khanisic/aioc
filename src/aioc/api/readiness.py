"""Is this deployment able to answer a request? (Day 24)

`/healthz` says the process is up; `/readyz` says what it is connected to, one check a
dependency, each with a plain-words detail. Only the database is required - the smoke
request and the audit log both need it - and its check is a query, not a connection:
a hosted database that accepts logins but never had the schema applied is the failure
this is here to name.

The rest are reported and never fail readiness, because a deployment may honestly run
without them (no Langfuse means no traces; no Prometheus means the Deployment agent's
health tool answers ``PROMETHEUS_UNAVAILABLE``). No check calls the Anthropic API: a key
is reported as configured or not, never tested by spending.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
import psycopg

from aioc.llm import LLMSettings
from aioc.observability.tracing import TracingSettings


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    required: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "required": self.required, "detail": self.detail}


def check_database(dsn: str, *, timeout_seconds: int = 3) -> Check:
    try:
        with psycopg.connect(dsn, connect_timeout=timeout_seconds) as conn:
            incidents = conn.execute("SELECT to_regclass('incidents')").fetchone()
            audit = conn.execute("SELECT to_regclass('hitl_audit_log')").fetchone()
            if incidents is None or incidents[0] is None:
                return Check(
                    "database",
                    False,
                    True,
                    "connected, but there is no incidents table: apply the schema "
                    "(scripts/apply_schema.py)",
                )
            row = conn.execute("SELECT count(*) FROM incidents").fetchone()
            count = 0 if row is None else int(row[0])
    except psycopg.Error as exc:
        return Check("database", False, True, f"{type(exc).__name__}: {' '.join(str(exc).split())}")
    audit_note = "" if audit is not None and audit[0] is not None else ", no audit table yet"
    return Check("database", True, True, f"{count} incidents in the corpus{audit_note}")


def check_prometheus(url: str, *, timeout_seconds: float = 2.0) -> Check:
    url = url.rstrip("/")
    try:
        reply = httpx.get(f"{url}/-/ready", timeout=timeout_seconds)
    except httpx.HTTPError as exc:
        return Check("prometheus", False, False, f"{url} unreachable ({type(exc).__name__})")
    ok = reply.status_code == 200
    return Check("prometheus", ok, False, f"{url} answered {reply.status_code}")


def check_configured(name: str, configured: bool, missing: str) -> Check:
    return Check(name, configured, False, "configured" if configured else missing)


def default_checks(dsn: str, prometheus_url: str) -> list[Callable[[], Check]]:
    llm, tracing = LLMSettings(), TracingSettings()
    return [
        lambda: check_database(dsn),
        lambda: check_configured(
            "anthropic",
            llm.anthropic_api_key is not None,
            "ANTHROPIC_API_KEY is not set: live requests will fail",
        ),
        lambda: check_configured(
            "langfuse", tracing.configured, "LANGFUSE_* keys are not set: no traces"
        ),
        lambda: check_prometheus(prometheus_url),
    ]


def readiness(checks: list[Callable[[], Check]]) -> tuple[bool, dict[str, Any]]:
    results = [check() for check in checks]
    ready = all(result.ok for result in results if result.required)
    return ready, {
        "ready": ready,
        "checks": {result.name: result.to_dict() for result in results},
    }
