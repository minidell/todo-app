"""Dual-engine test harness (master decision D-T1).

Default lane: in-memory SQLite via ``aiosqlite`` — always runnable, no Docker.
Fidelity lane: set ``TEST_DATABASE_URL`` and the *identical* suite runs against
PostgreSQL::

    docker compose up -d db
    TEST_DATABASE_URL=postgresql+asyncpg://todo:todo@localhost:5433/todo_test uv run pytest

Tests marked ``@pytest.mark.postgres`` are skipped when that variable is unset.

The app is driven through ``httpx.AsyncClient`` + ``ASGITransport`` rather than
``TestClient`` on purpose: it runs the application in the *test's own* event
loop, so a single session/engine can be shared with the app. ``TestClient``
runs the app in a second thread and loop, which asyncpg connections (which are
loop-bound) cannot survive.
"""

import os

# Set before importing anything from ``app``: ``app.main`` builds a module-level
# application (uvicorn's entrypoint) and refuses to start without a signing key,
# and ``app.security`` picks its argon2 profile at import time. CI exports its
# own values; ``setdefault`` leaves those alone.
os.environ.setdefault("APP_ENV", "dev")
# Test-only, not a secret: 64 hex characters so it passes the length check.
os.environ.setdefault("JWT_SECRET", "0" * 64)
# Risk R3: production argon2 parameters cost ~64 MiB and tens of ms per hash,
# which would make this suite minutes long. Dev-only by construction.
os.environ.setdefault("TEST_ARGON2_FAST", "1")

#: Test-only, not a secret. Long enough to clear ``MIN_AI_AGENT_TOKEN_LENGTH``
#: and deliberately not the ``.env.example`` placeholder — the suite has to run
#: the same validation a real deployment does.
TEST_AI_AGENT_TOKEN = "test-only-internal-token-not-a-real-secret"

# AI is left *enabled* in the suite on purpose: the ai-agent client is never
# actually reached (every AI test injects a mock transport), and disabling it
# here would make `AI_ENABLED=false` the path the whole suite exercises while
# production runs the other one.
os.environ.setdefault("AI_AGENT_TOKEN", TEST_AI_AGENT_TOKEN)

from typing import AsyncIterator  # noqa: E402
from urllib.parse import unquote, urlsplit  # noqa: E402

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import User  # noqa: E402
from app.db.session import create_engine_from_url, create_sessionmaker  # noqa: E402
from app.deps import get_session  # noqa: E402
from app.main import create_app  # noqa: E402
from app.repositories.lists import SqlAlchemyListRepository  # noqa: E402
from app.repositories.users import SqlAlchemyUserRepository  # noqa: E402
from app.security import create_access_token, hash_password  # noqa: E402

SQLITE_URL = "sqlite+aiosqlite:///:memory:"

#: Test-only, not a secret. Long enough to satisfy the ≥32-character rule.
TEST_JWT_SECRET = "test-only-jwt-secret-not-a-real-key-0123456789"
TEST_PASSWORD = "correct horse battery"


def iter_api_routes(app):
    """Every real route of an app, descending into included routers.

    FastAPI 0.141 keeps an included router as a single ``_IncludedRouter``
    entry in ``app.routes`` instead of flattening its routes into it. Anything
    that inspects ``app.routes`` directly therefore sees only the endpoints
    declared on the app object — health — and silently skips /api/auth,
    /api/lists and /api/todos, which is exactly the set a structural check
    exists to police.
    """
    for route in app.routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from iter_api_routes(inner)
        elif hasattr(route, "dependant"):
            yield route


def iter_dependencies(dependant):
    """Every dependency in a route's tree, transitively."""
    for sub in dependant.dependencies:
        yield sub
        yield from iter_dependencies(sub)


def _configured_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or SQLITE_URL


def _database_name(url: str) -> str:
    """The database name of a SQLAlchemy URL, without query string."""
    path = urlsplit(url).path.lstrip("/")
    return unquote(path)


