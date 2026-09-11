"""FastAPI application factory: lifespan, middleware, error handlers, routers."""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.ai_client import AiAgentClient
from app.config import (
    MIN_JWT_SECRET_LENGTH,
    REALTIME_STARTUP_NOTICE,
    Settings,
    get_settings,
)
from app.db.session import create_engine_from_url, create_sessionmaker
from app.errors import DatabaseUnavailable, register_error_handlers
from app.events import EventBroker
from app.middleware import BodySizeLimitMiddleware
from app.ratelimit import RateLimiter
from app.routers.ai import router as ai_router
from app.routers.auth import router as auth_router
from app.routers.events import router as events_router
from app.routers.lists import router as lists_router
from app.routers.tags import router as tags_router
from app.routers.todos import router as todos_router
from app.schemas.common import ErrorResponse, HealthResponse, ReadyResponse

logger = logging.getLogger(__name__)

#: Login/registration limiter (slice-2 spec B4): 10 attempts per 15 minutes,
#: keyed by client address + email — this protects one account from guessing.
AUTH_RATE_LIMIT = 10
AUTH_RATE_WINDOW_SECONDS = 15 * 60

#: AI limiter window (master §6.6): 20 requests per 5 minutes, per user id.
AI_RATE_WINDOW_SECONDS = 5 * 60

#: The package every logger in this application hangs off. Configuring the one
#: parent is what makes ``logging.getLogger(__name__)`` work in every module
#: without each of them knowing anything about handlers.
APP_LOGGER_NAME = "app"

#: Plain and greppable. Container logs are read with ``docker compose logs``
#: and a pipe into ``grep``, not by a log-shipping stack; JSON here would cost
#: readability for a consumer that does not exist.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: Settings = app.state.settings
    # Said out loud on every start (master §7.3, iteration-4 delta): the
    # single-replica constraint is invisible otherwise, and the way it breaks —
    # some tabs quietly stop updating — is not one an operator would trace back
    # to a `--scale backend=N`.
    logger.info(REALTIME_STARTUP_NOTICE, settings.REALTIME_BACKEND)
    app.state.engine = create_engine_from_url(settings.DATABASE_URL)
    app.state.sessionmaker = create_sessionmaker(app.state.engine)
    try:
        yield
    finally:
        # The ai-agent client is built in create_app (tests drive the app
        # without a lifespan), but its connection pool still has to be closed
        # on a real shutdown.
        await app.state.ai_client.aclose()
        await app.state.engine.dispose()


def configure_logging(settings: Settings) -> None:
    """Make this application's own log records actually come out somewhere.

    Nothing else does it. uvicorn configures the ``uvicorn.*`` loggers and
    leaves everything else alone, and Python's default configuration drops any
    record below WARNING that reaches an unconfigured logger — so every
    ``logger.info`` in ``app.*`` (the realtime single-replica notice, the
    tag-delete audit line, the ai-agent health result) vanished in the
    container. Found by QA in iteration 4.

    Two deliberate narrownesses:

    *Only the ``app`` logger's level is set.* Turning the level down on the
    root logger would also turn up SQLAlchemy, asyncpg and httpx, which at
    DEBUG print statements and headers — including an ``Authorization`` header.

    *A handler is attached only when nothing is listening yet.* uvicorn's
    handlers (or pytest's, or a production log-config file's) must not be
    duplicated: a second handler on the same stream prints every line twice.
    This makes the function idempotent, which matters because tests build
    dozens of applications in one process.
    """
    app_logger = logging.getLogger(APP_LOGGER_NAME)
    app_logger.setLevel(settings.LOG_LEVEL)

    if app_logger.handlers or logging.getLogger().handlers:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    app_logger.addHandler(handler)


