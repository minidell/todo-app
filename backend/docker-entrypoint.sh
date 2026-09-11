#!/usr/bin/env bash
set -euo pipefail

# Call the venv's binaries directly (not `uv run`) — `uv run` re-syncs the project
# on every invocation, which would try to fetch dev-group packages (pytest, etc.)
# from PyPI at container start and fails on an offline/production host. The image
# already has the exact locked, non-dev environment baked in at build time.
/app/.venv/bin/alembic upgrade head

# --limit-concurrency caps in-flight requests (including open SSE streams) per
# worker process so one client (or a burst of them) cannot exhaust the event loop;
# nginx's `limit_conn perip 32` on /api/ caps a single address, this caps the whole
# process (slice-4 security review).
exec /app/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --limit-concurrency 200
