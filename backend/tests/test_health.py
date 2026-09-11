"""Health probes: /api/health must stay DB-free, /api/health/ready must not."""

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event

from app.db.session import create_engine_from_url, create_sessionmaker
from app.main import create_app

UNREACHABLE_URL = "postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nothing"


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_issues_no_sql(client: AsyncClient, engine) -> None:
    """It is the compose and Playwright readiness probe — it must never block
    on the database."""
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        response = await client.get("/api/health")
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)

    assert response.status_code == 200
    assert statements == []


async def test_health_ready_reports_the_database(client: AsyncClient) -> None:
    response = await client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_health_ok_and_ready_503_when_the_database_is_unreachable(
    test_settings,
) -> None:
    settings = test_settings.model_copy(update={"DATABASE_URL": UNREACHABLE_URL})
    app = create_app(settings)
    app.state.engine = create_engine_from_url(settings.DATABASE_URL)
    app.state.sessionmaker = create_sessionmaker(app.state.engine)

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            liveness = await c.get("/api/health")
            assert liveness.status_code == 200
            assert liveness.json() == {"status": "ok"}

            readiness = await c.get("/api/health/ready")
            assert readiness.status_code == 503
            assert readiness.json() == {
                "detail": "Database unavailable",
                "code": "database_unavailable",
            }
    finally:
        await app.state.engine.dispose()
