"""Settings for the Claude API harness (Day 2).

Read from the environment / ``.env`` via ``pydantic-settings``. The variable names match
the ``DAY 2 - Claude API`` block in ``.env.example``:

  - ``ANTHROPIC_API_KEY`` - the console key. The SDK reads this on its own too; modelling
    it here lets the demo and tests fail with a clear message instead of an SDK stack trace.
  - ``AIOC_MODEL`` - the harness default model. The Day 23 routing experiment (Haiku/Sonnet
    for subagents, Opus for the coordinator) is a Reasoning-Layer concern layered on top of
    this; the harness itself just needs one default.

Nothing here is frozen - model selection and retrieval parameters churn freely (CLAUDE.md).
"""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """Harness configuration. Every field has an environment override and a working default."""

    # protected_namespaces=() so a plain ``model`` field doesn't collide with pydantic's
    # reserved ``model_`` prefix. populate_by_name=True so a field can be set by its Python
    # name (``LLMSettings(effort="low")``) as well as by its env alias.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
        populate_by_name=True,
    )

    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    # Sonnet rather than Opus: measured by scripts/check_structured_output.py, it is the cheapest
    # model that returns a contract-valid IncidentAgentResponse on the first try (Haiku still
    # breaks the timeline ordering invariant). Opus stays the coordinator's model - that split is
    # the Day 23 routing experiment, and this default is the subagent half of it.
    model: str = Field(default="claude-sonnet-5", validation_alias="AIOC_MODEL")

    # 8192, not 4096: a full structured incident report does not fit in 4096 output tokens. Opus
    # hit exactly 4096 and was truncated mid-JSON, which surfaces as a bogus "field required"
    # validation error rather than an obvious limit problem (IncidentAgent.diagnose now names it).
    max_tokens: int = Field(default=8192, gt=0, validation_alias="AIOC_MAX_TOKENS")

    # Effort is Opus/Sonnet-only (Haiku 4.5 rejects it with a 400), so it is opt-in: left
    # None, the harness omits output_config entirely and every model - including Haiku - is
    # safe. Set it to low/medium/high/xhigh/max to control depth on a model that supports it.
    effort: str | None = Field(default=None, validation_alias="AIOC_LLM_EFFORT")

    timeout_seconds: float = Field(default=600.0, gt=0, validation_alias="AIOC_LLM_TIMEOUT")

    # Backstop for the tool_use loop: how many assistant<->tool round trips before we give
    # up. A runaway loop is a bug, not a slow answer, so this stays low.
    max_tool_iterations: int = Field(default=8, gt=0, validation_alias="AIOC_MAX_TOOL_ITERATIONS")
