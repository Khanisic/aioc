"""Ground-truth access policy shared by the tool servers (Day 7).

The chaos injector publishes every injected fault (`chaos_knob_value`, the `chaos-*`
namespace) and those signals are the Day 19 eval's ground truth. An agent that reads them is
not diagnosing, it is transcribing the answer key - and the failure is silent, because the
evals simply start passing. `aioc.observability.prometheus` already enforces this for the
Incident agent's context; this module is the same guard at the tool layer, where an agent
could otherwise route around it by asking a tool directly.

The class is `permission`, not `business`, and that is deliberate: the signals exist, the
caller lacks the scope. A `business` "no such service" would be a lie the caller could
disprove, and an empty success would tell an agent "nothing there" when the truth is "not
for you". The four-class taxonomy exists precisely so this distinction reaches the agent.

**No `aioc.contracts` import here or in any tool server** - the MCP boundary is JSON Schema
(contract sec 6). `CHAOS_SCOPE_REQUIRED` is an additive error code, flagged like
`TIMELINE_STORE_TIMEOUT` in the timeline server: a patch-level addition under contract
sec 0, pending its entry in `docs/design-notes/contract-changes.md` and a sec 9 row.
"""

from __future__ import annotations

from mcp import types

from aioc.tools.envelope import err

# Everything the injector owns starts with this. The demo services are checkout-api,
# payments-api, and inventory-api; nothing legitimate shares the prefix.
RESTRICTED_PREFIX = "chaos"

REQUIRED_SCOPE = "eval:ground_truth"


def restricted_names(names: object) -> list[str]:
    """The subset of ``names`` that belongs to the chaos ground-truth namespace."""
    if not isinstance(names, (list, tuple, set)):
        return []
    return sorted(
        {
            str(name)
            for name in names
            if isinstance(name, str) and name.lower().startswith(RESTRICTED_PREFIX)
        }
    )


def ground_truth_denied(field: str, names: list[str]) -> types.CallToolResult:
    """The structured `permission` error for a request that names chaos signals."""
    return err(
        "permission",
        "CHAOS_SCOPE_REQUIRED",
        f"`{field}` names chaos-injector signal(s) {names}, which are the eval harness's "
        "injected ground truth and are not readable by agents.",
        remediation=(
            "Diagnose from the service's own metrics and events instead - the demo services "
            "are checkout-api, payments-api, and inventory-api. Ground-truth access is "
            f"reserved for the eval harness ({REQUIRED_SCOPE})."
        ),
        details={"required_scope": REQUIRED_SCOPE, "field": field, "restricted": names},
    )
