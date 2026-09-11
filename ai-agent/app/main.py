"""FastAPI application for the ai-agent service (slice-4 spec B1/B4).

Two surfaces:

* ``GET /health`` — unauthenticated, **never fails**. It reports whether Ollama
  answers and whether the configured model has been pulled, so the backend's
  ``GET /api/ai/status`` can drive the UI (master §6.6).
* ``POST /ai/*`` — the four AI endpoints, all behind ``X-Internal-Token``.

There is no CORS configuration on purpose: browsers never talk to this service
(master D-AI2), only the backend does, over the compose network.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator

import httpx
from fastapi import Depends, FastAPI

from app.config import Settings, get_settings
from app.deps import get_ollama_client
from app.errors import install_error_handlers
from app.middleware import BodySizeLimitMiddleware
from app.ollama import OllamaClient, OllamaError
from app.routers.ai import router as ai_router
from app.schemas import HealthResponse

logger = logging.getLogger(__name__)

#: ``/health`` must stay fast even when Ollama is wedged (slice-4 spec B4).
HEALTH_TIMEOUT_SECONDS = 3.0


def build_http_client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    """The shared httpx client, with the timeout ladder from master §8.3."""
    return httpx.AsyncClient(
        base_url=settings.base_url,
        transport=transport,
        timeout=httpx.Timeout(
            connect=5.0,
            read=settings.OLLAMA_TIMEOUT_SECONDS,
            write=10.0,
            pool=5.0,
        ),
    )


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Application factory.

    ``settings`` and ``transport`` are injection seams for the test suite: the
    tests drive a fake Ollama through an ``httpx.MockTransport`` while still
    exercising the real lifespan, client construction and shutdown.
    """
    settings = settings or get_settings()
    logging.basicConfig(level=settings.LOG_LEVEL.upper())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = build_http_client(settings, transport)
        app.state.http_client = client
        app.state.ollama = OllamaClient(
            client,
            settings.OLLAMA_MODEL,
            budget_seconds=settings.OLLAMA_TIMEOUT_SECONDS,
        )
        logger.info(
            "ai-agent ready: model=%s ollama=%s",
            settings.OLLAMA_MODEL,
            settings.base_url,
        )
        try:
            yield
        finally:
            await client.aclose()

    docs = settings.docs_enabled
    app = FastAPI(
        title="Todo AI agent",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings
    app.add_middleware(BodySizeLimitMiddleware)
    install_error_handlers(app)
    app.include_router(ai_router)

    @app.get("/health", response_model=HealthResponse)
    async def health(
        client: Annotated[OllamaClient, Depends(get_ollama_client)],
    ) -> HealthResponse:
        """Report Ollama reachability; never raise (slice-4 spec B4)."""
        status = "unavailable"
        model_present = False
        try:
            models = await client.list_models(timeout=HEALTH_TIMEOUT_SECONDS)
        except OllamaError as exc:
            logger.warning("Ollama health check failed: %s", exc)
        except Exception:  # pragma: no cover - defensive: /health must not 500
            logger.exception("Unexpected error during the Ollama health check")
        else:
            status = "ok"
            model_present = _model_present(settings.OLLAMA_MODEL, models)

        return HealthResponse(
            status="ok",
            ollama=status,
            model=settings.OLLAMA_MODEL,
            model_present=model_present,
        )

    return app


def _model_present(configured: str, models: list[str]) -> bool:
    """Is the configured model among the pulled ones?

    An exact match wins. A configured name without a tag (``qwen2.5``) also
    matches any tag of that model, because that is how Ollama resolves it.
    """
    if configured in models:
        return True
    if ":" in configured:
        return False
    return any(name.split(":", 1)[0] == configured for name in models)


app = create_app()
