"""What the deployed service may do, read from the environment (Day 24).

Every setting that can spend money defaults to the answer that spends none. A URL on the
public internet that runs `respond()` costs about a dollar a request, so the service is
closed until it is opened twice over: a request needs the bearer token
(`AIOC_API_TOKEN`), and a ``live`` request also needs `AIOC_API_LIVE=true`. Without the
token nothing but the health checks answers; without the switch only the zero-cost
``smoke`` request runs.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # The bearer token every request but the health checks must carry. Unset means the
    # service refuses every request: closed is the default, never open.
    api_token: SecretStr | None = Field(default=None, validation_alias="AIOC_API_TOKEN")

    # A live request runs the real coordinator and agents. Off unless switched on.
    live_enabled: bool = Field(default=False, validation_alias="AIOC_API_LIVE")

    # Requests run one at a time by default: each live one is several minutes and about a
    # dollar, and two at once share one Anthropic rate limit for no benefit to a demo.
    max_concurrent: int = Field(default=1, ge=1, le=8, validation_alias="AIOC_API_MAX_CONCURRENT")

    # Requests waiting or running before a new one is refused (429). The queue is the
    # spend that can be committed before anyone looks.
    max_pending: int = Field(default=4, ge=1, le=64, validation_alias="AIOC_API_MAX_PENDING")

    # The most refinement rounds a request may ask for. Each round re-asks agents; the
    # Day 25 run is planned at zero.
    max_refinement_rounds: int = Field(
        default=0, ge=0, le=2, validation_alias="AIOC_API_MAX_ROUNDS"
    )

    # Finished requests kept in memory for GET. The service is a demo surface, not a
    # store: the durable records are the audit log and the traces.
    keep_finished: int = Field(default=100, ge=1, validation_alias="AIOC_API_KEEP_FINISHED")

    @field_validator("api_token", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `AIOC_API_TOKEN=` in a platform's settings, or `${AIOC_API_TOKEN:-}` in compose,
        # is a token nobody chose. Read it as unset - closed - never as an empty password.
        if isinstance(value, str) and not value.strip():
            return None
        return value
