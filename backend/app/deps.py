"""FastAPI dependencies: settings, session, current user, repositories."""

import logging
from collections.abc import Sequence
from datetime import datetime
from ipaddress import ip_address, ip_network
from typing import Annotated, AsyncIterator
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_client import AiAgentClient
from app.config import DEFAULT_TRUSTED_PROXY_CIDRS, Settings
from app.db.models import User
from app.errors import RateLimited, Unauthorized
from app.events import EventBroker, EventPublisher
from app.ratelimit import RateLimiter
from app.repositories.lists import SqlAlchemyListRepository
from app.repositories.protocols import (
    ListRepository,
    TagRepository,
    TodoRepository,
    UserRepository,
)
from app.repositories.tags import SqlAlchemyTagRepository
from app.repositories.todos import SqlAlchemyTodoRepository
from app.repositories.users import SqlAlchemyUserRepository
from app.security import (
    InvalidToken,
    decode_access_token,
    decode_access_token_claims,
)

logger = logging.getLogger(__name__)

#: ``auto_error=False`` so a missing or non-Bearer header reaches our own
#: handler: FastAPI's built-in error would be a 403 with a different body, and
#: the contract says 401 ``unauthorized`` (master §5).
bearer_scheme = HTTPBearer(auto_error=False, description="Access token from POST /api/auth/login")


def get_app_settings(request: Request) -> Settings:
    """The settings the app was created with (``create_app`` stores them)."""
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request. It **does not commit** — the handler does.

    This used to commit in its teardown, and that was a real bug: the code
    after a dependency's ``yield`` runs *after* the response has been sent, so
    a client could act on a 201 whose transaction had not landed yet — register
    then immediately log in returned 401, and a todo created then immediately
    listed showed stale counts. It never reproduced under ``ASGITransport``,
    where the test regains control only once teardown has finished, which is
    exactly why the suite was green while real clients raced.

    Two independent measures fix it, and both are deliberate:

    1. every consumer takes :data:`SessionDep`, i.e. ``scope="function"``, so
       teardown runs *before* the response goes out, and
    2. durability is no longer a side effect of teardown at all — each mutating
       handler calls ``await session.commit()`` itself, before it returns.

    Rollback stays here: an exception must undo the request's writes no matter
    which handler raised, and there is no ordering hazard in undoing work.
    """
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


#: The one way to ask for the request's session.
#:
#: ``scope="function"`` ends the dependency before the response is sent (see
#: :func:`get_session`). Every consumer must use *this alias* rather than its
#: own ``Depends(get_session)``: FastAPI caches a dependency per request, and
#: two declarations that disagree about the scope risk resolving to two
#: different sessions — the repository would write to one and the handler would
#: commit the other. ``test_session_scope.py`` asserts they are the same object.
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]


def get_auth_rate_limiter(request: Request) -> RateLimiter:
    """The per-(address, email) login/registration limiter."""
    return request.app.state.auth_rate_limiter


def get_auth_ip_rate_limiter(request: Request) -> RateLimiter:
    """The per-address limiter that bounds total credential work per client."""
    return request.app.state.auth_ip_rate_limiter


def is_trusted_proxy(address: str, cidrs: Sequence[str]) -> bool:
    """Whether a socket peer is one of our own proxies.

    Anything unparseable — including the ``"unknown"`` placeholder for a
    missing peer — is untrusted: a value we cannot even read as an address is
    not a value we can decide to believe.
    """
    try:
        peer = ip_address(address)
    except ValueError:
        return False
    for cidr in cidrs:
        try:
            if peer in ip_network(cidr, strict=False):
                return True
        except ValueError:  # pragma: no cover - Settings validates the list
            continue
    return False


def client_ip_from(
    request: Request,
    *,
    trust_proxy_headers: bool,
    trusted_proxy_cidrs: Sequence[str] = DEFAULT_TRUSTED_PROXY_CIDRS,
) -> str:
    """Best-effort client address for rate-limit keys.

    With ``TRUST_PROXY_HEADERS`` off (the default) only the socket address is
    used: ``X-Forwarded-For`` is client-controlled, so believing it on a
    directly reachable app would let an attacker mint a fresh rate-limit key
    per request — and evict everyone else's along the way.

    With it on, the header is honoured **only when the connection itself comes
    from a trusted proxy network**. The flag says "a proxy of ours sets this
    header"; it cannot say "nothing else can reach me". A published port, a
    sidecar or a misrouted probe is enough for a client to connect directly and
    forge its address again, so the socket peer is checked against
    ``TRUSTED_PROXY_CIDRS`` before any part of the header is believed.

    From a trusted peer the address is taken from the **last** hop. Every
    earlier entry was supplied by whoever called our proxy and can say
    anything; the last one is what our own nginx appended, and it is the only
    part of the chain we have any reason to believe. A missing or empty header
    falls back to the socket address, so a misconfigured proxy degrades to
    "everyone shares one key" rather than to no limiting at all.
    """
    socket_host = request.client.host if request.client else "unknown"
    if not trust_proxy_headers:
        return socket_host
    if not is_trusted_proxy(socket_host, trusted_proxy_cidrs):
        return socket_host

    forwarded = request.headers.get("X-Forwarded-For", "")
    hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
    return hops[-1] if hops else socket_host


def get_client_ip(
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> str:
    return client_ip_from(
        request,
        trust_proxy_headers=settings.TRUST_PROXY_HEADERS,
        trusted_proxy_cidrs=settings.TRUSTED_PROXY_CIDRS,
    )


def get_todo_repository(
    session: SessionDep,
) -> TodoRepository:
    return SqlAlchemyTodoRepository(session)


def get_list_repository(
    session: SessionDep,
) -> ListRepository:
    return SqlAlchemyListRepository(session)


def get_tag_repository(
    session: SessionDep,
) -> TagRepository:
    return SqlAlchemyTagRepository(session)


def get_user_repository(
    session: SessionDep,
) -> UserRepository:
    return SqlAlchemyUserRepository(session)


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    users: Annotated[UserRepository, Depends(get_user_repository)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> User:
    """The authenticated owner of the request — the only place auth is decided.

    Every failure mode (absent header, wrong scheme, bad signature, expired,
    unknown ``sub``) collapses into the same 401 ``unauthorized``: the caller
    learns that it is not authenticated and nothing else. No code path in this
    application serves a request without passing through here.

    Defined below the repository factories on purpose: ``Depends(...)`` inside
    an ``Annotated`` is evaluated when the function is defined, so the callable
    must already exist.
    """
    if credentials is None or not credentials.credentials:
        raise Unauthorized()

    try:
        user_id = decode_access_token(credentials.credentials, settings)
    except InvalidToken as exc:
        # Debug level and no token material: an invalid token is normal traffic
        # (an expired session), and the token is a credential.
        logger.debug("Rejected bearer token: %s", exc)
        raise Unauthorized() from exc

    user = await users.get(user_id)
    if user is None:
        # A validly signed token for a deleted account must stop working.
        logger.info("Bearer token for unknown user %s", user_id)
        raise Unauthorized()
    return user


def get_client_id(
    x_client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
) -> str | None:
    """The sending tab's id — realtime echo suppression (master §7.2).

    Validated as a UUID and normalised, then copied into ``origin`` on every
    frame the request produces so the originating tab can ignore its own echo.
    A malformed value is treated as absent rather than rejected: the header is
    optional, a bad one costs the sender nothing but a duplicate re-render, and
    it *is* reflected to every other tab of that user — so an unvalidated
    string would be a free way to push arbitrary content into a frame field.
    """
    if x_client_id is None:
        return None
    try:
        return str(UUID(x_client_id))
    except (ValueError, AttributeError):
        return None


# --- realtime --------------------------------------------------------------


def get_broker(request: Request) -> EventBroker:
    """The app's single in-process broker (``create_app`` builds it)."""
    return request.app.state.broker


