"""Shared fixtures: a fake Ollama and a TestClient wired to it.

Ollama is **always** mocked here (master §2): ``uv run pytest`` must be green on
a clean checkout with no Docker and no model. The one live test lives in
``test_live_ollama.py`` and is skipped unless ``AI_AGENT_LIVE=1``.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Iterator

#: At least config.MIN_TOKEN_LENGTH characters, like a real one.
TEST_TOKEN = "test-internal-token-0123456789abcdef"

# ``app.main`` builds a module-level ``app`` (that is what uvicorn imports), and
# building it needs the shared secret. Pin a known test value before any app
# import: **unconditionally**, not via setdefault, so the suite is hermetic.
# With setdefault, `AI_AGENT_TOKEN=whatever uv run pytest` would leak the
# developer's own token into the tests. Individual tests that care about an
# absent or different token use monkeypatch.
os.environ["AI_AGENT_TOKEN"] = TEST_TOKEN

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402

TEST_MODEL = "test-model:1b"
AUTH_HEADERS: dict[str, str] = {"X-Internal-Token": TEST_TOKEN}


def chat_response(content: str, *, status_code: int = 200) -> httpx.Response:
    """An Ollama ``/api/chat`` envelope wrapping ``content``."""
    return httpx.Response(
        status_code,
        json={
            "model": TEST_MODEL,
            "message": {"role": "assistant", "content": content},
            "done": True,
        },
    )


def chat_json_response(payload: Any) -> httpx.Response:
    """An Ollama reply whose content is ``payload`` serialized as JSON."""
    return chat_response(json.dumps(payload))


class FakeClock:
    """A monotonic clock the tests drive by hand (see ``fake_ollama``)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeOllama:
    """A scripted Ollama: queue replies, then inspect what was sent.

    Queue entries are either an ``httpx.Response`` or an exception instance to
    raise (used for connection errors and timeouts). Running out of scripted
    replies is a test bug and fails loudly.

    Set ``seconds_per_call`` to make each ``/api/chat`` call appear to take that
    long, which is how the deadline tests exhaust the budget without sleeping.
    """

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.seconds_per_call = 0.0
        self.chat_replies: list[httpx.Response | Exception] = []
        self.chat_requests: list[httpx.Request] = []
        self.chat_payloads: list[dict[str, Any]] = []
        self.chat_read_timeouts: list[float | None] = []
        self.chat_connect_timeouts: list[float | None] = []
        self.tags_reply: httpx.Response | Exception = httpx.Response(
            200, json={"models": [{"name": TEST_MODEL}]}
        )
        self.tags_calls = 0

    # -- scripting ---------------------------------------------------------
    def queue(self, reply: httpx.Response | Exception) -> None:
        self.chat_replies.append(reply)

    def queue_content(self, content: str) -> None:
        """Queue a chat reply whose message content is the raw ``content``."""
        self.queue(chat_response(content))

    def queue_object(self, payload: Any) -> None:
        """Queue a chat reply whose message content is JSON ``payload``."""
        self.queue(chat_json_response(payload))

    # -- transport ---------------------------------------------------------
    @property
    def chat_calls(self) -> int:
        return len(self.chat_requests)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags":
            self.tags_calls += 1
            return _resolve(self.tags_reply)
        if path == "/api/chat":
            self.chat_requests.append(request)
            self.chat_payloads.append(json.loads(request.content))
            timeout = request.extensions.get("timeout", {})
            self.chat_read_timeouts.append(timeout.get("read"))
            self.chat_connect_timeouts.append(timeout.get("connect"))
            self.clock.advance(self.seconds_per_call)
            if not self.chat_replies:
                raise AssertionError(
                    f"fake Ollama received an unscripted /api/chat call "
                    f"(call #{len(self.chat_requests)})"
                )
            return _resolve(self.chat_replies.pop(0))
        raise AssertionError(f"fake Ollama received an unexpected path: {path}")


def _resolve(reply: httpx.Response | Exception) -> httpx.Response:
    if isinstance(reply, Exception):
        raise reply
    return reply


@pytest.fixture
def settings() -> Settings:
    return Settings(
        OLLAMA_BASE_URL="http://ollama.test:11434",
        OLLAMA_MODEL=TEST_MODEL,
        OLLAMA_TIMEOUT_SECONDS=5.0,
        AI_AGENT_TOKEN=TEST_TOKEN,
        LOG_LEVEL="warning",
        APP_ENV="dev",
    )


@pytest.fixture
def fake_ollama(monkeypatch: pytest.MonkeyPatch) -> FakeOllama:
    fake = FakeOllama()
    # Deadlines read app.ollama._now; point it at the scripted clock so tests
    # can burn the budget without sleeping.
    monkeypatch.setattr("app.ollama._now", fake.clock)
    return fake


@pytest.fixture
def make_client(
    settings: Settings, fake_ollama: FakeOllama
) -> Iterator[Callable[..., TestClient]]:
    """Factory for a ``TestClient`` bound to the fake Ollama."""
    from app.main import create_app

    clients: list[TestClient] = []

    def _make(**overrides: Any) -> TestClient:
        effective = settings.model_copy(update=overrides) if overrides else settings
        app = create_app(effective, transport=httpx.MockTransport(fake_ollama.handler))
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return client

    yield _make

    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()
