"""Regression tests over a **real** uvicorn on a **real** socket.

Everything else in this suite drives the app through ``ASGITransport``, which
cannot see the bug QA reported: in-process, the test only regains control once
the response *and* all dependency teardown have finished, so a commit that
happens after the response still looks synchronous. Over TCP the client gets
the response bytes first and can issue its next request while teardown is still
running — which is how register(201) → immediate login returned 401 two times
in three, and a created todo was missing from the next list counts.

These tests therefore boot ``uvicorn app.main:app`` in a subprocess against a
file-backed database and talk to it over the loopback interface. They run in
both lanes: SQLite gets a temporary file database (``:memory:`` cannot be shared
with another process), PostgreSQL uses ``TEST_DATABASE_URL`` as it is.

**Where the teeth are.** These assertions were verified against a deliberately
reintroduced teardown-commit: on the PostgreSQL lane three of the four fail
(register → login returns 401, list counts lag). On the SQLite lane they pass
even with the bug, and that is not fixable here — the SQLite engine uses a
``StaticPool``, so every request shares one DBAPI connection and an uncommitted
transaction is fully visible to the next request. SQLite therefore runs these
as a real-socket smoke test (which is worth having: it is the only place the
suite exercises uvicorn, the module-level app and a live TCP client), and the
PostgreSQL lane is the one that can actually catch a regression. Both lanes run
in CI.
"""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.base import Base
from app.db.session import create_engine_from_url
from tests.conftest import SQLITE_URL, TEST_PASSWORD, assert_safe_to_wipe

BACKEND_ROOT = Path(__file__).resolve().parent.parent

