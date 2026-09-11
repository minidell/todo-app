"""``GET /health`` — reports, never fails (slice-4 spec B4).

The backend's ``GET /api/ai/status`` is built on this endpoint, and that one
"must never fail" (master §6.6), so every Ollama failure mode below still has to
come back 200.
"""

from __future__ import annotations

from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from tests.conftest import TEST_MODEL, TEST_TOKEN, FakeOllama


def test_reports_ok_when_the_model_is_present(
    client: TestClient, fake_ollama: FakeOllama
) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "ollama": "ok",
        "model": TEST_MODEL,
        "model_present": True,
    }


def test_reports_the_model_as_absent_when_it_was_not_pulled(
    client: TestClient, fake_ollama: FakeOllama
) -> None:
    fake_ollama.tags_reply = httpx.Response(
        200, json={"models": [{"name": "some-other-model:7b"}]}
    )

    body = client.get("/health").json()

    assert body["ollama"] == "ok"
    assert body["model_present"] is False


def test_reports_unavailable_when_ollama_is_down(
    client: TestClient, fake_ollama: FakeOllama
) -> None:
    fake_ollama.tags_reply = httpx.ConnectError("connection refused")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "ollama": "unavailable",
        "model": TEST_MODEL,
        "model_present": False,
    }


def test_reports_unavailable_when_ollama_times_out(
    client: TestClient, fake_ollama: FakeOllama
) -> None:
    fake_ollama.tags_reply = httpx.ReadTimeout("too slow")

    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["ollama"] == "unavailable"


def test_reports_unavailable_on_a_server_error(
    client: TestClient, fake_ollama: FakeOllama
) -> None:
    fake_ollama.tags_reply = httpx.Response(500, text="boom")

    assert client.get("/health").json()["ollama"] == "unavailable"


def test_health_does_not_call_the_chat_endpoint(
    client: TestClient, fake_ollama: FakeOllama
) -> None:
    client.get("/health")

    assert fake_ollama.chat_calls == 0
    assert fake_ollama.tags_calls == 1


def test_untagged_model_names_match_any_pulled_tag(
    make_client: Callable[..., TestClient], fake_ollama: FakeOllama
) -> None:
    fake_ollama.tags_reply = httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]})
    client = make_client(OLLAMA_MODEL="qwen2.5")

    assert client.get("/health").json()["model_present"] is True


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
@pytest.mark.parametrize("app_env", ["prod", "", "Dev", "staging"])
def test_docs_are_disabled_outside_dev(
    make_client: Callable[..., TestClient], path: str, app_env: str
) -> None:
    client = make_client(APP_ENV=app_env)

    assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_docs_are_served_in_dev(
    make_client: Callable[..., TestClient], path: str
) -> None:
    client = make_client(APP_ENV="dev")

    assert client.get(path).status_code == 200


def test_app_env_defaults_to_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: docs stay hidden unless APP_ENV is explicitly ``dev``."""
    monkeypatch.delenv("APP_ENV", raising=False)

    settings = Settings(AI_AGENT_TOKEN=TEST_TOKEN, _env_file=None)  # type: ignore[call-arg]

    assert settings.APP_ENV == "prod"
    assert settings.docs_enabled is False