def get_token_expiry(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> datetime:
    """When the bearer token presented with *this* request stops being valid.

    Only the SSE stream asks. Every other endpoint is a single request that is
    over long before the token is, but a stream is held open for hours: without
    this, a connection authenticated at 09:00 with a 60-minute token would keep
    delivering that user's data at 17:00, and revoking access by letting a token
    expire would not actually revoke anything.

    Decoded a second time rather than smuggled out of ``get_current_user`` via
    ``request.state``: an HMAC verification costs microseconds, and a dependency
    that says what it returns is worth more than the saving.
    """
    if credentials is None or not credentials.credentials:
        raise Unauthorized()
    try:
        claims = decode_access_token_claims(credentials.credentials, settings)
    except InvalidToken as exc:
        logger.debug("Rejected bearer token on the event stream: %s", exc)
        raise Unauthorized() from exc
    return claims.expires_at


def get_event_publisher(
    broker: Annotated[EventBroker, Depends(get_broker)],
    user: Annotated[User, Depends(get_current_user)],
    client_id: Annotated[str | None, Depends(get_client_id)],
) -> EventPublisher:
    """A staging area for the events this request wants to publish.

    Bound to the authenticated user, so a handler physically cannot publish
    into somebody else's stream: the user id is taken from the token here
    rather than from anything the payload says.
    """
    return EventPublisher(broker, user.id, origin=client_id)


# --- AI --------------------------------------------------------------------


def get_ai_client(request: Request) -> AiAgentClient:
    """The shared ai-agent HTTP client (one pool per app, not per request)."""
    return request.app.state.ai_client


def get_ai_rate_limiter(request: Request) -> RateLimiter:
    """The per-user AI limiter: 20 requests / 5 minutes (master §6.6)."""
    return request.app.state.ai_rate_limiter


def enforce_ai_rate_limit(
    user: Annotated[User, Depends(get_current_user)],
    limiter: Annotated[RateLimiter, Depends(get_ai_rate_limiter)],
) -> None:
    """Bound how much model time one account can spend.

    Keyed on the **user id**, not the address: an AI call costs seconds of GPU
    or CPU on a single-model Ollama that serves everybody, so the limit has to
    follow the account that authenticated rather than the network path it came
    from. ``Retry-After`` is populated because the frontend shows a "please
    wait a moment" message and needs to know how long a moment is.
    """
    result = limiter.hit(str(user.id))
    if not result.allowed:
        logger.info("AI rate limit reached for user %s", user.id)
        raise RateLimited(headers={"Retry-After": str(result.retry_after)})
