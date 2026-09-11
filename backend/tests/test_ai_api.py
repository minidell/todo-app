"""``/api/ai/*`` against a mocked ai-agent (slice-4 spec A6.6).

ai-agent is replaced by an ``httpx.MockTransport``, so these tests are about
the *proxy*: what it sends upstream, what it does with what comes back, and
what a caller sees when the upstream misbehaves. The model itself is BACKEND-B's
suite; the one live check lives at the bottom, behind ``AI_AGENT_LIVE=1``.

The rule the whole module exists to defend is master decision **D-AI1**: no AI
endpoint writes to the database. Every happy path here is followed by a
snapshot comparison, so an endpoint that ever grew a write would fail loudly
rather than quietly persisting whatever a language model produced.
"""

import asyncio
import logging
import os
from typing import Any, Callable
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_client import (
    HEALTH_CACHE_SECONDS,
    INTERNAL_TOKEN_HEADER,
    PING_PATH,
    AiAgentClient,
)
from app.config import Settings
from app.db.models import Tag, Todo, TodoList, User
from app.main import create_app
from tests.conftest import TEST_AI_AGENT_TOKEN, bearer_headers

STATUS = "/api/ai/status"
PARSE = "/api/ai/parse-todo"
SUBTASKS = "/api/ai/suggest-subtasks"
METADATA = "/api/ai/suggest-metadata"
SUMMARY = "/api/ai/daily-summary"
EDIT = "/api/ai/edit-todo"
TODAY = "2026-09-02"

#: The proposal §2.2 mandates for "nothing to change": every key present, every
#: value inert. Tests override exactly the keys they are about, so a case can
#: never accidentally depend on a second change it did not ask for.
NO_CHANGES: dict[str, Any] = {
    "title": None,
    "description": None,
    "clear_description": False,
    "priority": None,
    "due_date": None,
    "clear_due_date": False,
    "completed": None,
    "tags_add": [],
    "tags_remove": [],
    "subtasks": [],
}

HEALTHY = {
    "status": "ok",
    "ollama": "ok",
    "model": "qwen2.5:3b",
    "model_present": True,
}


class FakeAgent:
    """A stand-in ai-agent that records every request it is sent."""

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self._handler = handler
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.content:
            import json

            self.bodies.append(json.loads(request.content))
        return self._handler(request)

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def last_body(self) -> dict[str, Any]:
        return self.bodies[-1]


def ping_like_the_real_agent(request: httpx.Request) -> httpx.Response:
    """``GET /ai/ping`` exactly as ai-agent serves it (master §8.2).

    It lives on the authenticated ``/ai`` router, so the token check is the
    whole endpoint: 200 ``{"status": "ok"}`` with the right secret, and the
    standard 401 body without it. No Ollama call, no I/O.
    """
    if request.headers.get(INTERNAL_TOKEN_HEADER) != TEST_AI_AGENT_TOKEN:
        return httpx.Response(
            401, json={"detail": "Invalid internal token", "code": "unauthorized"}
        )
    return httpx.Response(200, json={"status": "ok"})


def responder(**by_path: Any) -> Callable[[httpx.Request], httpx.Response]:
    """Map ai-agent paths to canned responses (a dict, or a ready Response).

    ``/ai/ping`` answers like the real service unless a test says otherwise:
    every ``/api/ai/status`` call probes it (IT3-2), and a fake that 404'd it
    would make "ai-agent is healthy" mean something it does not mean upstream.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        canned = by_path.get(request.url.path)
        if canned is None and request.url.path == PING_PATH:
            return ping_like_the_real_agent(request)
        if canned is None:
            return httpx.Response(404, json={"detail": "no such route"})
        if isinstance(canned, httpx.Response):
            return canned
        if callable(canned):
            return canned(request)
        return httpx.Response(200, json=canned)

    return handle


def install_agent(app, agent: FakeAgent) -> FakeAgent:
    """Point the app's ai-agent client at ``agent``."""
    app.state.ai_client = AiAgentClient(
        app.state.settings, transport=httpx.MockTransport(agent)
    )
    return agent


@pytest.fixture
def agent(app) -> FakeAgent:
    """A healthy ai-agent that answers every endpoint with a sane payload."""
    return install_agent(
        app,
        FakeAgent(
            responder(
                **{
                    "/health": HEALTHY,
                    "/ai/parse-todo": {
                        "title": "Call the dentist",
                        "description": None,
                        "priority": "high",
                        "due_date": "2026-09-03",
                        "tags": ["health"],
                        "subtasks": [{"title": "Find the phone number"}],
                    },
                    "/ai/suggest-subtasks": {
                        "subtasks": [{"title": "Step one"}, {"title": "Step two"}]
                    },
                    "/ai/suggest-metadata": {
                        "priority": "high",
                        "tags": ["work", "urgent"],
                    },
                    "/ai/daily-summary": {"summary": "You have 2 open todos."},
                    "/ai/edit-todo": {
                        **NO_CHANGES,
                        "title": "Call the dentist",
                        "priority": "high",
                        "tags_add": ["health"],
                        "subtasks": [
                            {
                                "action": "add",
                                "index": None,
                                "title": "Find the phone number",
                            }
                        ],
                    },
                }
            )
        ),
    )


def proposing(**overrides: Any) -> dict:
    """One edit proposal, spelled as ai-agent would spell it (§2.2)."""
    return {**NO_CHANGES, **overrides}


def install_proposal(app, **overrides: Any) -> FakeAgent:
    return install_agent(
        app, FakeAgent(responder(**{"/ai/edit-todo": proposing(**overrides)}))
    )


async def ask_edit(
    client: AsyncClient, todo_id: str, instruction: str = "change it"
) -> dict:
    response = await client.post(
        EDIT, json={"todo_id": todo_id, "instruction": instruction, "today": TODAY}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def snapshot(session: AsyncSession) -> dict[str, list]:
    """Everything an AI endpoint could conceivably touch.

    **Mapped columns only.** ``vars()`` also carries whichever relationships
    happen to be loaded on the instance, and that is session bookkeeping rather
    than persisted state: a request that raises rolls this shared session back,
    expiring the collections, so an otherwise identical database would compare
    unequal purely because ``Todo.tags`` was loaded before and not after. The
    columns are what "the database is byte-identical" means.
    """

    async def rows(model, *order):
        columns = {attr.key for attr in sa_inspect(model).mapper.column_attrs}
        result = await session.execute(select(model).order_by(*order))
        return [
            {
                key: value
                for key, value in vars(entity).items()
                if key in columns
            }
            for entity in result.scalars().unique().all()
        ]

    return {
        "todos": await rows(Todo, Todo.created_at, Todo.id),
        "lists": await rows(TodoList, TodoList.created_at, TodoList.id),
        "tags": await rows(Tag, Tag.name),
    }


@pytest.fixture
async def todo(client: AsyncClient) -> dict:
    created = await client.post(
        "/api/todos",
        json={"title": "Plan the offsite", "description": "Two days", "tags": ["work"]},
    )
    assert created.status_code == 201
    return created.json()


# --- status ----------------------------------------------------------------


async def test_status_reports_a_healthy_agent(
    client: AsyncClient, agent: FakeAgent
) -> None:
    response = await client.get(STATUS)
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "available": True,
        "model": "qwen2.5:3b",
        "reason": None,
    }


async def test_status_requires_authentication(anon_client: AsyncClient) -> None:
    assert (await anon_client.get(STATUS)).status_code == 401


async def test_status_is_200_when_ollama_is_down(app, client: AsyncClient) -> None:
    """ai-agent is up but the model is not usable: available must be false."""
    install_agent(
        app,
        FakeAgent(
            responder(
                **{
                    "/health": {
                        "status": "ok",
                        "ollama": "unavailable",
                        "model": "qwen2.5:3b",
                        "model_present": False,
                    }
                }
            )
        ),
    )

    response = await client.get(STATUS)
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    assert response.json()["available"] is False
    assert response.json()["reason"] == "model_unavailable"


async def test_status_is_200_when_the_model_was_never_pulled(
    app, client: AsyncClient
) -> None:
    install_agent(
        app,
        FakeAgent(
            responder(
                **{
                    "/health": {
                        "status": "ok",
                        "ollama": "ok",
                        "model": "qwen2.5:3b",
                        "model_present": False,
                    }
                }
            )
        ),
    )

    assert (await client.get(STATUS)).json()["available"] is False


async def test_status_is_200_when_the_agent_is_unreachable(
    app, client: AsyncClient
) -> None:
    """The endpoint that must never fail (§6.6), with nothing behind it."""

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    install_agent(app, FakeAgent(refuse))

    response = await client.get(STATUS)
    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "available": False,
        "model": None,
        "reason": "unreachable",
    }


