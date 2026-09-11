"""The request session commits before the response, not after it.

QA reproduced the original bug over a real socket: a dependency with ``yield``
resolves its teardown *after* the response has been sent, so a client acting on
a 201 could beat the transaction that produced it (register → immediate login
returned 401; create todo → immediate list showed stale counts). Under
``ASGITransport`` the test only regains control once teardown has finished, so
the whole suite stayed green while real clients raced.

``test_live_server.py`` is the end-to-end proof over TCP. This module pins the
two structural properties that make it hold, so a future edit that reintroduces
the pattern fails here with a readable message rather than intermittently in
somebody's browser.
"""

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import SessionDep, get_session, get_todo_repository
from tests.conftest import iter_api_routes, iter_dependencies

MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}

#: POST routes that deliberately write nothing, and so have nothing to commit.
#:
#: Master decision D-AI1: every AI endpoint returns a *draft* the user reviews;
#: an LLM is not an unreviewed write path. They are POSTs only because they
#: carry a body. Listed one by one rather than matched by prefix, so adding a
#: writing route under ``/api/ai`` fails this test instead of inheriting an
#: exemption — and ``test_ai_api.py`` proves the exemption is earned by
#: asserting the database is unchanged after every AI call.
NON_WRITING_POST_PATHS = {
    "/api/ai/parse-todo",
    "/api/ai/suggest-subtasks",
    "/api/ai/suggest-metadata",
    "/api/ai/daily-summary",
    "/api/ai/edit-todo",
}


def _session_scopes(route) -> set[str | None]:
    return {
        getattr(dependency, "scope", None)
        for dependency in iter_dependencies(route.dependant)
        if dependency.call is get_session
    }


def test_the_route_walk_actually_reaches_the_api_routes(app) -> None:
    """Guards the guard: an inspection that silently sees nothing proves nothing."""
    paths = {route.path for route in iter_api_routes(app)}
    assert {
        "/api/auth/register",
        "/api/lists",
        "/api/todos",
        "/api/tags",
        "/api/todos/{todo_id}/subtasks",
    } <= paths


def test_every_non_writing_exemption_names_a_real_route(app) -> None:
    """A stale exemption is a hole — it would cover a route nobody checked."""
    paths = {route.path for route in iter_api_routes(app)}
    assert NON_WRITING_POST_PATHS <= paths


def test_every_route_resolves_the_session_with_function_scope(app) -> None:
    offenders = {
        (route.path, tuple(sorted(route.methods))): scopes
        for route in iter_api_routes(app)
        if (scopes := _session_scopes(route)) and scopes != {"function"}
    }
    assert offenders == {}


def test_no_route_mixes_two_session_scopes(app) -> None:
    """Two declarations that disagree could resolve to two different sessions —
    the repository would write to one and the handler would commit the other."""
    for route in iter_api_routes(app):
        scopes = _session_scopes(route)
        assert len(scopes) <= 1, (route.path, scopes)


def test_every_mutating_route_takes_the_session_so_it_can_commit(app) -> None:
    """A write path that never sees the session cannot commit its own work, and
    would be back to relying on teardown."""
    for route in iter_api_routes(app):
        if not (route.methods & MUTATING_METHODS):
            continue
        if route.path in NON_WRITING_POST_PATHS:
            continue
        handler_params = route.dependant.dependencies
        takes_session = any(
            dependency.call is get_session for dependency in handler_params
        )
        assert takes_session, f"{sorted(route.methods)} {route.path}"


async def test_get_session_does_not_commit_in_its_teardown(app, engine) -> None:
    """The dependency's own contract: it opens and closes, nothing more."""
    calls: list[str] = []

    class SpySession:
        async def commit(self) -> None:
            calls.append("commit")

        async def rollback(self) -> None:
            calls.append("rollback")

        async def close(self) -> None:
            calls.append("close")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            await self.close()

    class FakeRequest:
        app = type("App", (), {"state": type("State", (), {})()})()

    FakeRequest.app.state.sessionmaker = SpySession

    generator = get_session(FakeRequest())
    session = await generator.__anext__()
    assert isinstance(session, SpySession)
    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()

    assert "commit" not in calls
    assert calls == ["close"]


async def test_get_session_still_rolls_back_on_an_exception() -> None:
    calls: list[str] = []

    class SpySession:
        async def commit(self) -> None:
            calls.append("commit")

        async def rollback(self) -> None:
            calls.append("rollback")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            calls.append("close")

    class FakeRequest:
        app = type("App", (), {"state": type("State", (), {})()})()

    FakeRequest.app.state.sessionmaker = SpySession

    generator = get_session(FakeRequest())
    await generator.__anext__()
    with pytest.raises(RuntimeError):
        await generator.athrow(RuntimeError("handler blew up"))

    assert calls == ["rollback", "close"]
    assert "commit" not in calls


async def test_the_handler_and_its_repository_share_one_session(
    engine, test_settings
) -> None:
    """``scope="function"`` must not split the per-request dependency cache."""
    from app.db.session import create_sessionmaker

    probe = FastAPI()
    probe.state.sessionmaker = create_sessionmaker(engine)
    seen: dict[str, object] = {}

    @probe.get("/probe")
    async def _probe(
        session: SessionDep,
        repo=Depends(get_todo_repository),
    ) -> dict:
        seen["handler"] = session
        seen["repository"] = repo._session
        return {"same": session is repo._session}

    transport = ASGITransport(app=probe)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get("/probe")).json() == {"same": True}

    assert isinstance(seen["handler"], AsyncSession)
    assert seen["handler"] is seen["repository"]
