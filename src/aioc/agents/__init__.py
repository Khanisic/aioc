"""Reasoning Layer subagents (Phase 1).

Days 3-4: the Incident agent - prose (``investigate``) and schema-validated output
(``diagnose``). Day 8: the Docs agent - retrieval-grounded, schema-validated ``answer``.
GitHub and Deployment agents land on Days 11 and 12 respectively.
"""

from __future__ import annotations

from .docs import (
    DOCS_STRUCTURED_SYSTEM_PROMPT,
    DocsAgent,
    DocsAgentError,
    DocsReport,
)
from .docs import EMIT_TOOL_NAME as DOCS_EMIT_TOOL_NAME
from .incident import (
    EMIT_TOOL_NAME,
    INCIDENT_STRUCTURED_SYSTEM_PROMPT,
    INCIDENT_SYSTEM_PROMPT,
    IncidentAgent,
    IncidentAgentError,
    IncidentProse,
    IncidentReport,
)

__all__ = [
    "DOCS_EMIT_TOOL_NAME",
    "DOCS_STRUCTURED_SYSTEM_PROMPT",
    "EMIT_TOOL_NAME",
    "INCIDENT_STRUCTURED_SYSTEM_PROMPT",
    "INCIDENT_SYSTEM_PROMPT",
    "DocsAgent",
    "DocsAgentError",
    "DocsReport",
    "IncidentAgent",
    "IncidentAgentError",
    "IncidentProse",
    "IncidentReport",
]
