"""AIOC - Enterprise AI Operations Center.

A multi-agent AIOps system built as CCA-F Foundations portfolio evidence.

The `aioc.contracts` package is the Python (Pydantic v2) implementation of the frozen
integration contract in `docs/CONTRACTS.md` (schema version 1.0.0). Anything that crosses
the MCP tool boundary is additionally governed by JSON Schema per that document; these
models are normative for the Reasoning Layer.
"""

__version__ = "0.1.0"

# Mirrors docs/CONTRACTS.md. Every payload carries this and consumers must fail loudly on a
# major mismatch rather than best-effort parsing (CONTRACTS.md sec 0).
SCHEMA_VERSION = "1.0.0"
