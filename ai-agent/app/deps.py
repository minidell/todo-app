"""FastAPI dependencies.

The httpx client (and therefore its connection pool) is created once in the
application lifespan and shared by every request; tests override
``get_ollama_client`` with a client built on an ``httpx.MockTransport``.
"""

from __future__ import annotations

from fastapi import Request

from app.config import Settings
from app.ollama import OllamaClient


def get_ollama_client(request: Request) -> OllamaClient:
    """The process-wide Ollama client stored on ``app.state``."""
    return request.app.state.ollama


def get_app_settings(request: Request) -> Settings:
    """The settings this app was **built** with.

    Deliberately not ``config.get_settings``: that is an ``lru_cache``d read of
    the process environment, so a ``create_app(settings=...)`` with an explicit
    token would enforce a *different* token than the one it was configured with.
    Whatever the app was constructed with is what gets enforced.
    """
    return request.app.state.settings
