# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv, pinned by version tag + content digest (matches the host's `uv --version`).
COPY --from=ghcr.io/astral-sh/uv:0.11.28@sha256:0f36cb9361a3346885ca3677e3767016687b5a170c1a6b88465ec14aefec90aa /uv /uvx /bin/

WORKDIR /app

# Never let `uv run` re-sync at container start (it would try to fetch dev-group
# packages like pytest from PyPI, which fails offline/in production). The entrypoint
# invokes the venv's binaries directly instead of going through `uv run`.
ENV UV_NO_SYNC=1

# Install dependencies in their own layer so `uv sync` is only re-run when the
# manifest/lockfile change, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

# Application source: app/, migrations/, alembic.ini, docker-entrypoint.sh, etc.
# (see .dockerignore for exclusions — tests, caches, venv, env files never enter the image).
COPY . .

RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
