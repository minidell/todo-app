"""Shared-secret protection of ``/ai/*`` (slice-4 spec B5/B6.1)."""

from __future__ import annotations

import secrets
from typing import Callable

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import MIN_TOKEN_LENGTH, PLACEHOLDER_TOKENS, Settings
from tests.conftest import AUTH_HEADERS, TEST_TOKEN, FakeOllama

#: Every route behind ``X-Internal-Token``, as ``(method, path, body)``. The
#: auth matrix below is parametrised over this list, so adding a route to the
#: ``/ai`` router and to this list is all it takes to have it covered — no
#: per-route copy of the missing/wrong/empty-token assertions.
#: ``GET /ai/ping`` (iteration 4, IT3-2) sends no body.
ROUTES = [
    ("POST", "/ai/parse-todo", {"text": "call the dentist", "today": "2026-09-02"}),
    ("POST", "/ai/suggest-subtasks", {"title": "Plan the trip"}),
    ("POST", "/ai/suggest-metadata", {"title": "Plan the trip", "today": "2026-09-02"}),
    ("POST", "/ai/daily-summary", {"today": "2026-09-02", "todos": []}),
    (
        "POST",
        "/ai/edit-todo",
        {
            "instruction": "rename it to Call the dentist",
            "today": "2026-09-02",
            "todo": {"title": "Dentist"},
        },
    ),
    ("GET", "/ai/ping", None),
]


