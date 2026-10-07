# The AIOC service image (Day 24): the orchestrator over HTTP, plus the schema tool.
#
#   docker build -t aioc-api .
#   docker run --env-file <env> -p 8080:8080 aioc-api                    # the service
#   docker run --env-file <env> aioc-api python -m aioc.db migrate       # the deploy step
#
# Host-agnostic on purpose (the host is not chosen yet): it listens on $PORT, which every
# platform that runs containers sets, reads all configuration from the environment, and
# never bakes a secret in (.dockerignore keeps .env out of the build context).
#
# The four agents' MCP tool servers are not separate services: each agent launches its
# server as a subprocess of this process (`aioc.llm.mcp`), so one image carries them all.

FROM python:3.12-slim-bookworm

# uv, pinned, copied from its own image rather than installed with pip.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8080

WORKDIR /app

# Dependencies first, from the lockfile exactly: a source change rebuilds one layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# The package, and the schema files `python -m aioc.db` and the audit log apply. The
# project installs in place (editable), so `src/aioc/...` keeps the repository layout the
# code resolves `docker/postgres/init/` against.
COPY src ./src
COPY docker/postgres/init ./docker/postgres/init
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --home-dir /app aioc
USER aioc

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.environ.get('PORT', '8080') + '/healthz', timeout=3)"]

CMD ["sh", "-c", "exec uvicorn --factory aioc.api.app:create_app --host 0.0.0.0 --port \"$PORT\" --proxy-headers --forwarded-allow-ips='*'"]