def assert_safe_to_wipe(url: str) -> None:
    """Refuse to drop tables in anything that is not obviously a test database.

    The ``engine`` fixture runs ``drop_all`` on whatever ``TEST_DATABASE_URL``
    points at. One stray copy-paste of a real URL would silently destroy real
    data, so the name must end in ``_test`` (or be the in-memory SQLite lane).
    """
    if url == SQLITE_URL:
        return
    name = _database_name(url)
    if name.endswith("_test"):
        return
    raise RuntimeError(
        "Refusing to run the test suite against "
        f"database {name!r}: these tests DROP every table. "
        "Point TEST_DATABASE_URL at a database whose name ends in '_test' "
        "(the compose db service creates 'todo_test' for exactly this), or "
        "unset it to use the in-memory SQLite lane."
    )


def pytest_report_header(config) -> str:
    url = _configured_url()
    lane = "sqlite (default)" if url == SQLITE_URL else "postgres (TEST_DATABASE_URL)"
    # Never print credentials from the URL.
    return f"database lane: {lane}"


def pytest_collection_modifyitems(config, items) -> None:
    if os.environ.get("TEST_DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="requires TEST_DATABASE_URL (PostgreSQL lane)")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def db_url() -> str:
    url = _configured_url()
    assert_safe_to_wipe(url)
    return url


@pytest.fixture
async def engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    """A fresh schema per test — no state is ever shared between tests."""
    assert_safe_to_wipe(db_url)
    engine = create_engine_from_url(db_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
async def db_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    sessionmaker = create_sessionmaker(engine)
    async with sessionmaker() as session:
        yield session


@pytest.fixture
def test_settings(db_url: str) -> Settings:
    return Settings(
        DATABASE_URL=db_url,
        APP_ENV="dev",
        JWT_SECRET=TEST_JWT_SECRET,
        JWT_EXPIRES_MINUTES=60,
        CORS_ORIGINS=["http://localhost:5173"],
    )


@pytest.fixture
def app(engine: AsyncEngine, db_session: AsyncSession, test_settings: Settings):
    application = create_app(test_settings)
    # The lifespan is not run here: wire the same engine/session the test uses.
    application.state.engine = engine
    application.state.sessionmaker = create_sessionmaker(engine)

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        """Mirror ``get_session`` exactly: no commit in the teardown.

        The override used to commit here, which quietly papered over the
        production bug — every request looked durable to the suite while a real
        client could race the teardown. Handlers commit for themselves now, and
        this override must not do it for them, or the regression would be
        invisible again.
        """
        try:
            yield db_session
        except Exception:
            await db_session.rollback()
            raise

    application.dependency_overrides[get_session] = override_get_session
    return application


async def create_user(
    session: AsyncSession,
    email: str,
    password: str = TEST_PASSWORD,
    display_name: str | None = None,
) -> User:
    """Create an account exactly as ``POST /api/auth/register`` does.

    Built directly rather than over HTTP so that fixtures cost one hash instead
    of two round trips, and so a test's own requests are the only thing the
    login rate limiter ever sees.
    """
    user = await SqlAlchemyUserRepository(session).create(
        email=email,
        password_hash=await hash_password(password),
        display_name=display_name,
    )
    await SqlAlchemyListRepository(session).create(user.id, "Inbox", is_default=True)
    await session.commit()
    return user


def bearer_headers(user: User, settings: Settings) -> dict[str, str]:
    token, _ = create_access_token(user.id, settings)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def user(db_session: AsyncSession) -> User:
    """The primary account. Every authenticated fixture below belongs to it."""
    return await create_user(db_session, "a@example.com")


@pytest.fixture
async def other_user(db_session: AsyncSession) -> User:
    """A second account, used by the cross-user isolation matrix."""
    return await create_user(db_session, "b@example.com")


@pytest.fixture
def auth_headers(user: User, test_settings: Settings) -> dict[str, str]:
    return bearer_headers(user, test_settings)


@pytest.fixture
def other_user_auth_headers(
    other_user: User, test_settings: Settings
) -> dict[str, str]:
    return bearer_headers(other_user, test_settings)


@pytest.fixture
async def client(app, auth_headers: dict[str, str]) -> AsyncIterator[AsyncClient]:
    """An authenticated client for the primary user.

    The bearer header is a *default* on the client rather than an argument on
    every call: it keeps the iteration-1 test bodies readable, and a test that
    needs a different (or no) identity uses ``anon_client`` or passes its own
    ``headers=``, which httpx merges over the defaults.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", headers=auth_headers
    ) as c:
        yield c


@pytest.fixture
async def anon_client(app) -> AsyncIterator[AsyncClient]:
    """A client with no credentials — for the 401 and auth-flow tests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
