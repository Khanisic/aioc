"""Settings for the Claude API harness (Day 2).

Read from the environment / ``.env`` via ``pydantic-settings``. The variable names match
the ``DAY 2 - Claude API`` block in ``.env.example``:

  - ``ANTHROPIC_API_KEY`` - the console key. The SDK reads this on its own too; modelling
    it here lets the demo and tests fail with a clear message instead of an SDK stack trace.
  - ``AIOC_MODEL`` - the harness default model.
  - ``AIOC_COORDINATOR_MODEL`` / ``AIOC_SUBAGENT_MODEL`` - per-role overrides (Day 23), read
    through `LLMSettings.for_role`: the coordinator's calls (planning and the model-written
    synthesis) and the four subagents' calls can run different models, each falling back to
    ``AIOC_MODEL``. Model routing is therefore a configuration change, not a code change.

Nothing here is frozen - model selection and retrieval parameters churn freely (CLAUDE.md).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["coordinator", "subagent"]


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

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `.env.example` ships `ANTHROPIC_API_KEY=` blank and compose's env_file passes the
        # blank on: an empty string is a key nobody set (the Day 25 fresh-clone test).
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # Sonnet rather than Opus: measured by scripts/check_structured_output.py, it is the cheapest
    # model that returns a contract-valid IncidentAgentResponse on the first try (Haiku still
    # breaks the timeline ordering invariant). Both roles run it unless overridden below; the
    # Day 23 projection (`docs/design-notes/model-routing.md`) is what a split would cost.
    model: str = Field(default="claude-sonnet-5", validation_alias="AIOC_MODEL")

    # Per-role overrides (Day 23). Unset means the role runs `model`. They are fields, not
    # os.environ reads, so a value in `.env` takes effect - before Day 23 the coordinator's
    # override was read from the process environment only, and `.env.example` told people
    # to put it in `.env`, where it did nothing.
    coordinator_model: str | None = Field(default=None, validation_alias="AIOC_COORDINATOR_MODEL")
    subagent_model: str | None = Field(default=None, validation_alias="AIOC_SUBAGENT_MODEL")

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

    # The validation-retry loop's cap (Day 17, `aioc.agents._retry`): how many times an
    # agent re-requests a report the contract or its own grounding rules refused, with the
    # error attached. 0 disables the loop. Two, like the refinement rounds - an identical
    # rejection stops it earlier, and every retry re-sends the whole conversation.
    max_validation_retries: int = Field(
        default=2, ge=0, validation_alias="AIOC_MAX_VALIDATION_RETRIES"
    )

    # Prompt caching (Day 19). On by default: every agent's system prompt and tool schema
    # are byte-identical on every call and sit at the front of the prefix, so a marker on
    # the system block caches both. A write costs 1.25x the input rate and a read 0.1x, so
    # a prefix read back once has already paid for itself; a prefix below the model's
    # minimum (1024 tokens on Sonnet 5) is silently not cached and costs nothing extra.
    prompt_caching: bool = Field(default=True, validation_alias="AIOC_PROMPT_CACHING")

    # 5m unless the calls that share a prefix start more than five minutes apart - the one
    # case here is a batch, which may sit in the queue longer than that. A 1h write costs 2x.
    prompt_cache_ttl: Literal["5m", "1h"] = Field(
        default="5m", validation_alias="AIOC_PROMPT_CACHE_TTL"
    )

    def for_role(self, role: Role) -> LLMSettings:
        """These settings with `model` set to the role's override, or unchanged without one.

        The coordinator role is the planner and the model-written synthesis; the subagent
        role is the four agents. Everything but the model is shared."""
        override = self.coordinator_model if role == "coordinator" else self.subagent_model
        return self if override is None else self.model_copy(update={"model": override})
