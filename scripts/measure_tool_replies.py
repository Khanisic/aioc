"""How big the tool replies an agent re-reads are, measured on this repository (Day 21).

    uv run python scripts/measure_tool_replies.py            # the four-agent run's calls
    uv run python scripts/measure_tool_replies.py --json

No Claude calls. GitHub API reads only (the token in `.env`), the same ones the GitHub and
Deployment agents made in the four-agent run of 2026-09-30: PR #11 and the release range
it shipped, with and without patches, and the default commit list. Each reply is sized as
the characters the model is sent and as the envelope's own `meta.token_estimate`, the
number `ToolCallRef.tokens_returned` records.

The Day 21 before is in `docs/interview-prep/numbers.md`; re-running this after a change
to the servers' shaping is the after. The commit list moves as the repository grows, so
compare a before and an after taken the same day.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from aioc.tools.deployment import server as deployment
from aioc.tools.github import server as github

FROM = "884b0b645fb674995e3c27719998a89d6f491833"  # the last recorded release
TO = "c729c7220da5ce96d2206759b490faa80396ed42"  # PR #11's merge commit

CALLS: list[tuple[str, str, dict[str, Any]]] = [
    ("github", "get_pull_request", {"number": 11}),
    ("github", "get_pull_request", {"number": 11, "include_patch": True}),
    ("github", "list_commits", {}),
    ("github", "diff_refs", {"base": FROM, "head": TO, "include_patch": True}),
    (
        "deployment",
        "diff_release",
        {"service": "checkout-api", "from_version": FROM, "to_version": TO},
    ),
]


def measure() -> list[dict[str, Any]]:
    rows = []
    for server, tool, args in CALLS:
        result = (github if server == "github" else deployment).call(tool, args)
        text = result.content[0].text  # type: ignore[union-attr]
        payload = json.loads(text)
        row: dict[str, Any] = {"tool": tool, "args": args, "chars": len(text)}
        if payload["ok"]:
            meta = payload["meta"]
            row |= {"token_estimate": meta["token_estimate"], "truncated": meta["truncated"]}
        else:
            row["error"] = payload["error"]["code"]
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the rows as JSON")
    args = parser.parse_args()
    rows = measure()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        shown = ", ".join(f"{k}={v}" for k, v in row["args"].items() if k != "base")
        if "error" in row:
            print(f"{row['tool']:<18} {shown:<60} ERROR {row['error']}")
            continue
        cut = " truncated" if row["truncated"] else ""
        print(
            f"{row['tool']:<18} {shown[:60]:<60} {row['chars']:>7,} chars "
            f"~{row['token_estimate']:>6,} tokens{cut}"
        )
    return 1 if any("error" in r for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
