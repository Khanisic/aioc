"""GitHub MCP tools (Day 11, Platform Layer).

`api.py` is the thin read-only REST client with the contract's four-class error mapping;
`server.py` is the stdio MCP server that exposes it as three tools. Neither imports
`aioc.contracts` - the MCP boundary is JSON Schema (contract sec 6).
"""
