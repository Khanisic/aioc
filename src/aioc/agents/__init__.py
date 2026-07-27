"""Reasoning Layer subagents (Phase 1).

Days 3-4: the Incident agent - prose (``investigate``) and schema-validated output
(``diagnose``). Docs, GitHub, and Deployment agents land on Days 8, 11, and 12 respectively.
"""

from __future__ import annotations

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
    "EMIT_TOOL_NAME",
    "INCIDENT_STRUCTURED_SYSTEM_PROMPT",
    "INCIDENT_SYSTEM_PROMPT",
    "IncidentAgent",
    "IncidentAgentError",
    "IncidentProse",
    "IncidentReport",
]
