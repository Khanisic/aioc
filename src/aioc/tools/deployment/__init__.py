"""Deployment tools (Day 12): `diff_release` and `check_rollout_health` as the
`aioc-deployment` stdio MCP server. `release.py` is the structural, keys-only release diff;
`health.py` is the Prometheus rollout read and the deterministic status rule; `server.py`
is the wire. No `aioc.contracts` import anywhere in this package (contract sec 6)."""