def require_jwt_secret(settings: Settings) -> None:
    """Refuse to build an app that cannot verify a token.

    Failing here — loudly, at startup — is the point: an app that starts
    without a signing key would either mint unverifiable tokens or invent a
    default one, and a default signing key is a public signing key.
    """
    if not settings.JWT_SECRET:
        raise RuntimeError(
            "JWT_SECRET is not set. The backend signs access tokens with it and "
            "refuses to start without one. Set the JWT_SECRET environment "
            f"variable to at least {MIN_JWT_SECRET_LENGTH} characters — "
            "generate one with: openssl rand -hex 32"
        )


def require_ai_agent_token(settings: Settings) -> None:
    """AI on means the shared secret must be present (master §9.1).

    Without it every AI call would reach ai-agent unauthenticated and come back
    401, which the proxy reports as ``ai_unavailable`` — the user sees "AI is
    down" and an operator sees a working network. Failing at startup names the
    real problem, and the message names the one-variable way out for a
    deployment that simply does not want AI.
    """
    if settings.AI_ENABLED and not settings.AI_AGENT_TOKEN:
        raise RuntimeError(
            "AI_ENABLED is true but AI_AGENT_TOKEN is not set. The backend "
            "authenticates to the ai-agent service with that shared secret and "
            "will not start without it. Set AI_AGENT_TOKEN to the same value "
            "the ai-agent service uses (generate one with: openssl rand -hex "
            "32), or set AI_ENABLED=false to run without AI — every other "
            "feature works unchanged."
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # First, so that everything below — including a startup refusal — is
    # actually visible in the log it is written to.
    configure_logging(settings)
    require_jwt_secret(settings)
    require_ai_agent_token(settings)

    # Carry-over C5: interactive docs are a dev-only affordance.
    docs_enabled = settings.docs_enabled
    app = FastAPI(
        title="Todo API",
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.settings = settings
    app.state.auth_rate_limiter = RateLimiter(
        AUTH_RATE_LIMIT, AUTH_RATE_WINDOW_SECONDS
    )
    # Keyed on the address alone, so a caller cycling through fresh emails —
    # which never repeats a per-email key and would otherwise never be
    # throttled — still cannot make the server hash indefinitely.
    app.state.auth_ip_rate_limiter = RateLimiter(
        settings.AUTH_IP_RATE_LIMIT, AUTH_RATE_WINDOW_SECONDS
    )
    # Keyed on the user id: an AI call costs seconds of model time on a single
    # shared Ollama, so the budget follows the account, not the network path.
    app.state.ai_rate_limiter = RateLimiter(
        settings.AI_RATE_LIMIT, AI_RATE_WINDOW_SECONDS
    )
    # One broker and one ai-agent connection pool per application. Both are
    # built here rather than in the lifespan so that a test (which drives the
    # app without running one) still gets a fully wired app; the lifespan owns
    # closing the pool.
    app.state.broker = EventBroker(settings.MAX_STREAMS_PER_USER)
    app.state.ai_client = AiAgentClient(settings)

    # Order matters: add_middleware *prepends*, so the last one added is the
    # outermost. CORS must wrap the body-size limit, or its 413 — generated
    # before the request ever reaches the app — would carry no CORS headers and
    # the browser would surface a generic network error instead of the status.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.MAX_BODY_BYTES)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Client-Id"],
        expose_headers=["X-Total-Count"],
        allow_credentials=False,
    )

    register_error_handlers(app)
    app.include_router(auth_router)
    app.include_router(lists_router)
    app.include_router(tags_router)
    app.include_router(todos_router)
    app.include_router(events_router)
    app.include_router(ai_router)

    @app.get("/api/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        """Liveness probe — deliberately touches no database."""
        return HealthResponse(status="ok")

    @app.get(
        "/api/health/ready",
        response_model=ReadyResponse,
        tags=["health"],
        responses={503: {"model": ErrorResponse, "description": "Database unavailable"}},
    )
    async def ready(request: Request) -> ReadyResponse:
        """Readiness probe — reports whether the database answers."""
        try:
            async with request.app.state.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - any driver failure is "down"
            logger.warning("Readiness check failed: %s", exc)
            raise DatabaseUnavailable() from exc
        return ReadyResponse(status="ok", database="ok")

    return app


app = create_app()