def _request(
    client: TestClient,
    method: str,
    path: str,
    body: dict | None,
    headers: dict[str, str] | None = None,
):
    """Issue one request from the ``ROUTES`` matrix."""
    return client.request(method, path, json=body, headers=headers)


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_missing_token_is_rejected(
    client: TestClient, method: str, path: str, body: dict | None, fake_ollama: FakeOllama
) -> None:
    response = _request(client, method, path, body)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid internal token", "code": "unauthorized"}
    assert fake_ollama.chat_calls == 0


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_wrong_token_is_rejected(
    client: TestClient, method: str, path: str, body: dict | None, fake_ollama: FakeOllama
) -> None:
    response = _request(
        client, method, path, body, {"X-Internal-Token": TEST_TOKEN + "-nope"}
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert fake_ollama.chat_calls == 0


@pytest.mark.parametrize(("method", "path", "body"), ROUTES)
def test_empty_token_header_is_rejected(
    client: TestClient, method: str, path: str, body: dict | None
) -> None:
    response = _request(client, method, path, body, {"X-Internal-Token": ""})

    assert response.status_code == 401


@pytest.mark.parametrize(
    "raw_token",
    [
        b"\xe9token",  # a lone latin-1 byte
        "café-token".encode("utf-8"),  # multi-byte UTF-8
        b"\xff\xfe\x00",
    ],
)
def test_a_non_ascii_token_is_rejected_not_a_500(
    client: TestClient, raw_token: bytes
) -> None:
    """Starlette decodes headers as latin-1; comparing str would raise TypeError.

    An unauthenticated caller must never be able to turn a 401 into a 500.
    """
    response = client.post(
        "/ai/daily-summary",
        json={"today": "2026-09-02", "todos": []},
        headers={b"X-Internal-Token": raw_token},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_health_needs_no_token(client: TestClient) -> None:
    assert client.get("/health").status_code == 200


class TestTheEnforcedTokenIsTheConfiguredOne:
    """The app must enforce the settings it was built with, not the environment.

    ``config.get_settings`` is an ``lru_cache``d read of ``os.environ``; if auth
    used it, an app constructed with an explicit token would happily accept the
    environment's token instead. That also broke the live lane whenever a
    different ``AI_AGENT_TOKEN`` was exported.
    """

    APP_TOKEN = "app-specific-token-0123456789abcdef"
    BODY = {"today": "2026-09-02", "todos": []}

    def test_the_app_token_is_accepted(
        self, make_client: Callable[..., TestClient], fake_ollama: FakeOllama
    ) -> None:
        client = make_client(AI_AGENT_TOKEN=self.APP_TOKEN)
        fake_ollama.queue_object({"summary": "All quiet."})

        response = client.post(
            "/ai/daily-summary",
            json=self.BODY,
            headers={"X-Internal-Token": self.APP_TOKEN},
        )

        assert response.status_code == 200

    def test_any_other_token_is_rejected(
        self, make_client: Callable[..., TestClient], settings: Settings
    ) -> None:
        """The ambient token must not be honoured by an app configured otherwise.

        ``settings.AI_AGENT_TOKEN`` is the value conftest also pins into the
        process environment, so sending it here is exactly the "auth fell back
        to the environment" case — asserted against the resolved settings, never
        against ``os.environ``, so an exported token cannot redden the suite.
        """
        assert settings.AI_AGENT_TOKEN != self.APP_TOKEN
        client = make_client(AI_AGENT_TOKEN=self.APP_TOKEN)

        response = client.post(
            "/ai/daily-summary", json=self.BODY, headers=AUTH_HEADERS
        )

        assert response.status_code == 401
        assert response.json()["code"] == "unauthorized"


def test_auth_runs_before_request_validation(client: TestClient) -> None:
    """An unauthenticated caller learns nothing about the request schema."""
    response = client.post("/ai/parse-todo", json={"bogus": True})

    assert response.status_code == 401


def test_a_valid_token_reaches_the_handler(
    client: TestClient, fake_ollama: FakeOllama
) -> None:
    fake_ollama.queue_object({"summary": "All quiet."})

    response = client.post(
        "/ai/daily-summary",
        json={"today": "2026-09-02", "todos": []},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200


def test_token_is_required_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """No default, no fallback: an unset AI_AGENT_TOKEN is a startup failure."""
    monkeypatch.delenv("AI_AGENT_TOKEN", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_an_empty_token_is_rejected_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_AGENT_TOKEN", "")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


class TestTokenStrengthIsEnforcedAtStartup:
    """A weak or public shared secret is worse than none: it looks like defence."""

    def test_a_short_token_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_AGENT_TOKEN", "x" * (MIN_TOKEN_LENGTH - 1))

        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)  # type: ignore[call-arg]

        assert "at least 32 characters" in str(excinfo.value)

    def test_a_token_at_the_floor_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_AGENT_TOKEN", "x" * MIN_TOKEN_LENGTH)

        settings = Settings(_env_file=None)  # type: ignore[call-arg]

        assert len(settings.AI_AGENT_TOKEN) == MIN_TOKEN_LENGTH

    @pytest.mark.parametrize("placeholder", sorted(PLACEHOLDER_TOKENS))
    def test_the_env_example_placeholder_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, placeholder: str
    ) -> None:
        monkeypatch.setenv("AI_AGENT_TOKEN", placeholder)

        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)  # type: ignore[call-arg]

        message = str(excinfo.value)
        assert "placeholder" in message
        assert "openssl rand -hex 32" in message

    def test_the_shipped_placeholder_would_pass_a_length_check_alone(self) -> None:
        """Exactly why the length floor is not sufficient on its own."""
        assert len("change-me-internal-shared-secret") == MIN_TOKEN_LENGTH

    def test_a_placeholder_with_surrounding_whitespace_is_still_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AI_AGENT_TOKEN", "  change-me-internal-shared-secret  ")

        with pytest.raises(ValidationError):
            Settings(_env_file=None)  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "variant",
        [
            "CHANGE-ME-INTERNAL-SHARED-SECRET",
            "Change-Me-Internal-Shared-Secret",
            "cHaNgE-mE-iNtErNaL-sHaReD-sEcReT",
            "  CHANGE-ME-INTERNAL-SHARED-SECRET  ",
        ],
    )
    def test_a_recased_placeholder_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch, variant: str
    ) -> None:
        """Retyping the placeholder in capitals does not make it a secret.

        Matches the backend's own casefolded placeholder check.
        """
        monkeypatch.setenv("AI_AGENT_TOKEN", variant)

        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)  # type: ignore[call-arg]

        assert "openssl rand -hex 32" in str(excinfo.value)

    def test_a_secret_that_merely_extends_a_placeholder_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inverted in iteration 4 (IT3-3): the guard is no longer whole-value.

        Until iteration 3 only an exact match counted, so appending random hex
        to the shipped placeholder was accepted. That is the *most likely* way
        an operator "edits" the example file, and the leading placeholder is
        still public — the entropy of the suffix does not rescue it.
        """
        monkeypatch.setenv(
            "AI_AGENT_TOKEN", "change-me-internal-shared-secret-" + secrets.token_hex(8)
        )

        with pytest.raises(ValidationError) as excinfo:
            Settings(_env_file=None)  # type: ignore[call-arg]

        assert "openssl rand -hex 32" in str(excinfo.value)

    def test_a_real_generated_secret_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        generated = secrets.token_hex(32)  # what `openssl rand -hex 32` produces
        monkeypatch.setenv("AI_AGENT_TOKEN", generated)

        assert Settings(_env_file=None).AI_AGENT_TOKEN == generated  # type: ignore[call-arg]


def test_the_token_never_appears_in_a_response(
    client: TestClient, fake_ollama: FakeOllama
) -> None:
    fake_ollama.queue_object({"summary": "All quiet."})
    ok = client.post(
        "/ai/daily-summary",
        json={"today": "2026-09-02", "todos": []},
        headers=AUTH_HEADERS,
    )
    denied = client.post("/ai/daily-summary", json={"today": "2026-09-02", "todos": []})

    for response in (ok, denied):
        assert TEST_TOKEN not in response.text
        assert TEST_TOKEN not in str(dict(response.headers))
