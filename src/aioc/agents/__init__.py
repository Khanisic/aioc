"""Reasoning Layer subagents (Phase 1).

Days 3-4: the Incident agent - prose (``investigate``) and schema-validated output
(``diagnose``). Day 8: the Docs agent - retrieval-grounded, schema-validated ``answer``.
Day 11: the GitHub agent - tool-driven over the `aioc-github` MCP server, schema-validated
``analyze``. The Deployment agent lands on Day 12.
"""

from __future__ import annotations

from .docs import (
    DOCS_STRUCTURED_SYSTEM_PROMPT,
    DocsAgent,
    DocsAgentError,
    DocsReport,
)
from .docs import EMIT_TOOL_NAME as DOCS_EMIT_TOOL_NAME
from .github import EMIT_TOOL_NAME as GITHUB_EMIT_TOOL_NAME
from .github import (
    GITHUB_SYSTEM_PROMPT,
    GitHubAgent,
    GitHubAgentError,
    GitHubReport,
)
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
    "GITHUB_EMIT_TOOL_NAME",
    "GITHUB_SYSTEM_PROMPT",
    "INCIDENT_STRUCTURED_SYSTEM_PROMPT",
    "INCIDENT_SYSTEM_PROMPT",
    "DocsAgent",
    "DocsAgentError",
    "DocsReport",
    "GitHubAgent",
    "GitHubAgentError",
    "GitHubReport",
    "IncidentAgent",
    "IncidentAgentError",
    "IncidentProse",
    "IncidentReport",
]
