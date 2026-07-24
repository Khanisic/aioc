"""Pydantic v2 implementation of the frozen contract in docs/CONTRACTS.md (schema 1.0.0).

Import surface for the Reasoning Layer. The `CoordinatorResponse` is the top of the tree;
everything else hangs off it.
"""

from ._common import StrictModel, check_other_detail, is_other
from .coordinator import AgentInvocation, CoordinatorResponse, Cost, SkippedAgent
from .deployment import (
    ApprovalRequirement,
    DeploymentFindings,
    HealthSignals,
    ImageChange,
    ReleasesCompared,
)
from .docs import Claim, Coverage, DocsFindings, SourceRef
from .enums import (
    AgentName,
    ChangeType,
    Environment,
    ErrorClass,
    FailureMode,
    GapKind,
    Intent,
    InvocationMode,
    PullRequestState,
    ResponseStatus,
    RiskLevel,
    RollbackRecommendation,
    RolloutStatus,
    Severity,
    SourceType,
    TimelineEventKind,
)
from .envelope import (
    AgentResponse,
    AnyAgentResponse,
    DeploymentAgentResponse,
    DocsAgentResponse,
    GitHubAgentResponse,
    IncidentAgentResponse,
    walk_assessments,
)
from .github import CommitRef, GitHubFindings, PullRequestAnalysis, SuspectChange
from .incident import (
    Impact,
    IncidentFindings,
    IncidentWindow,
    RecommendedAction,
    TimelineEvent,
)
from .primitives import Assessment, Evidence, Gap, ToolCallRef
from .tools import ToolError, ToolFailure, ToolMeta, ToolSuccess

__all__ = [
    # base / helpers
    "StrictModel",
    "check_other_detail",
    "is_other",
    "walk_assessments",
    # primitives
    "Assessment",
    "Evidence",
    "Gap",
    "ToolCallRef",
    # enums
    "AgentName",
    "ChangeType",
    "Environment",
    "ErrorClass",
    "FailureMode",
    "GapKind",
    "Intent",
    "InvocationMode",
    "PullRequestState",
    "ResponseStatus",
    "RiskLevel",
    "RollbackRecommendation",
    "RolloutStatus",
    "Severity",
    "SourceType",
    "TimelineEventKind",
    # incident
    "IncidentFindings",
    "IncidentWindow",
    "TimelineEvent",
    "Impact",
    "RecommendedAction",
    # docs
    "DocsFindings",
    "Claim",
    "Coverage",
    "SourceRef",
    # github
    "GitHubFindings",
    "PullRequestAnalysis",
    "CommitRef",
    "SuspectChange",
    # deployment
    "DeploymentFindings",
    "ReleasesCompared",
    "ImageChange",
    "HealthSignals",
    "ApprovalRequirement",
    # envelope
    "AgentResponse",
    "AnyAgentResponse",
    "IncidentAgentResponse",
    "DocsAgentResponse",
    "GitHubAgentResponse",
    "DeploymentAgentResponse",
    # coordinator
    "CoordinatorResponse",
    "AgentInvocation",
    "SkippedAgent",
    "Cost",
    # tools
    "ToolError",
    "ToolFailure",
    "ToolMeta",
    "ToolSuccess",
]