async def test_status_is_200_when_the_agent_times_out(
    app, client: AsyncClient
) -> None:
    def hang(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    install_agent(app, FakeAgent(hang))

    assert (await client.get(STATUS)).json()["available"] is False


# --- health caching --------------------------------------------------------


class FakeClock:
    """A monotonic clock a test can move by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def install_agent_with_clock(app, agent: FakeAgent, clock: FakeClock) -> FakeAgent:
    app.state.ai_client = AiAgentClient(
        app.state.settings, transport=httpx.MockTransport(agent), clock=clock
    )
    return agent


async def test_repeated_status_calls_hit_the_agent_once(
    app, client: AsyncClient
) -> None:
    """/status is not rate-limited, so it must not be a free upstream amplifier."""
    clock = FakeClock()
    agent = install_agent_with_clock(
        app, FakeAgent(responder(**{"/health": HEALTHY})), clock
    )

    for _ in range(10):
        assert (await client.get(STATUS)).json()["available"] is True

    # One *pair* of probes (health + ping), shared by every caller in the
    # window — ten tabs asking still cost two upstream round trips.
    assert agent.calls == 2
    assert sorted(request.url.path for request in agent.requests) == [
        PING_PATH,
        "/health",
    ]


async def test_the_health_cache_expires(app, client: AsyncClient) -> None:
    clock = FakeClock()
    agent = install_agent_with_clock(
        app, FakeAgent(responder(**{"/health": HEALTHY})), clock
    )

    await client.get(STATUS)
    clock.advance(HEALTH_CACHE_SECONDS - 0.01)
    await client.get(STATUS)
    assert agent.calls == 2  # one pair of probes

    clock.advance(0.02)
    await client.get(STATUS)
    assert agent.calls == 4


async def test_a_failed_health_probe_is_cached_too(
    app, client: AsyncClient
) -> None:
    """The down case is the one where an uncached probe costs the most."""
    clock = FakeClock()

    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    agent = install_agent_with_clock(app, FakeAgent(refuse), clock)

    for _ in range(5):
        assert (await client.get(STATUS)).json()["available"] is False

    assert agent.calls == 2


async def test_a_recovering_agent_is_noticed_after_the_cache_window(
    app, client: AsyncClient
) -> None:
    clock = FakeClock()
    state = {"up": False}

    def flaky(request: httpx.Request) -> httpx.Response:
        if not state["up"]:
            raise httpx.ConnectError("connection refused")
        if request.url.path == PING_PATH:
            return ping_like_the_real_agent(request)
        return httpx.Response(200, json=HEALTHY)

    install_agent_with_clock(app, FakeAgent(flaky), clock)

    assert (await client.get(STATUS)).json()["available"] is False

    state["up"] = True
    assert (await client.get(STATUS)).json()["available"] is False  # still cached

    clock.advance(HEALTH_CACHE_SECONDS + 1)
    assert (await client.get(STATUS)).json()["available"] is True


# --- why AI is unavailable (IT3-2, master §5.1) ----------------------------
#
# The row that matters is ``auth_failed``: ai-agent answers /health happily
# while rejecting our shared secret, which used to report available:true and
# hand the user AI buttons that were certain to fail.

UNHEALTHY = {
    "status": "ok",
    "ollama": "unavailable",
    "model": "qwen2.5:3b",
    "model_present": False,
}

REJECTS_OUR_TOKEN = httpx.Response(
    401, json={"detail": "Invalid internal token", "code": "unauthorized"}
)


def install_probes(app, *, health: Any, ping: Any) -> FakeAgent:
    return install_agent(
        app, FakeAgent(responder(**{"/health": health, PING_PATH: ping}))
    )


async def test_status_reports_auth_failed_when_the_agent_rejects_our_token(
    app, client: AsyncClient
) -> None:
    """The case that used to look identical to a healthy service."""
    install_probes(app, health=HEALTHY, ping=REJECTS_OUR_TOKEN)

    body = (await client.get(STATUS)).json()
    assert body == {
        "enabled": True,
        "available": False,
        "model": "qwen2.5:3b",
        "reason": "auth_failed",
    }


async def test_auth_failed_outranks_a_missing_model(app, client: AsyncClient) -> None:
    """A token mismatch breaks every AI call whatever Ollama is doing, and it
    is the one an operator can fix — so it is the reason reported."""
    install_probes(app, health=UNHEALTHY, ping=REJECTS_OUR_TOKEN)

    assert (await client.get(STATUS)).json()["reason"] == "auth_failed"


async def test_an_unreachable_agent_outranks_the_ping_verdict(
    app, client: AsyncClient
) -> None:
    """If /health did not answer, why the authenticated probe failed is noise."""

    def health_is_down(request: httpx.Request) -> httpx.Response:
        if request.url.path == PING_PATH:
            return REJECTS_OUR_TOKEN
        raise httpx.ConnectError("connection refused")

    install_agent(app, FakeAgent(health_is_down))

    assert (await client.get(STATUS)).json()["reason"] == "unreachable"


async def test_a_ping_that_fails_with_anything_else_reads_as_unreachable(
    app, client: AsyncClient
) -> None:
    install_probes(app, health=HEALTHY, ping=httpx.Response(500, json={}))

    body = (await client.get(STATUS)).json()
    assert (body["available"], body["reason"]) == (False, "unreachable")


async def test_a_ping_that_raises_reads_as_unreachable(
    app, client: AsyncClient
) -> None:
    def hang_the_ping(request: httpx.Request) -> httpx.Response:
        if request.url.path == PING_PATH:
            raise httpx.ReadTimeout("too slow")
        return httpx.Response(200, json=HEALTHY)

    install_agent(app, FakeAgent(hang_the_ping))

    assert (await client.get(STATUS)).json()["reason"] == "unreachable"


async def test_a_healthy_agent_that_lost_ollama_reads_as_model_unavailable(
    app, client: AsyncClient
) -> None:
    install_probes(app, health=UNHEALTHY, ping={"status": "ok"})

    body = (await client.get(STATUS)).json()
    assert (body["available"], body["reason"]) == (False, "model_unavailable")


async def test_reason_is_null_exactly_when_ai_is_available(
    app, client: AsyncClient
) -> None:
    """The invariant the frontend branches on (§5.1)."""
    for health, ping, available in (
        (HEALTHY, {"status": "ok"}, True),
        (HEALTHY, REJECTS_OUR_TOKEN, False),
        (UNHEALTHY, {"status": "ok"}, False),
    ):
        install_probes(app, health=health, ping=ping)
        body = (await client.get(STATUS)).json()
        assert body["available"] is available
        assert (body["reason"] is None) is available


async def test_status_still_answers_200_if_a_probe_forgets_its_reason(
    app, client: AsyncClient, monkeypatch
) -> None:
    """``/status`` may never fail (§6.6), not even on the new invariant.

    ``reason is null iff available`` is enforced by the response model, so an
    unavailable answer that forgot to say why would otherwise turn the one
    endpoint that must always work into a 500.
    """
    from app.ai_client import AiHealth

    install_probes(app, health=HEALTHY, ping={"status": "ok"})

    async def forgetful_health(_self) -> AiHealth:
        return AiHealth(available=False, model=None, reason=None)

    monkeypatch.setattr(AiAgentClient, "health", forgetful_health)

    response = await client.get(STATUS)
    assert response.status_code == 200
    assert response.json()["reason"] == "unreachable"


async def test_status_probes_health_and_ping_concurrently(
    app, client: AsyncClient
) -> None:
    """Risk R3: two probes must not double the worst-case latency.

    Both requests have to be in flight at the same moment, which is only true
    if they were gathered rather than awaited one after the other. The second
    one to arrive releases the first, so a sequential implementation deadlocks
    here and fails on the timeout instead of quietly being slow.
    """
    both_arrived = asyncio.Event()
    seen: list[str] = []

    async def gate(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if len(seen) == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=2)
        if request.url.path == PING_PATH:
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json=HEALTHY)

    app.state.ai_client = AiAgentClient(
        app.state.settings, transport=httpx.MockTransport(gate)
    )

    assert (await client.get(STATUS)).json()["available"] is True
    assert sorted(seen) == [PING_PATH, "/health"]


async def test_a_token_mismatch_is_logged_once_per_probe_without_the_token(
    app, client: AsyncClient, caplog
) -> None:
    """Criterion 14: name the variable, never the value."""
    install_probes(app, health=HEALTHY, ping=REJECTS_OUR_TOKEN)

    with caplog.at_level(logging.WARNING, logger="app.ai_client"):
        await client.get(STATUS)
        await client.get(STATUS)  # served from the cache: no second probe

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "AI_AGENT_TOKEN" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert TEST_AI_AGENT_TOKEN not in caplog.text


async def test_the_ping_probe_carries_the_shared_secret(
    app, client: AsyncClient
) -> None:
    agent = install_probes(app, health=HEALTHY, ping={"status": "ok"})

    await client.get(STATUS)

    ping = next(r for r in agent.requests if r.url.path == PING_PATH)
    assert ping.headers[INTERNAL_TOKEN_HEADER] == TEST_AI_AGENT_TOKEN
    assert ping.method == "GET"


# (``reason: "disabled"`` is asserted with the rest of the AI-off contract, in
# ``test_status_reports_disabled`` below.)


# --- the shared secret -----------------------------------------------------


async def test_every_upstream_call_carries_the_internal_token(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    await client.get(STATUS)
    await client.post(PARSE, json={"text": "call the dentist", "today": TODAY})
    await client.post(SUBTASKS, json={"todo_id": todo["id"], "max_items": 5})

    # Four: /status probes both /health and /ai/ping (IT3-2).
    assert agent.calls == 4
    for request in agent.requests:
        assert request.headers[INTERNAL_TOKEN_HEADER] == TEST_AI_AGENT_TOKEN


async def test_the_internal_token_never_reaches_the_client(
    client: AsyncClient, agent: FakeAgent
) -> None:
    response = await client.post(
        PARSE, json={"text": "call the dentist", "today": TODAY}
    )
    assert TEST_AI_AGENT_TOKEN not in response.text
    assert INTERNAL_TOKEN_HEADER.lower() not in {
        key.lower() for key in response.headers
    }


async def test_an_upstream_401_is_ai_unavailable_and_never_leaks_the_token(
    app, client: AsyncClient, caplog
) -> None:
    """A misconfigured shared secret must be diagnosable without logging it."""
    install_agent(
        app,
        FakeAgent(
            responder(
                **{
                    "/ai/parse-todo": httpx.Response(
                        401, json={"detail": "Invalid internal token", "code": "unauthorized"}
                    )
                }
            )
        ),
    )

    with caplog.at_level("WARNING"):
        response = await client.post(
            PARSE, json={"text": "anything", "today": TODAY}
        )

    assert response.status_code == 503
    assert response.json()["code"] == "ai_unavailable"
    assert "AI_AGENT_TOKEN" in caplog.text
    assert TEST_AI_AGENT_TOKEN not in caplog.text
    # Nor does ai-agent's own wording reach the user.
    assert "Invalid internal token" not in response.text


# --- parse-todo ------------------------------------------------------------


async def test_parse_todo_returns_a_draft(
    client: AsyncClient, agent: FakeAgent
) -> None:
    response = await client.post(
        PARSE, json={"text": "call the dentist tomorrow, urgent", "today": TODAY}
    )

    assert response.status_code == 200
    assert response.json() == {
        "draft": {
            "title": "Call the dentist",
            "description": None,
            "priority": "high",
            "due_date": "2026-09-03",
            "tags": ["health"],
            "subtasks": [{"title": "Find the phone number"}],
        }
    }


async def test_parse_todo_adds_the_callers_own_tags(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    """The client sends only text and today; ``known_tags`` is assembled here."""
    await client.post(PARSE, json={"text": "something", "today": TODAY})

    assert agent.last_body["known_tags"] == ["work"]
    assert agent.last_body["today"] == TODAY
    assert set(agent.last_body) == {"text", "today", "known_tags"}


async def test_parse_todo_never_sees_another_users_tags(
    app, client: AsyncClient, agent: FakeAgent, db_session, other_user: User
) -> None:
    from app.repositories.tags import SqlAlchemyTagRepository

    await SqlAlchemyTagRepository(db_session).get_or_create_many(
        other_user.id, ["secret-project"]
    )
    await db_session.commit()

    await client.post(PARSE, json={"text": "something", "today": TODAY})
    assert "secret-project" not in agent.last_body["known_tags"]


@pytest.mark.parametrize("text", ["", "   ", "\n\t  \n"])
async def test_parse_todo_rejects_an_empty_note_without_calling_the_model(
    client: AsyncClient, agent: FakeAgent, app, text: str
) -> None:
    """A whitespace-only note is a 422 naming the field, not a 503.

    ``min_length`` has to see the *stripped* value: otherwise ``"   "`` passes
    validation here, ai-agent rejects it with its own 422, and the user is told
    "AI is unavailable" — a misleading answer to a plainly invalid request.
    """
    response = await client.post(PARSE, json={"text": text, "today": TODAY})

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [["body", "text"]]
    assert agent.calls == 0


async def test_a_rejected_body_still_counts_against_the_rate_limit(
    app, client: AsyncClient, agent: FakeAgent
) -> None:
    """Recorded because it is surprising, and because it is the safe direction.

    FastAPI resolves route-level ``dependencies`` before it validates the body,
    so the limiter counts a request whose body is then rejected. That is the
    right way round — otherwise malformed bodies would be an unmetered way to
    make the server work — but it means a 422 costs the caller a slot, and
    anyone reasoning about the budget should know it.
    """
    limiter = app.state.ai_rate_limiter
    limiter.reset()

    for _ in range(3):
        response = await client.post(PARSE, json={"text": "   ", "today": TODAY})
        assert response.status_code == 422

    key = next(iter(limiter._hits))
    remaining = 0
    while limiter.hit(key).allowed:
        remaining += 1
    assert limiter.limit - remaining == 3


async def test_parse_todo_strips_surrounding_whitespace(
    client: AsyncClient, agent: FakeAgent
) -> None:
    await client.post(PARSE, json={"text": "  call the dentist \n", "today": TODAY})
    assert agent.last_body["text"] == "call the dentist"


async def test_parse_todo_truncates_an_oversized_note(
    client: AsyncClient, agent: FakeAgent
) -> None:
    """ai-agent 422s past 4000 chars, and a 422 there is a backend bug (A5)."""
    await client.post(PARSE, json={"text": "x" * 9000, "today": TODAY})

    assert len(agent.last_body["text"]) == 4000


async def test_parse_todo_caps_known_tags_at_fifty(
    app, client: AsyncClient, agent: FakeAgent, db_session, user: User
) -> None:
    from app.repositories.tags import SqlAlchemyTagRepository

    await SqlAlchemyTagRepository(db_session).get_or_create_many(
        user.id, [f"tag{index:03d}" for index in range(60)]
    )
    await db_session.commit()

    await client.post(PARSE, json={"text": "something", "today": TODAY})
    assert len(agent.last_body["known_tags"]) == 50


async def test_parse_todo_sanitises_what_the_agent_returns(
    app, client: AsyncClient
) -> None:
    """ai-agent clamps too, but a draft must be submittable whatever it says."""
    install_agent(
        app,
        FakeAgent(
            responder(
                **{
                    "/ai/parse-todo": {
                        "title": "  A  " + "very " * 100 + "long title  ",
                        "description": "  ",
                        "priority": "URGENT",
                        "due_date": "next tuesday",
                        "tags": ["Home", "home", "bad!", "  work  "],
                        "subtasks": [{"title": ""}, {"title": "Real step"}],
                    }
                }
            )
        ),
    )

    draft = (
        await client.post(PARSE, json={"text": "whatever", "today": TODAY})
    ).json()["draft"]

    assert len(draft["title"]) <= 200
    assert draft["description"] is None
    assert draft["priority"] == "medium"
    assert draft["due_date"] is None
    assert draft["tags"] == ["home", "work"]
    assert draft["subtasks"] == [{"title": "Real step"}]


async def test_a_draft_is_accepted_by_the_ordinary_create_endpoint(
    client: AsyncClient, agent: FakeAgent
) -> None:
    """The point of D-AI1: the user confirms, and the normal API takes it."""
    draft = (
        await client.post(PARSE, json={"text": "call the dentist", "today": TODAY})
    ).json()["draft"]

    created = await client.post(
        "/api/todos",
        json={
            "title": draft["title"],
            "description": draft["description"],
            "priority": draft["priority"],
            "due_date": draft["due_date"],
            "tags": draft["tags"],
        },
    )
    assert created.status_code == 201


# --- suggest-subtasks ------------------------------------------------------


async def test_suggest_subtasks_forwards_the_todo(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    response = await client.post(
        SUBTASKS, json={"todo_id": todo["id"], "max_items": 5}
    )

    assert response.status_code == 200
    assert response.json() == {
        "subtasks": [{"title": "Step one"}, {"title": "Step two"}]
    }
    assert agent.last_body == {
        "title": "Plan the offsite",
        "description": "Two days",
        "max_items": 5,
    }


async def test_suggest_subtasks_clamps_max_items(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    await client.post(SUBTASKS, json={"todo_id": todo["id"], "max_items": 99})
    assert agent.last_body["max_items"] == 10

    await client.post(SUBTASKS, json={"todo_id": todo["id"], "max_items": 0})
    assert agent.last_body["max_items"] == 1


async def test_suggest_subtasks_defaults_to_five(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    await client.post(SUBTASKS, json={"todo_id": todo["id"]})
    assert agent.last_body["max_items"] == 5


async def test_suggest_subtasks_caps_the_returned_list(
    app, client: AsyncClient, todo: dict
) -> None:
    install_agent(
        app,
        FakeAgent(
            responder(
                **{
                    "/ai/suggest-subtasks": {
                        "subtasks": [{"title": f"Step {n}"} for n in range(12)]
                    }
                }
            )
        ),
    )

    response = await client.post(
        SUBTASKS, json={"todo_id": todo["id"], "max_items": 3}
    )
    assert len(response.json()["subtasks"]) == 3


async def test_suggest_subtasks_404s_on_an_unknown_todo(
    client: AsyncClient, agent: FakeAgent
) -> None:
    response = await client.post(SUBTASKS, json={"todo_id": str(uuid4())})
    assert response.status_code == 404
    assert response.json() == {"detail": "Todo not found", "code": "todo_not_found"}
    assert agent.calls == 0  # never asked the model about a todo we do not have


async def test_suggest_subtasks_404s_on_a_malformed_id(
    client: AsyncClient, agent: FakeAgent
) -> None:
    """D1: a bad id is 404, not 422 — it must not be a probe for valid ids."""
    response = await client.post(SUBTASKS, json={"todo_id": "not-a-uuid"})
    assert response.status_code == 404
    assert response.json()["code"] == "todo_not_found"


async def test_suggest_subtasks_404s_on_another_users_todo(
    app,
    client: AsyncClient,
    agent: FakeAgent,
    other_user: User,
    test_settings: Settings,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers=bearer_headers(other_user, test_settings),
    ) as other:
        theirs = (await other.post("/api/todos", json={"title": "Theirs"})).json()

    response = await client.post(SUBTASKS, json={"todo_id": theirs["id"]})
    assert response.status_code == 404
    assert agent.calls == 0


# --- suggest-metadata ------------------------------------------------------


async def test_suggest_metadata_returns_priority_and_tags(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    response = await client.post(METADATA, json={"todo_id": todo["id"]})

    assert response.status_code == 200
    assert response.json() == {"priority": "high", "tags": ["work", "urgent"]}
    assert agent.last_body["title"] == "Plan the offsite"
    assert agent.last_body["known_tags"] == ["work"]
    assert "today" in agent.last_body


async def test_suggest_metadata_404s_on_a_foreign_todo(
    client: AsyncClient, agent: FakeAgent
) -> None:
    assert (
        await client.post(METADATA, json={"todo_id": str(uuid4())})
    ).status_code == 404


# --- edit-todo (iteration 5 §2.1) ------------------------------------------
#
# The proxy's job here is *resolution*: ai-agent answers with a positional,
# sanitized proposal and this endpoint turns it into a change set the user can
# read and the ordinary endpoints will accept. Every rule below is a way the
# upstream answer can be wrong — indices that point nowhere, values that are
# already the current ones, a tag suggestion that would push the todo over the
# cap — and the point of each test is that the wrong version never reaches a
# checkbox.


@pytest.fixture
async def todo_with_steps(client: AsyncClient, todo: dict) -> dict:
    """The ``todo`` fixture plus two subtasks, the second already completed."""
    steps = []
    for title in ("Book it", "Pay the invoice"):
        created = await client.post(
            f"/api/todos/{todo['id']}/subtasks", json={"title": title}
        )
        assert created.status_code == 201
        steps.append(created.json())
    await client.patch(f"/api/todos/{steps[1]['id']}", json={"completed": True})
    return {**todo, "steps": steps}


async def test_edit_todo_returns_a_resolved_change_set(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    """The whole §2.1 shape, including the ``from`` wire key (risk R7)."""
    body = await ask_edit(client, todo["id"], "rename it and tag it health")

    assert body == {
        "todo_id": todo["id"],
        "empty": False,
        "context": {"subtasks_truncated": False},
        "change_set": {
            "title": {"from": "Plan the offsite", "to": "Call the dentist"},
            "description": None,
            "priority": {"from": "medium", "to": "high"},
            "due_date": None,
            "completed": None,
            "tags": {
                "from": ["work"],
                "to": ["work", "health"],
                "added": ["health"],
                "removed": [],
            },
            "subtasks": [
                {
                    "action": "add",
                    "id": None,
                    "title": "Find the phone number",
                    "from_title": None,
                }
            ],
        },
    }


async def test_edit_todo_sends_a_positional_snapshot_without_identifiers(
    client: AsyncClient, agent: FakeAgent, todo_with_steps: dict
) -> None:
    """D-IT5-3: the model works in positions because it never sees an id."""
    await ask_edit(client, todo_with_steps["id"], "add a step")

    sent = agent.last_body
    assert set(sent) == {"instruction", "today", "known_tags", "todo"}
    assert sent["instruction"] == "add a step"
    assert sent["today"] == TODAY
    assert sent["known_tags"] == ["work"]
    assert sent["todo"] == {
        "title": "Plan the offsite",
        "description": "Two days",
        "priority": "medium",
        "due_date": None,
        "completed": False,
        "tags": ["work"],
        "subtasks": [
            {"title": "Book it", "completed": False},
            {"title": "Pay the invoice", "completed": True},
        ],
    }

    import json

    payload = json.dumps(sent)
    for step in todo_with_steps["steps"]:
        assert step["id"] not in payload
    assert todo_with_steps["id"] not in payload


async def test_edit_todo_reports_an_empty_change_set(
    app, client: AsyncClient, todo: dict
) -> None:
    """The correct answer to an out-of-scope instruction (§1.5)."""
    install_proposal(app)

    body = await ask_edit(client, todo["id"], "delete this todo")

    assert body["empty"] is True
    assert body["change_set"] == {
        "title": None,
        "description": None,
        "priority": None,
        "due_date": None,
        "completed": None,
        "tags": None,
        "subtasks": [],
    }


# -- no-op suppression (criterion 6, risk R1) --------------------------------


async def test_a_restated_title_is_not_a_change(
    app, client: AsyncClient, todo: dict
) -> None:
    install_proposal(app, title="Plan the offsite")

    body = await ask_edit(client, todo["id"])

    assert body["change_set"]["title"] is None
    assert body["empty"] is True


async def test_a_title_is_clamped_before_it_is_compared(
    app, client: AsyncClient, todo: dict
) -> None:
    """Whitespace noise around an unchanged title is still not a change."""
    install_proposal(app, title="  Plan   the offsite \n")

    assert (await ask_edit(client, todo["id"]))["empty"] is True


async def test_an_over_long_title_is_truncated_to_the_column(
    app, client: AsyncClient, todo: dict
) -> None:
    install_proposal(app, title="x" * 500)

    change = (await ask_edit(client, todo["id"]))["change_set"]["title"]
    assert len(change["to"]) == 200


async def test_a_new_description_is_a_change(
    app, client: AsyncClient, todo: dict
) -> None:
    install_proposal(app, description="Ask about the crown")

    assert (await ask_edit(client, todo["id"]))["change_set"]["description"] == {
        "from": "Two days",
        "to": "Ask about the crown",
    }


async def test_clearing_the_description_is_a_change_with_a_null_to(
    app, client: AsyncClient, todo: dict
) -> None:
    """``{"from": "…", "to": null}`` unambiguously means *clear it* (§2.1)."""
    install_proposal(app, clear_description=True)

    assert (await ask_edit(client, todo["id"]))["change_set"]["description"] == {
        "from": "Two days",
        "to": None,
    }


async def test_clearing_a_description_the_todo_does_not_have_is_dropped(
    app, client: AsyncClient
) -> None:
    install_proposal(app, clear_description=True)
    plain = (await client.post("/api/todos", json={"title": "No prose"})).json()

    body = await ask_edit(client, plain["id"])
    assert body["change_set"]["description"] is None
    assert body["empty"] is True


async def test_an_explicit_description_outranks_a_contradictory_clear(
    app, client: AsyncClient, todo: dict
) -> None:
    """A model asking for both is contradicting itself; keep the text."""
    install_proposal(app, description="Keep this", clear_description=True)

    assert (await ask_edit(client, todo["id"]))["change_set"]["description"]["to"] == (
        "Keep this"
    )


async def test_an_unknown_priority_is_no_change_not_medium(
    app, client: AsyncClient
) -> None:
    """The trap this endpoint exists to avoid: ``_clean_priority`` would coerce
    ``URGENT`` to ``medium`` and invent a priority change out of garbage."""
    install_proposal(app, priority="URGENT")
    high = (
        await client.post("/api/todos", json={"title": "Loud", "priority": "high"})
    ).json()

    body = await ask_edit(client, high["id"])
    assert body["change_set"]["priority"] is None
    assert body["empty"] is True


async def test_a_restated_priority_is_not_a_change(
    app, client: AsyncClient, todo: dict
) -> None:
    install_proposal(app, priority="medium")

    assert (await ask_edit(client, todo["id"]))["change_set"]["priority"] is None


async def test_a_due_date_is_resolved_and_cleared(
    app, client: AsyncClient
) -> None:
    dated = (
        await client.post(
            "/api/todos", json={"title": "Dated", "due_date": "2026-09-11"}
        )
    ).json()

    install_proposal(app, clear_due_date=True)
    assert (await ask_edit(client, dated["id"]))["change_set"]["due_date"] == {
        "from": "2026-09-11",
        "to": None,
    }

    install_proposal(app, due_date="2026-09-12")
    assert (await ask_edit(client, dated["id"]))["change_set"]["due_date"] == {
        "from": "2026-09-11",
        "to": "2026-09-12",
    }


@pytest.mark.parametrize(
    ("due_date", "expected"),
    [
        # ``TODAY`` is 2026-09-02 and the horizon is 5 * 366 days either side
        # of it, so these four are the last date in and the first date out on
        # each end.
        ("2031-09-06", "2031-09-06"),
        ("2031-09-07", None),
        ("2021-08-29", "2021-08-29"),
        ("2021-08-28", None),
        ("9999-12-31", None),
        ("0001-01-01", None),
    ],
)
async def test_an_absurdly_distant_due_date_is_no_change(
    app, client: AsyncClient, todo: dict, due_date: str, expected: str | None
) -> None:
    """The same ±5-year window ai-agent applies, re-applied here.

    ``9999-12-31`` is not a date a user asked for — it is "next year" resolved
    badly — and it would arrive as a pre-checked box that quietly rewrites a
    real deadline. ai-agent bounding its own output is not a reason for this
    service to forward whatever it gets.
    """
    install_proposal(app, due_date=due_date)

    change = (await ask_edit(client, todo["id"]))["change_set"]["due_date"]

    assert (change["to"] if change else None) == expected


async def test_an_unparseable_due_date_is_no_change(
    app, client: AsyncClient, todo: dict
) -> None:
    install_proposal(app, due_date="next tuesday")

    body = await ask_edit(client, todo["id"])
    assert body["change_set"]["due_date"] is None
    assert body["empty"] is True


async def test_completed_is_only_a_change_when_it_differs(
    app, client: AsyncClient, todo: dict
) -> None:
    install_proposal(app, completed=False)
    assert (await ask_edit(client, todo["id"]))["change_set"]["completed"] is None

    install_proposal(app, completed=True)
    assert (await ask_edit(client, todo["id"]))["change_set"]["completed"] == {
        "from": False,
        "to": True,
    }


@pytest.mark.parametrize("value", ["true", 1, "yes"])
async def test_only_a_real_boolean_completes_a_todo(
    app, client: AsyncClient, todo: dict, value: Any
) -> None:
    """A truthy string is not a user asking to close their todo."""
    install_proposal(app, completed=value)

    assert (await ask_edit(client, todo["id"]))["change_set"]["completed"] is None


# -- tags --------------------------------------------------------------------


async def test_a_tag_the_todo_already_has_is_not_a_change(
    app, client: AsyncClient, todo: dict
) -> None:
    install_proposal(app, tags_add=["Work"])

    body = await ask_edit(client, todo["id"])
    assert body["change_set"]["tags"] is None
    assert body["empty"] is True


async def test_removing_a_tag_produces_the_final_list_and_the_difference(
    app, client: AsyncClient
) -> None:
    tagged = (
        await client.post(
            "/api/todos", json={"title": "Tagged", "tags": ["home", "work"]}
        )
    ).json()
    install_proposal(app, tags_add=["health"], tags_remove=["work"])

    assert (await ask_edit(client, tagged["id"]))["change_set"]["tags"] == {
        "from": ["home", "work"],
        "to": ["home", "health"],
        "added": ["health"],
        "removed": ["work"],
    }


async def test_a_tag_named_in_both_add_and_remove_is_dropped_from_both(
    app, client: AsyncClient, todo: dict
) -> None:
    install_proposal(app, tags_add=["work"], tags_remove=["work"])

    assert (await ask_edit(client, todo["id"]))["change_set"]["tags"] is None


async def test_an_invalid_tag_name_is_dropped_not_repaired(
    app, client: AsyncClient, todo: dict
) -> None:
    install_proposal(app, tags_add=["bad!", "  Health  "])

    assert (await ask_edit(client, todo["id"]))["change_set"]["tags"]["added"] == [
        "health"
    ]


async def test_the_tag_cap_drops_the_suggestions_never_the_users_own(
    app, client: AsyncClient
) -> None:
    """Risk R6: an over-cap suggestion must not silently delete a tag the user
    put there themselves."""
    existing = [f"own{index}" for index in range(9)]
    full = (
        await client.post("/api/todos", json={"title": "Busy", "tags": existing})
    ).json()
    install_proposal(app, tags_add=["extra1", "extra2", "extra3"])

    change = (await ask_edit(client, full["id"]))["change_set"]["tags"]

    assert len(change["to"]) == 10
    assert set(existing) <= set(change["to"])
    assert change["removed"] == []
    assert change["added"] == ["extra1"]


# -- subtasks (D-IT5-3, risk R3) ---------------------------------------------


async def test_a_subtask_index_becomes_the_real_id(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    first, second = todo_with_steps["steps"]
    install_proposal(
        app,
        subtasks=[
            {"action": "rename", "index": 1, "title": "Book the appointment"},
            {"action": "remove", "index": 2, "title": None},
        ],
    )

    ops = (await ask_edit(client, todo_with_steps["id"]))["change_set"]["subtasks"]

    assert ops == [
        {
            "action": "rename",
            "id": first["id"],
            "title": "Book the appointment",
            "from_title": "Book it",
        },
        {
            "action": "remove",
            "id": second["id"],
            "title": None,
            "from_title": "Pay the invoice",
        },
    ]


@pytest.mark.parametrize("index", [0, 3, -1, "two", None, True, 1.0])
async def test_an_index_that_names_nothing_is_dropped(
    app, client: AsyncClient, todo_with_steps: dict, index: Any
) -> None:
    """Second half of the defence in depth: ai-agent bounds the index against
    the snapshot it received, and this bounds it against the real list."""
    install_proposal(app, subtasks=[{"action": "remove", "index": index, "title": None}])

    body = await ask_edit(client, todo_with_steps["id"])
    assert body["change_set"]["subtasks"] == []
    assert body["empty"] is True


async def test_an_unknown_action_is_dropped(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    install_proposal(
        app,
        subtasks=[
            {"action": "delete_todo", "index": 1, "title": None},
            {"action": "remove", "index": 1, "title": None},
        ],
    )

    ops = (await ask_edit(client, todo_with_steps["id"]))["change_set"]["subtasks"]
    assert [op["action"] for op in ops] == ["remove"]


async def test_an_add_without_a_title_is_dropped_and_carries_no_index(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    install_proposal(
        app,
        subtasks=[
            {"action": "add", "index": None, "title": "   "},
            {"action": "add", "index": 2, "title": "Find the phone number"},
        ],
    )

    ops = (await ask_edit(client, todo_with_steps["id"]))["change_set"]["subtasks"]
    assert ops == [
        {
            "action": "add",
            "id": None,
            "title": "Find the phone number",
            "from_title": None,
        }
    ]


async def test_a_rename_to_the_current_title_is_dropped(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    install_proposal(
        app, subtasks=[{"action": "rename", "index": 1, "title": "Book it"}]
    )

    assert (await ask_edit(client, todo_with_steps["id"]))["empty"] is True


async def test_a_rename_without_a_title_is_dropped(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    install_proposal(app, subtasks=[{"action": "rename", "index": 1, "title": None}])

    assert (await ask_edit(client, todo_with_steps["id"]))["empty"] is True


async def test_completing_an_already_completed_subtask_is_dropped(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    install_proposal(
        app,
        subtasks=[
            {"action": "complete", "index": 2, "title": None},
            {"action": "reopen", "index": 1, "title": None},
        ],
    )

    assert (await ask_edit(client, todo_with_steps["id"]))["empty"] is True


async def test_completing_an_active_subtask_survives_and_forgets_the_title(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    first = todo_with_steps["steps"][0]
    install_proposal(
        app,
        subtasks=[{"action": "complete", "index": 1, "title": "ignored"}],
    )

    ops = (await ask_edit(client, todo_with_steps["id"]))["change_set"]["subtasks"]
    assert ops == [
        {
            "action": "complete",
            "id": first["id"],
            "title": None,
            "from_title": "Book it",
        }
    ]


async def test_reopening_a_completed_subtask_survives_with_its_real_id(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    """The other half of the toggle: dropping a no-op ``reopen`` is only right
    if a genuine one still comes through, pointed at the right row."""
    second = todo_with_steps["steps"][1]
    install_proposal(
        app, subtasks=[{"action": "reopen", "index": 2, "title": "ignored"}]
    )

    body = await ask_edit(client, todo_with_steps["id"])

    assert body["empty"] is False
    assert body["change_set"]["subtasks"] == [
        {
            "action": "reopen",
            "id": second["id"],
            "title": None,
            "from_title": "Pay the invoice",
        }
    ]


async def test_both_toggles_survive_together_on_the_right_subtasks(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    """One active subtask completed and one completed subtask reopened, in one
    change set: the two ops must not be mapped to the same row."""
    first, second = todo_with_steps["steps"]
    install_proposal(
        app,
        subtasks=[
            {"action": "complete", "index": 1, "title": None},
            {"action": "reopen", "index": 2, "title": None},
        ],
    )

    ops = (await ask_edit(client, todo_with_steps["id"]))["change_set"]["subtasks"]

    assert [(op["action"], op["id"], op["from_title"]) for op in ops] == [
        ("complete", first["id"], "Book it"),
        ("reopen", second["id"], "Pay the invoice"),
    ]


async def test_a_removed_subtask_is_targeted_by_nothing_else(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    """One operation per id, with ``remove`` winning: renaming a row that is
    about to be deleted is wasted work at best and a 404 mid-apply at worst."""
    first = todo_with_steps["steps"][0]
    install_proposal(
        app,
        subtasks=[
            {"action": "rename", "index": 1, "title": "Book the appointment"},
            {"action": "remove", "index": 1, "title": None},
            {"action": "remove", "index": 1, "title": None},
        ],
    )

    ops = (await ask_edit(client, todo_with_steps["id"]))["change_set"]["subtasks"]
    assert ops == [
        {"action": "remove", "id": first["id"], "title": None, "from_title": "Book it"}
    ]


async def test_subtask_operations_are_capped_at_ten(
    app, client: AsyncClient, todo: dict
) -> None:
    install_proposal(
        app,
        subtasks=[
            {"action": "add", "index": None, "title": f"Step {n}"} for n in range(14)
        ],
    )

    ops = (await ask_edit(client, todo["id"]))["change_set"]["subtasks"]
    assert len(ops) == 10
    assert ops[0]["title"] == "Step 0"


async def test_subtask_operations_come_back_in_apply_order(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    """rename → complete/reopen → remove → add (§2.1)."""
    install_proposal(
        app,
        subtasks=[
            {"action": "add", "index": None, "title": "A new step"},
            {"action": "remove", "index": 2, "title": None},
            {"action": "complete", "index": 1, "title": None},
        ],
    )

    ops = (await ask_edit(client, todo_with_steps["id"]))["change_set"]["subtasks"]
    assert [op["action"] for op in ops] == ["complete", "remove", "add"]


async def test_a_long_subtask_list_is_truncated_and_says_so(
    app, client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    for index in range(21):
        created = await client.post(
            f"/api/todos/{todo['id']}/subtasks", json={"title": f"Step {index:02d}"}
        )
        assert created.status_code == 201

    body = await ask_edit(client, todo["id"])

    assert len(agent.last_body["todo"]["subtasks"]) == 20
    assert agent.last_body["todo"]["subtasks"][0]["title"] == "Step 00"
    assert body["context"]["subtasks_truncated"] is True


async def test_a_short_subtask_list_is_not_reported_as_truncated(
    client: AsyncClient, agent: FakeAgent, todo_with_steps: dict
) -> None:
    body = await ask_edit(client, todo_with_steps["id"])
    assert body["context"]["subtasks_truncated"] is False


# -- ownership and shape errors ---------------------------------------------


async def test_edit_todo_404s_on_an_unknown_todo(
    client: AsyncClient, agent: FakeAgent
) -> None:
    response = await client.post(
        EDIT, json={"todo_id": str(uuid4()), "instruction": "hi", "today": TODAY}
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Todo not found", "code": "todo_not_found"}
    assert agent.calls == 0


async def test_edit_todo_404s_on_a_malformed_id(
    client: AsyncClient, agent: FakeAgent
) -> None:
    response = await client.post(
        EDIT, json={"todo_id": "not-a-uuid", "instruction": "hi", "today": TODAY}
    )

    assert response.status_code == 404
    assert response.json()["code"] == "todo_not_found"
    assert agent.calls == 0


async def test_edit_todo_404s_on_another_users_todo(
    app,
    client: AsyncClient,
    agent: FakeAgent,
    other_user: User,
    test_settings: Settings,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers=bearer_headers(other_user, test_settings),
    ) as other:
        theirs = (await other.post("/api/todos", json={"title": "Theirs"})).json()

    response = await client.post(
        EDIT, json={"todo_id": theirs["id"], "instruction": "hi", "today": TODAY}
    )
    assert response.status_code == 404
    assert agent.calls == 0


async def test_edit_todo_404s_on_a_subtask_id(
    client: AsyncClient, agent: FakeAgent, todo_with_steps: dict
) -> None:
    """Decision D-IT5-6: this endpoint edits top-level todos, and D-E2 already
    collapses "not a valid target for you" into 404 — a distinct code would
    confirm that the id exists."""
    response = await client.post(
        EDIT,
        json={
            "todo_id": todo_with_steps["steps"][0]["id"],
            "instruction": "rename it",
            "today": TODAY,
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Todo not found", "code": "todo_not_found"}
    assert agent.calls == 0


@pytest.mark.parametrize("instruction", ["", "   ", "\n\t  \n"])
async def test_a_whitespace_only_instruction_is_422(
    client: AsyncClient, agent: FakeAgent, todo: dict, instruction: str
) -> None:
    response = await client.post(
        EDIT,
        json={"todo_id": todo["id"], "instruction": instruction, "today": TODAY},
    )

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [
        ["body", "instruction"]
    ]
    assert agent.calls == 0


async def test_an_over_long_instruction_is_truncated_not_rejected(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    """D-IT5-9: ai-agent 422s past 500 characters, and a 422 there is a backend
    bug — not something a user can act on."""
    response = await client.post(
        EDIT,
        json={"todo_id": todo["id"], "instruction": "x" * 501, "today": TODAY},
    )

    assert response.status_code == 200
    assert len(agent.last_body["instruction"]) == 500


async def test_control_characters_are_stripped_from_an_instruction(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    """A NUL, an ANSI escape or a bidi override is never an edit request — it
    is an attempt to make the prompt render differently from what it says."""
    response = await client.post(
        EDIT,
        json={
            "todo_id": todo["id"],
            "instruction": "rename\x00 it\x1b[31m to‮ Call the dentist",
            "today": TODAY,
        },
    )

    assert response.status_code == 200
    assert agent.last_body["instruction"] == "rename it[31m to Call the dentist"


async def test_a_multi_line_instruction_keeps_its_newlines(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    """A pasted two-line instruction is ordinary input, not an attack."""
    await client.post(
        EDIT,
        json={
            "todo_id": todo["id"],
            "instruction": "rename it\n\tand tag it work",
            "today": TODAY,
        },
    )

    assert agent.last_body["instruction"] == "rename it\n\tand tag it work"


async def test_an_instruction_of_only_control_characters_is_422(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    """Stripping happens before ``min_length``, so this is the 422 it always
    was rather than a forwarded empty string and a misleading 503."""
    response = await client.post(
        EDIT,
        json={"todo_id": todo["id"], "instruction": "\x00\x07 ​", "today": TODAY},
    )

    assert response.status_code == 422
    assert [error["loc"] for error in response.json()["detail"]] == [
        ["body", "instruction"]
    ]
    assert agent.calls == 0


async def test_control_characters_are_removed_before_the_length_is_measured(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    """Otherwise 500 real characters could still arrive as 500 *bytes* of
    escape sequences plus a truncated instruction."""
    await client.post(
        EDIT,
        json={
            "todo_id": todo["id"],
            "instruction": "\x00" * 200 + "x" * 500,
            "today": TODAY,
        },
    )

    assert agent.last_body["instruction"] == "x" * 500


async def test_edit_todo_requires_today(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    response = await client.post(
        EDIT, json={"todo_id": todo["id"], "instruction": "rename it"}
    )
    assert response.status_code == 422


async def test_edit_todo_rejects_an_unknown_key(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    response = await client.post(
        EDIT,
        json={
            "todo_id": todo["id"],
            "instruction": "rename it",
            "today": TODAY,
            "tags": ["work"],
        },
    )
    assert response.status_code == 422


async def test_edit_todo_survives_a_garbage_upstream_body(
    app, client: AsyncClient, todo_with_steps: dict
) -> None:
    """Nothing in a proposal is trusted, including its types.

    Run against a todo that *does* have subtasks, so the operation branch is
    actually reachable and a malformed op is dropped on its merits rather than
    because there was nothing to point at.
    """
    install_agent(
        app,
        FakeAgent(
            responder(
                **{
                    "/ai/edit-todo": {
                        "title": 42,
                        "description": ["nope"],
                        "clear_description": "yes",
                        "priority": {"value": "high"},
                        "due_date": 7,
                        "clear_due_date": "true",
                        "completed": "done",
                        "tags_add": "work",
                        "tags_remove": None,
                        "subtasks": [
                            "remove the second one",
                            None,
                            3,
                            # Unhashable values: a membership test on one of
                            # these raises TypeError, which would be a 500 out
                            # of the endpoint whose whole point is that a wrong
                            # upstream answer degrades to "nothing to change".
                            {"action": ["add"], "index": 1, "title": "x"},
                            {"action": {"remove": True}, "index": 1, "title": None},
                            {"action": "remove", "index": [1], "title": None},
                            {"action": "rename", "index": {"n": 1}, "title": "x"},
                        ],
                    }
                }
            )
        ),
    )

    body = await ask_edit(client, todo_with_steps["id"])
    assert body["empty"] is True


async def test_the_internal_token_never_reaches_an_edit_caller(
    client: AsyncClient, agent: FakeAgent, todo: dict, caplog
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = await client.post(
            EDIT,
            json={"todo_id": todo["id"], "instruction": "rename it", "today": TODAY},
        )

    assert response.status_code == 200
    assert TEST_AI_AGENT_TOKEN not in response.text
    assert TEST_AI_AGENT_TOKEN not in caplog.text
    assert INTERNAL_TOKEN_HEADER.lower() not in {
        key.lower() for key in response.headers
    }


async def test_edit_todo_writes_nothing_even_when_it_fails(
    app, client: AsyncClient, db_session: AsyncSession, todo_with_steps: dict
) -> None:
    """D-AI1 across the failure paths as well as the happy one (criterion 3)."""
    before = await snapshot(db_session)

    install_proposal(
        app,
        title="Call the dentist",
        tags_add=["health"],
        subtasks=[
            {"action": "add", "index": None, "title": "Find the number"},
            {"action": "remove", "index": 1, "title": None},
        ],
    )
    assert (await ask_edit(client, todo_with_steps["id"]))["empty"] is False

    for status in (404, 422):
        bad = await client.post(
            EDIT,
            json={"todo_id": str(uuid4()), "instruction": "hi", "today": TODAY}
            if status == 404
            else {"todo_id": todo_with_steps["id"], "instruction": " ", "today": TODAY},
        )
        assert bad.status_code == status

    def hang(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow")

    install_agent(app, FakeAgent(hang))
    timed_out = await client.post(
        EDIT,
        json={"todo_id": todo_with_steps["id"], "instruction": "hi", "today": TODAY},
    )
    assert timed_out.status_code == 504

    assert await snapshot(db_session) == before


async def test_a_suggested_tag_is_never_created_by_an_edit(
    client: AsyncClient, agent: FakeAgent, db_session: AsyncSession, todo: dict
) -> None:
    await ask_edit(client, todo["id"], "tag it health")

    names = (await db_session.execute(select(Tag.name))).scalars().all()
    assert "health" not in names


# --- daily-summary ---------------------------------------------------------


async def test_daily_summary_sends_the_active_top_level_todos(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    done = (await client.post("/api/todos", json={"title": "Already done"})).json()
    await client.patch(f"/api/todos/{done['id']}", json={"completed": True})
    await client.post(f"/api/todos/{todo['id']}/subtasks", json={"title": "A step"})

    response = await client.post(SUMMARY, json={"list_id": None, "today": TODAY})

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == "You have 2 open todos."
    assert body["todo_count"] == 1
    assert body["generated_at"]

    sent = agent.last_body
    assert sent["today"] == TODAY
    assert [row["title"] for row in sent["todos"]] == ["Plan the offsite"]
    assert sent["todos"][0]["list_name"] == "Inbox"
    assert sent["todos"][0]["completed"] is False


async def test_daily_summary_accepts_an_explicit_null_list_id(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    """The frontend always sends the key, with null for "all lists"."""
    assert (
        await client.post(SUMMARY, json={"list_id": None, "today": TODAY})
    ).status_code == 200


async def test_daily_summary_can_be_scoped_to_one_list(
    client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    work = (await client.post("/api/lists", json={"name": "Work"})).json()
    await client.post("/api/todos", json={"title": "Only this", "list_id": work["id"]})

    response = await client.post(SUMMARY, json={"list_id": work["id"], "today": TODAY})

    assert response.json()["todo_count"] == 1
    assert [row["title"] for row in agent.last_body["todos"]] == ["Only this"]
    assert agent.last_body["todos"][0]["list_name"] == "Work"


async def test_daily_summary_404s_on_a_foreign_list(
    client: AsyncClient, agent: FakeAgent
) -> None:
    response = await client.post(
        SUMMARY, json={"list_id": str(uuid4()), "today": TODAY}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "list_not_found"
    assert agent.calls == 0


async def test_daily_summary_404s_on_a_malformed_list_id(
    client: AsyncClient, agent: FakeAgent
) -> None:
    """D1/D-E2: a bad id reads the same as one that is not yours."""
    response = await client.post(SUMMARY, json={"list_id": "not-a-uuid", "today": TODAY})

    assert response.status_code == 404
    assert response.json() == {"detail": "List not found", "code": "list_not_found"}
    assert agent.calls == 0


async def test_daily_summary_404s_on_another_users_list(
    app,
    client: AsyncClient,
    agent: FakeAgent,
    other_user: User,
    test_settings: Settings,
) -> None:
    other_headers = bearer_headers(other_user, test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", headers=other_headers
    ) as other:
        theirs = (await other.post("/api/lists", json={"name": "Theirs"})).json()

    response = await client.post(
        SUMMARY, json={"list_id": theirs["id"], "today": TODAY}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "list_not_found"
    assert agent.calls == 0


async def test_daily_summary_orders_by_due_date_then_priority(
    client: AsyncClient, agent: FakeAgent
) -> None:
    await client.post(
        "/api/todos", json={"title": "No date, high", "priority": "high"}
    )
    await client.post(
        "/api/todos",
        json={"title": "Later", "due_date": "2026-09-10", "priority": "high"},
    )
    await client.post(
        "/api/todos",
        json={"title": "Today, low", "due_date": TODAY, "priority": "low"},
    )
    await client.post(
        "/api/todos",
        json={"title": "Today, high", "due_date": TODAY, "priority": "high"},
    )

    await client.post(SUMMARY, json={"list_id": None, "today": TODAY})

    assert [row["title"] for row in agent.last_body["todos"]] == [
        "Today, high",
        "Today, low",
        "Later",
        "No date, high",
    ]


async def test_daily_summary_caps_the_context_at_fifty(
    client: AsyncClient, agent: FakeAgent
) -> None:
    for index in range(55):
        await client.post("/api/todos", json={"title": f"Todo {index}"})

    response = await client.post(SUMMARY, json={"list_id": None, "today": TODAY})

    assert len(agent.last_body["todos"]) == 50
    assert response.json()["todo_count"] == 50


async def test_daily_summary_with_nothing_open_does_not_call_the_model(
    client: AsyncClient, agent: FakeAgent
) -> None:
    """No todos, no prompt: the frontend shows its empty copy off todo_count."""
    response = await client.post(SUMMARY, json={"list_id": None, "today": TODAY})

    assert response.status_code == 200
    assert response.json()["todo_count"] == 0
    assert response.json()["summary"] == ""
    assert agent.calls == 0


# --- failure mapping -------------------------------------------------------


@pytest.mark.parametrize("path", [PARSE, SUBTASKS, METADATA, SUMMARY, EDIT])
async def test_an_upstream_503_becomes_ai_unavailable(
    app, client: AsyncClient, todo: dict, path: str
) -> None:
    install_agent(
        app,
        FakeAgent(
            lambda _request: httpx.Response(
                503, json={"detail": "Ollama is unavailable", "code": "ai_unavailable"}
            )
        ),
    )

    response = await client.post(path, json=_body_for(path, todo))
    assert response.status_code == 503
    assert response.json() == {
        "detail": "AI service is unavailable",
        "code": "ai_unavailable",
    }


@pytest.mark.parametrize("path", [PARSE, SUBTASKS, METADATA, SUMMARY, EDIT])
async def test_an_upstream_timeout_becomes_ai_timeout(
    app, client: AsyncClient, todo: dict, path: str
) -> None:
    def hang(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("ai-agent took too long")

    install_agent(app, FakeAgent(hang))

    response = await client.post(path, json=_body_for(path, todo))
    assert response.status_code == 504
    assert response.json() == {"detail": "AI request timed out", "code": "ai_timeout"}


async def test_a_refused_connection_becomes_ai_unavailable(
    app, client: AsyncClient
) -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    install_agent(app, FakeAgent(refuse))

    response = await client.post(PARSE, json={"text": "hi", "today": TODAY})
    assert response.status_code == 503
    assert response.json()["code"] == "ai_unavailable"


async def test_a_non_json_upstream_body_becomes_ai_unavailable(
    app, client: AsyncClient
) -> None:
    install_agent(
        app, FakeAgent(lambda _r: httpx.Response(200, text="<html>oops</html>"))
    )

    response = await client.post(PARSE, json={"text": "hi", "today": TODAY})
    assert response.status_code == 503
    assert response.json()["code"] == "ai_unavailable"


async def test_an_upstream_error_body_is_never_echoed(
    app, client: AsyncClient
) -> None:
    """Prompt fragments and internal wording stay upstream."""
    install_agent(
        app,
        FakeAgent(
            lambda _r: httpx.Response(
                500,
                json={"detail": "Traceback: prompt was 'user secret note'"},
            )
        ),
    )

    response = await client.post(PARSE, json={"text": "hi", "today": TODAY})
    assert "user secret note" not in response.text
    assert "Traceback" not in response.text


def _body_for(path: str, todo: dict) -> dict:
    if path == PARSE:
        return {"text": "call the dentist", "today": TODAY}
    if path == SUMMARY:
        return {"list_id": None, "today": TODAY}
    if path == EDIT:
        return {
            "todo_id": todo["id"],
            "instruction": "rename it to Call the dentist",
            "today": TODAY,
        }
    return {"todo_id": todo["id"]}


# --- AI_ENABLED=false ------------------------------------------------------


@pytest.fixture
def ai_disabled_app(engine, db_session, test_settings: Settings):
    from app.db.session import create_sessionmaker
    from app.deps import get_session

    settings = test_settings.model_copy(update={"AI_ENABLED": False})
    application = create_app(settings)
    application.state.engine = engine
    application.state.sessionmaker = create_sessionmaker(engine)

    async def override_get_session():
        yield db_session

    application.dependency_overrides[get_session] = override_get_session
    install_agent(application, FakeAgent(responder(**{"/health": HEALTHY})))
    return application


@pytest.fixture
async def disabled_client(ai_disabled_app, auth_headers) -> AsyncClient:
    transport = ASGITransport(app=ai_disabled_app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", headers=auth_headers
    ) as c:
        yield c


async def test_status_reports_disabled(disabled_client: AsyncClient) -> None:
    response = await disabled_client.get(STATUS)
    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "available": False,
        "model": None,
        "reason": "disabled",
    }


@pytest.mark.parametrize("path", [PARSE, SUBTASKS, METADATA, SUMMARY, EDIT])
async def test_every_other_endpoint_is_disabled(
    disabled_client: AsyncClient, path: str
) -> None:
    response = await disabled_client.post(path, json=_body_for(path, {"id": str(uuid4())}))
    assert response.status_code == 503
    assert response.json() == {
        "detail": "AI features are disabled",
        "code": "ai_disabled",
    }


async def test_a_disabled_status_never_calls_the_agent(
    ai_disabled_app, disabled_client: AsyncClient
) -> None:
    await disabled_client.get(STATUS)
    # The client was replaced by a FakeAgent-backed one in the fixture; with AI
    # off, /status must answer from settings alone.
    assert ai_disabled_app.state.ai_client._client._transport.handler.calls == 0


# --- auth & rate limiting --------------------------------------------------


@pytest.mark.parametrize("path", [PARSE, SUBTASKS, METADATA, SUMMARY, EDIT])
async def test_the_ai_endpoints_require_authentication(
    anon_client: AsyncClient, path: str
) -> None:
    response = await anon_client.post(path, json=_body_for(path, {"id": str(uuid4())}))
    assert response.status_code == 401


@pytest.mark.parametrize("path", [PARSE, SUBTASKS, METADATA, SUMMARY, EDIT])
async def test_a_disabled_endpoint_still_answers_401_to_a_stranger(
    ai_disabled_app, path: str
) -> None:
    """Auth runs before the master switch (SEC-5).

    ``ai_disabled`` answers a question an anonymous caller is not entitled to
    ask — whether this deployment runs AI at all — and would make these the
    only routes in the API that tell an unauthenticated client something about
    the configuration.
    """
    transport = ASGITransport(app=ai_disabled_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as anon:
        response = await anon.post(path, json=_body_for(path, {"id": str(uuid4())}))

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_the_rate_limit_is_twenty_per_five_minutes(
    app, client: AsyncClient, agent: FakeAgent
) -> None:
    limiter = app.state.ai_rate_limiter
    assert (limiter.limit, limiter.window_seconds) == (20, 300)

    body = {"text": "hello", "today": TODAY}
    for index in range(limiter.limit):
        assert (await client.post(PARSE, json=body)).status_code == 200, index

    blocked = await client.post(PARSE, json=body)
    assert blocked.status_code == 429
    assert blocked.json() == {
        "detail": "Too many requests. Please wait and try again.",
        "code": "rate_limited",
    }
    assert int(blocked.headers["Retry-After"]) > 0
    # The refused call never reached the model.
    assert agent.calls == limiter.limit


async def test_the_rate_limit_is_per_user(
    app,
    client: AsyncClient,
    agent: FakeAgent,
    other_user: User,
    test_settings: Settings,
) -> None:
    # Minted before the 429: raising through get_session rolls the shared test
    # session back, which expires every entity loaded from it — including this
    # fixture's user, whose id would then need a query the session cannot run.
    other_headers = bearer_headers(other_user, test_settings)

    body = {"text": "hello", "today": TODAY}
    for _ in range(app.state.ai_rate_limiter.limit):
        await client.post(PARSE, json=body)
    assert (await client.post(PARSE, json=body)).status_code == 429

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://testserver", headers=other_headers
    ) as other:
        assert (await other.post(PARSE, json=body)).status_code == 200


async def test_edit_todo_shares_the_one_ai_budget(
    app, client: AsyncClient, agent: FakeAgent, todo: dict
) -> None:
    """Criterion 2: one 20-per-5-minutes budget per user, not one per endpoint.

    A per-endpoint budget would make the newest AI feature a way to buy 20 more
    model calls, which is the opposite of what a shared limiter is for.
    """
    limiter = app.state.ai_rate_limiter
    limiter.reset()

    for _ in range(limiter.limit - 1):
        assert (await client.post(PARSE, json={"text": "hi", "today": TODAY})).status_code == 200

    assert (await client.post(EDIT, json=_body_for(EDIT, todo))).status_code == 200

    blocked = await client.post(EDIT, json=_body_for(EDIT, todo))
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "rate_limited"
    assert int(blocked.headers["Retry-After"]) > 0


async def test_status_is_not_rate_limited(
    app, client: AsyncClient, agent: FakeAgent
) -> None:
    """A 429 on "is AI up?" would black out the UI's AI section for 5 minutes."""
    for _ in range(app.state.ai_rate_limiter.limit + 5):
        assert (await client.get(STATUS)).status_code == 200


async def test_a_disabled_endpoint_does_not_spend_rate_limit_budget(
    ai_disabled_app, disabled_client: AsyncClient
) -> None:
    for _ in range(30):
        response = await disabled_client.post(
            PARSE, json={"text": "hi", "today": TODAY}
        )
        assert response.json()["code"] == "ai_disabled"


# --- D-AI1: nothing here writes -------------------------------------------


async def test_no_ai_endpoint_writes_to_the_database(
    client: AsyncClient, agent: FakeAgent, db_session: AsyncSession, todo: dict
) -> None:
    """Master D-AI1, asserted rather than assumed."""
    before = await snapshot(db_session)

    assert (await client.get(STATUS)).status_code == 200
    assert (
        await client.post(PARSE, json={"text": "call the dentist", "today": TODAY})
    ).status_code == 200
    assert (
        await client.post(SUBTASKS, json={"todo_id": todo["id"], "max_items": 5})
    ).status_code == 200
    assert (
        await client.post(METADATA, json={"todo_id": todo["id"]})
    ).status_code == 200
    assert (
        await client.post(SUMMARY, json={"list_id": None, "today": TODAY})
    ).status_code == 200
    assert (await client.post(EDIT, json=_body_for(EDIT, todo))).status_code == 200

    assert await snapshot(db_session) == before


async def test_a_suggested_tag_is_not_created_as_a_tag(
    client: AsyncClient, agent: FakeAgent, db_session: AsyncSession
) -> None:
    """The suggestion names tags the user does not have; none may appear."""
    await client.post(PARSE, json={"text": "call the dentist", "today": TODAY})

    names = (
        (await db_session.execute(select(Tag.name))).scalars().all()
    )
    assert "health" not in names


async def test_an_ai_call_publishes_no_realtime_event(
    app, client: AsyncClient, agent: FakeAgent, todo: dict, user: User
) -> None:
    """Nothing changed, so no other tab may be told that something did."""
    from app.events import Event

    published: list[Event] = []
    original = type(app.state.broker).publish

    async def spy(self, user_id, event):
        published.append(event)
        return await original(self, user_id, event)

    app.state.broker.__class__.publish = spy
    try:
        await client.post(PARSE, json={"text": "hi", "today": TODAY})
        await client.post(SUBTASKS, json={"todo_id": todo["id"]})
        await client.post(METADATA, json={"todo_id": todo["id"]})
        await client.post(SUMMARY, json={"list_id": None, "today": TODAY})
        await client.post(EDIT, json=_body_for(EDIT, todo))
    finally:
        app.state.broker.__class__.publish = original

    assert published == []


# --- live (opt-in) ---------------------------------------------------------


@pytest.mark.skipif(
    os.getenv("AI_AGENT_LIVE") != "1",
    reason="requires a running ai-agent + Ollama (set AI_AGENT_LIVE=1)",
)
async def test_live_ai_agent_round_trip(app, client: AsyncClient) -> None:
    """All five endpoints through the proxy to a running ai-agent + Ollama.

    Asserts only the **contract** — shapes, types, bounds — never the model's
    wording, which is neither reproducible nor ours to specify. A test that
    pinned the prose would fail on a model upgrade for no reason.

    ``AI_AGENT_URL``/``AI_AGENT_TOKEN`` come from the environment; the proxy
    timeout is widened because a cold CPU-only model can take a while, and a
    flaky local run is worse than a slow one.
    """
    settings = app.state.settings.model_copy(
        update={
            "AI_AGENT_URL": os.environ.get("AI_AGENT_URL", "http://127.0.0.1:8022"),
            "AI_AGENT_TOKEN": os.environ["AI_AGENT_TOKEN"],
            "AI_TIMEOUT_SECONDS": 180,
        }
    )
    app.state.ai_client = AiAgentClient(settings)

    status = await client.get(STATUS)
    assert status.status_code == 200, status.text
    assert status.json() == {
        "enabled": True,
        "available": True,
        "model": status.json()["model"],
    }
    assert status.json()["model"]

    parsed = await asyncio.wait_for(
        client.post(
            PARSE,
            json={"text": "call the dentist tomorrow morning, urgent", "today": TODAY},
        ),
        timeout=240,
    )
    assert parsed.status_code == 200, parsed.text
    draft = parsed.json()["draft"]
    assert draft["title"].strip() != ""
    assert len(draft["title"]) <= 200
    assert draft["priority"] in {"low", "medium", "high"}
    assert isinstance(draft["subtasks"], list)

    todo = (
        await client.post(
            "/api/todos",
            json={"title": "Organise a team offsite", "tags": ["work"]},
        )
    ).json()

    subtasks = await asyncio.wait_for(
        client.post(SUBTASKS, json={"todo_id": todo["id"], "max_items": 4}),
        timeout=240,
    )
    assert subtasks.status_code == 200, subtasks.text
    assert len(subtasks.json()["subtasks"]) <= 4
    assert all(item["title"].strip() for item in subtasks.json()["subtasks"])

    metadata = await asyncio.wait_for(
        client.post(METADATA, json={"todo_id": todo["id"]}), timeout=240
    )
    assert metadata.status_code == 200, metadata.text
    assert metadata.json()["priority"] in {"low", "medium", "high"}
    assert len(metadata.json()["tags"]) <= 10

    summary = await asyncio.wait_for(
        client.post(SUMMARY, json={"list_id": None, "today": TODAY}), timeout=240
    )
    assert summary.status_code == 200, summary.text
    assert summary.json()["todo_count"] == 1
    assert summary.json()["summary"].strip() != ""

    # D-AI1 holds against the real service too: the only row in the database is
    # the one *this test* created through the ordinary endpoint.
    listed = (await client.get("/api/todos")).json()
    assert [row["title"] for row in listed] == ["Organise a team offsite"]
    assert listed[0]["subtasks"] == []
    assert listed[0]["tags"] == ["work"]
