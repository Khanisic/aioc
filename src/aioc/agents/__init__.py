"""Reasoning Layer subagents (Phase 1).

Day 3: the Incident agent skeleton - prose out, no tools. Docs, GitHub, and Deployment
agents land on Days 8, 11, and 12 respectively.
"""

from __future__ import annotations

from .incident import INCIDENT_SYSTEM_PROMPT, IncidentAgent, IncidentProse

__all__ = [
    "INCIDENT_SYSTEM_PROMPT",
    "IncidentAgent",
    "IncidentProse",
]