#: The server has to import the app, connect and answer /api/health.
STARTUP_TIMEOUT_SECONDS = 60
PASSWORD = TEST_PASSWORD


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def live_database_url(db_url: str, tmp_path: Path) -> str:
    """A database URL a *second process* can open.

    The default lane's ``sqlite+aiosqlite:///:memory:`` lives inside the test
    process, so it is swapped for a file in ``tmp_path``; the PostgreSQL lane
    already points at a shared server and is used unchanged.
    """
    if db_url == SQLITE_URL:
        # A throwaway file inside pytest's own tmp_path — safe by construction,
        # which is why the "is this really a test database" guard (aimed at a
        # shared server URL) is not applied to it.
        url = f"sqlite+aiosqlite:///{tmp_path / 'live.db'}"
    else:
        url = db_url
        assert_safe_to_wipe(url)

    engine: AsyncEngine = create_engine_from_url(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()
    try:
        yield url
    finally:
        engine = create_engine_from_url(url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
async def live_server(live_database_url: str):
    """``uvicorn app.main:app`` on a free loopback port; yields its base URL."""
    port = _free_port()
    env = {
        **os.environ,
        "DATABASE_URL": live_database_url,
        "APP_ENV": "dev",
        "TEST_ARGON2_FAST": "1",
        "JWT_SECRET": "0" * 64,  # test-only, not a secret
        # These tests make far more than 30 credential calls from one address;
        # the limiter itself is covered in tests/test_ratelimit.py.
        "AUTH_IP_RATE_LIMIT": "10000",
        # Never let a developer's .env leak into the subprocess' settings.
        "TEST_DATABASE_URL": "",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=BACKEND_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        await _wait_until_healthy(process, base_url)
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=10)


async def _wait_until_healthy(process: subprocess.Popen, base_url: str) -> None:
    import asyncio

    deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT_SECONDS
    async with AsyncClient(base_url=base_url, timeout=5) as client:
        while asyncio.get_running_loop().time() < deadline:
            if process.poll() is not None:
                output = process.stdout.read().decode(errors="replace")
                raise RuntimeError(f"uvicorn exited early:\n{output}")
            try:
                if (await client.get("/api/health")).status_code == 200:
                    return
            except Exception:  # noqa: BLE001 - not up yet
                pass
            await asyncio.sleep(0.1)
    process.terminate()
    raise RuntimeError("uvicorn did not become healthy in time")


async def test_register_then_immediate_login_over_a_real_socket(
    live_server: str,
) -> None:
    """The exact sequence QA reproduced with curl: 20 accounts, no 401."""
    failures: list[tuple[str, int, str]] = []

    async with AsyncClient(base_url=live_server, timeout=30) as client:
        for index in range(20):
            email = f"race-{index}@example.com"
            registered = await client.post(
                "/api/auth/register", json={"email": email, "password": PASSWORD}
            )
            assert registered.status_code == 201, registered.text

            # No pause: the whole point is to arrive while the previous
            # request's dependency teardown could still be running.
            logged_in = await client.post(
                "/api/auth/login", json={"email": email, "password": PASSWORD}
            )
            if logged_in.status_code != 200:
                failures.append((email, logged_in.status_code, logged_in.text))

    assert failures == []


async def test_register_then_immediately_use_the_token(live_server: str) -> None:
    """The account and its Inbox must both be durable before the 201 lands."""
    async with AsyncClient(base_url=live_server, timeout=30) as client:
        email = "immediate@example.com"
        assert (
            await client.post(
                "/api/auth/register", json={"email": email, "password": PASSWORD}
            )
        ).status_code == 201

        token = (
            await client.post(
                "/api/auth/login", json={"email": email, "password": PASSWORD}
            )
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        me = await client.get("/api/auth/me", headers=headers)
        assert me.status_code == 200

        lists = await client.get("/api/lists", headers=headers)
        assert lists.status_code == 200
        assert [row["name"] for row in lists.json()] == ["Inbox"]


async def test_created_todos_are_immediately_visible_over_a_real_socket(
    live_server: str,
) -> None:
    """Create → list, ten times, with no settling time: counts must never lag."""
    async with AsyncClient(base_url=live_server, timeout=30) as client:
        email = "counts@example.com"
        await client.post(
            "/api/auth/register", json={"email": email, "password": PASSWORD}
        )
        token = (
            await client.post(
                "/api/auth/login", json={"email": email, "password": PASSWORD}
            )
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        stale: list[tuple[int, dict]] = []
        for index in range(1, 11):
            created = await client.post(
                "/api/todos", json={"title": f"todo {index}"}, headers=headers
            )
            assert created.status_code == 201, created.text

            listed = await client.get("/api/todos", headers=headers)
            assert listed.status_code == 200
            if len(listed.json()) != index:
                stale.append((index, {"todos": len(listed.json())}))

            counts = (await client.get("/api/lists", headers=headers)).json()[0]
            if (counts["todo_count"], counts["active_count"]) != (index, index):
                stale.append((index, counts))

        assert stale == []


async def test_slice3_mutations_are_durable_immediately_over_a_real_socket(
    live_server: str,
) -> None:
    """Tags and subtasks, read back with no settling time.

    These endpoints write more than one table per request — a todo plus its
    tags and join rows, a subtask plus its parent's collection — so a commit
    that lands after the response would show up here as a filter that finds
    nothing or a parent with no children.
    """
    async with AsyncClient(base_url=live_server, timeout=30) as client:
        email = "enrichment@example.com"
        await client.post(
            "/api/auth/register", json={"email": email, "password": PASSWORD}
        )
        token = (
            await client.post(
                "/api/auth/login", json={"email": email, "password": PASSWORD}
            )
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        created = await client.post(
            "/api/todos",
            json={"title": "Buy milk", "priority": "high", "tags": ["home", "errand"]},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        todo_id = created.json()["id"]

        # The tag rows and the join rows must be there for the filter to match.
        filtered = await client.get(
            "/api/todos", params={"tag": ["home", "errand"]}, headers=headers
        )
        assert [row["id"] for row in filtered.json()] == [todo_id]
        assert [tag["name"] for tag in (await client.get("/api/tags", headers=headers)).json()] == [
            "errand",
            "home",
        ]

        subtask = await client.post(
            f"/api/todos/{todo_id}/subtasks", json={"title": "Check fridge"},
            headers=headers,
        )
        assert subtask.status_code == 201, subtask.text
        parent = (await client.get(f"/api/todos/{todo_id}", headers=headers)).json()
        assert [sub["title"] for sub in parent["subtasks"]] == ["Check fridge"]

        patched = await client.patch(
            f"/api/todos/{todo_id}", json={"tags": ["home"], "description": "2%"},
            headers=headers,
        )
        assert patched.status_code == 200
        refetched = (await client.get(f"/api/todos/{todo_id}", headers=headers)).json()
        assert refetched["tags"] == ["home"]
        assert refetched["description"] == "2%"

        tags = (await client.get("/api/tags", headers=headers)).json()
        errand = next(tag for tag in tags if tag["name"] == "errand")
        home = next(tag for tag in tags if tag["name"] == "home")

        renamed = await client.patch(
            f"/api/tags/{home['id']}", json={"name": "house"}, headers=headers
        )
        assert renamed.status_code == 200
        assert (
            await client.get(f"/api/todos/{todo_id}", headers=headers)
        ).json()["tags"] == ["house"]

        assert (
            await client.delete(f"/api/tags/{errand['id']}", headers=headers)
        ).status_code == 204
        names = [
            tag["name"] for tag in (await client.get("/api/tags", headers=headers)).json()
        ]
        assert names == ["house"]


async def test_mutations_are_durable_immediately_over_a_real_socket(
    live_server: str,
) -> None:
    """Toggle and delete land before their responses do, too."""
    async with AsyncClient(base_url=live_server, timeout=30) as client:
        email = "mutations@example.com"
        await client.post(
            "/api/auth/register", json={"email": email, "password": PASSWORD}
        )
        token = (
            await client.post(
                "/api/auth/login", json={"email": email, "password": PASSWORD}
            )
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        created = (
            await client.post(
                "/api/todos", json={"title": "Toggle me"}, headers=headers
            )
        ).json()

        patched = await client.patch(
            f"/api/todos/{created['id']}", json={"completed": True}, headers=headers
        )
        assert patched.status_code == 200
        counts = (await client.get("/api/lists", headers=headers)).json()[0]
        assert (counts["todo_count"], counts["active_count"]) == (1, 0)

        work = await client.post("/api/lists", json={"name": "Work"}, headers=headers)
        assert work.status_code == 201
        names = [row["name"] for row in (await client.get("/api/lists", headers=headers)).json()]
        assert names == ["Inbox", "Work"]

        assert (
            await client.delete(f"/api/todos/{created['id']}", headers=headers)
        ).status_code == 204
        assert (await client.get("/api/todos", headers=headers)).json() == []

        assert (
            await client.delete(f"/api/lists/{work.json()['id']}", headers=headers)
        ).status_code == 204
        names = [row["name"] for row in (await client.get("/api/lists", headers=headers)).json()]
        assert names == ["Inbox"]
