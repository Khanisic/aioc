"""Where the incident corpus lives - shared by the incident tool servers (Days 6-7).

Extracted from `timeline_server` when `correlate_server` arrived: both stdio servers read
the same seeded corpus, and two private copies of the DSN logic would drift on exactly the
kind of environment detail (the port override) that already cost a debugging session once.

Reads `DATABASE_URL` from the process environment first and the repo's `.env` second, the
same precedence `aioc.llm.LLMSettings` uses - so a developer who changed
`POSTGRES_PASSWORD` in `.env` gets a working tool without also exporting it. The `.env`
path is resolved from this file rather than the working directory, because an MCP client
launches these servers from wherever it happens to be.

Using pydantic-settings here is not a contract coupling: the rule is that a tool server
must not import `aioc.contracts`, not that it must avoid shared libraries.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class StoreSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3].parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Explicit URL wins when set - that is what a deployed environment provides.
    database_url: str | None = Field(default=None, validation_alias="DATABASE_URL")

    # Otherwise the DSN is composed from the same POSTGRES_* variables docker-compose reads.
    # Without this, a developer who set only POSTGRES_PASSWORD in `.env` gets a container
    # built with their password and a tool connecting with the documented default, which
    # fails as an authentication error that looks like the stack being down.
    postgres_user: str = Field(default="aioc", validation_alias="POSTGRES_USER")
    postgres_password: SecretStr = Field(
        default=SecretStr("aioc_dev_only"), validation_alias="POSTGRES_PASSWORD"
    )
    postgres_db: str = Field(default="aioc", validation_alias="POSTGRES_DB")
    postgres_port: int = Field(default=5432, validation_alias="POSTGRES_PORT")
    postgres_host: str = Field(default="localhost", validation_alias="POSTGRES_HOST")

    def dsn(self) -> str:
        if self.database_url:
            return self.database_url
        password = quote_plus(self.postgres_password.get_secret_value())
        return (
            f"postgresql://{quote_plus(self.postgres_user)}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


def dsn() -> str:
    return StoreSettings().dsn()
