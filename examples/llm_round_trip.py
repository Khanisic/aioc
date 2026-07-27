"""Day 2 done-when: round-trip a real tool call through the Claude API harness.

Run it:

    uv run python examples/llm_round_trip.py

Needs ``ANTHROPIC_API_KEY`` (console key - a Claude Max subscription does NOT cover
programmatic API use; see the DAY 2 block in ``.env.example``). Without a key it prints how
to set one and exits cleanly, so the file is safe to run in any checkout.

It exercises all three harness entry points against the live API: a streamed completion, then
the ``tool_use`` loop with a mock ``get_service_health`` tool that Claude must call to answer.
"""

from __future__ import annotations

from aioc.llm import LLMClient, LLMSettings, ToolResult, ToolSpec

# A stand-in for the Prometheus-backed tools that land in Phase 2. Two services, one degraded.
_METRICS: dict[str, dict[str, object]] = {
    "checkout-api": {"status": "healthy", "error_rate_pct": 0.1, "p99_ms": 120},
    "payments-api": {"status": "degraded", "error_rate_pct": 7.4, "p99_ms": 2100},
}


def _get_service_health(args: dict[str, object]) -> ToolResult | str:
    service = str(args.get("service", ""))
    metrics = _METRICS.get(service)
    if metrics is None:
        return ToolResult(content=f"Unknown service '{service}'.", is_error=True)
    return (
        f"{service}: status={metrics['status']}, "
        f"error_rate={metrics['error_rate_pct']}%, p99={metrics['p99_ms']}ms"
    )


_HEALTH_TOOL = ToolSpec(
    name="get_service_health",
    description=(
        "Return current health metrics (status, error rate, p99 latency) for one named "
        "service. Input: {'service': <service name>}. Example services: checkout-api, "
        "payments-api. Call once per service you need to inspect."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Service name, e.g. checkout-api"}
        },
        "required": ["service"],
    },
    handler=_get_service_health,
)

_SYSTEM = (
    "You are an SRE assistant. Use the tools to check real service health before answering. "
    "Cite the error rate you observed. Be concise."
)


def main() -> int:
    settings = LLMSettings()
    if settings.anthropic_api_key is None:
        print(
            "ANTHROPIC_API_KEY is not set.\n"
            "  Set it in your shell:  export ANTHROPIC_API_KEY=sk-ant-...\n"
            "  or add it to .env (see the DAY 2 block in .env.example).\n"
            "A Claude Max subscription does not cover programmatic API use - use a console key."
        )
        return 0

    client = LLMClient(settings)
    print(f"Model: {settings.model}\n")

    # 1) Streaming - text deltas as they arrive.
    print("=== streamed completion ===")
    for chunk in client.stream_text(
        messages=[
            {"role": "user", "content": "In one sentence, what does an AIOps coordinator do?"}
        ],
        max_tokens=200,
    ):
        print(chunk, end="", flush=True)
    print("\n")

    # 2) The tool_use loop - Claude must call get_service_health to answer.
    print("=== tool_use loop ===")
    result = client.run_tool_loop(
        messages=[
            {
                "role": "user",
                "content": (
                    "Compare the health of checkout-api and payments-api. Which one is "
                    "degraded, and what is its error rate?"
                ),
            }
        ],
        tools=[_HEALTH_TOOL],
        system=_SYSTEM,
    )

    print(f"\nFinal answer ({result.stop_reason}, {result.rounds} rounds):")
    print(f"  {result.text}\n")
    print(f"Tool calls ({len(result.tool_calls)}):")
    for record in result.tool_calls:
        flag = "ok" if record.ok else "ERROR"
        summary = f"{record.name}({record.arguments}) -> {record.output}"
        print(f"  [{flag}] {summary} ({record.duration_ms}ms)")
    print(f"\nTokens: in={result.usage.input_tokens}, out={result.usage.output_tokens}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
