"""Every enum in the frozen contract (docs/CONTRACTS.md).

Each carries an ``other`` member by design: closed enums cause fabrication, so the model
picks ``other`` (paired with a detail string) rather than the nearest wrong member.
Members are `snake_case` string values, matching the wire format.
"""

from enum import StrEnum


class AgentName(StrEnum):
    INCIDENT = "incident"
    DOCS = "docs"
    GITHUB = "github"
    DEPLOYMENT = "deployment"


class ResponseStatus(StrEnum):
    """Shared by `AgentResponse.status` and `CoordinatorResponse.status`."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ERROR = "error"
    OTHER = "other"


class SourceType(StrEnum):
    METRIC = "metric"
    LOG = "log"
    EVENT = "event"
    DOCUMENT = "document"
    COMMIT = "commit"
    PULL_REQUEST = "pull_request"
    DEPLOYMENT = "deployment"
    CONFIG = "config"
    OTHER = "other"


class GapKind(StrEnum):
    MISSING_DATA = "missing_data"
    OUT_OF_SCOPE = "out_of_scope"
    TOOL_ERROR = "tool_error"
    AMBIGUOUS_QUERY = "ambiguous_query"
    INSUFFICIENT_PERMISSION = "insufficient_permission"
    OTHER = "other"


class ErrorClass(StrEnum):
    """The four-class tool error taxonomy. `transient` is the only retryable class."""

    TRANSIENT = "transient"
    VALIDATION = "validation"
    BUSINESS = "business"
    PERMISSION = "permission"


class Severity(StrEnum):
    SEV1 = "sev1"
    SEV2 = "sev2"
    SEV3 = "sev3"
    SEV4 = "sev4"
    OTHER = "other"


class FailureMode(StrEnum):
    """Members map 1:1 to the Day 4 chaos scripts. Do not add a member without a chaos mode."""

    RESOURCE_EXHAUSTION = "resource_exhaustion"
    BAD_CONFIG_DEPLOY = "bad_config_deploy"
    DOWNSTREAM_LATENCY = "downstream_latency"
    CODE_REGRESSION = "code_regression"
    OTHER = "other"


class TimelineEventKind(StrEnum):
    DEPLOY = "deploy"
    ALERT = "alert"
    CONFIG_CHANGE = "config_change"
    RESTART = "restart"
    SCALE = "scale"
    METRIC_THRESHOLD = "metric_threshold"
    LOG_PATTERN = "log_pattern"
    OTHER = "other"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    OTHER = "other"


class PullRequestState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"
    DRAFT = "draft"
    OTHER = "other"


class ChangeType(StrEnum):
    CODE = "code"
    DEPENDENCY = "dependency"
    CONFIG = "config"
    SCHEMA = "schema"
    INFRASTRUCTURE = "infrastructure"
    OTHER = "other"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    OTHER = "other"


class RolloutStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    IN_PROGRESS = "in_progress"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"
    OTHER = "other"


class RollbackRecommendation(StrEnum):
    ROLLBACK_NOW = "rollback_now"
    HOLD_AND_MONITOR = "hold_and_monitor"
    NO_ACTION = "no_action"
    INSUFFICIENT_DATA = "insufficient_data"
    OTHER = "other"


class Intent(StrEnum):
    INCIDENT_DIAGNOSIS = "incident_diagnosis"
    DOCUMENTATION_LOOKUP = "documentation_lookup"
    CODE_CHANGE_REVIEW = "code_change_review"
    DEPLOYMENT_CHECK = "deployment_check"
    MIXED = "mixed"
    OTHER = "other"


class InvocationMode(StrEnum):
    PARALLEL = "parallel"
    SEQUENTIAL = "sequential"
    OTHER = "other"
