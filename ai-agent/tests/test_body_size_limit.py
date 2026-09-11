"""Request-size limits: the ASGI floor and the per-field ceilings.

Pydantic's `max_length` only runs *after* the whole body has been buffered, so
the middleware is what actually bounds memory; the per-item string limits are
what bound how much of that body can reach a prompt.
"""

from __future__ import annotations

from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.middleware import MAX_BODY_BYTES
from tests.conftest import AUTH_HEADERS, FakeOllama


class TestAsgiBodyLimit:
    def test_an_oversized_body_is_rejected_with_413(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        response = client.post(
            "/ai/parse-todo",
            content=b'{"text": "' + b"x" * (MAX_BODY_BYTES + 1024) + b'"}',
            headers={**AUTH_HEADERS, "content-type": "application/json"},
        )

        assert response.status_code == 413
        assert response.json() == {
            "detail": "Request body too large",
            "code": "request_too_large",
        }
        assert fake_ollama.chat_calls == 0

    def test_a_chunked_body_with_no_content_length_is_also_rejected(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        """Master C4: a lying or absent Content-Length must not get through."""

        def chunks() -> Iterator[bytes]:
            yield b'{"text": "'
            for _ in range(10):
                yield b"x" * 16 * 1024
            yield b'", "today": "2026-09-02"}'

        response = client.post(
            "/ai/parse-todo",
            content=chunks(),
            headers={**AUTH_HEADERS, "content-type": "application/json"},
        )

        assert response.status_code == 413
        assert response.json()["code"] == "request_too_large"
        assert fake_ollama.chat_calls == 0

    def test_the_limit_applies_before_authentication(
        self, client: TestClient
    ) -> None:
        """The body is never buffered for an unauthenticated caller either."""
        response = client.post(
            "/ai/parse-todo",
            content=b"x" * (MAX_BODY_BYTES + 1),
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 413

    def test_a_normal_body_passes_through(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        fake_ollama.queue_object({"summary": "All quiet."})

        response = client.post(
            "/ai/daily-summary",
            json={"today": "2026-09-02", "todos": []},
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 200

    def test_get_requests_are_unaffected(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200


class TestPerItemStringLimits:
    def test_an_oversized_tag_item_is_rejected_with_422(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        response = client.post(
            "/ai/parse-todo",
            json={
                "text": "buy milk",
                "today": "2026-09-02",
                "known_tags": ["home", "x" * 41],
            },
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 422
        assert fake_ollama.chat_calls == 0

    def test_an_oversized_tag_item_on_suggest_metadata_is_rejected(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        response = client.post(
            "/ai/suggest-metadata",
            json={
                "title": "Pay tax",
                "today": "2026-09-02",
                "known_tags": ["y" * 200],
            },
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 422
        assert fake_ollama.chat_calls == 0

    @pytest.mark.parametrize(
        "todo",
        [
            {"title": "Pay tax", "priority": "z" * 11},
            {"title": "Pay tax", "list_name": "w" * 101},
            {"title": "t" * 201},
        ],
    )
    def test_oversized_summary_todo_fields_are_rejected(
        self, client: TestClient, todo: dict, fake_ollama: FakeOllama
    ) -> None:
        response = client.post(
            "/ai/daily-summary",
            json={"today": "2026-09-02", "todos": [todo]},
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 422
        assert fake_ollama.chat_calls == 0

    def test_values_at_the_limit_are_accepted(
        self, client: TestClient, fake_ollama: FakeOllama
    ) -> None:
        fake_ollama.queue_object({"summary": "All quiet."})

        response = client.post(
            "/ai/daily-summary",
            json={
                "today": "2026-09-02",
                "todos": [
                    {
                        "title": "t" * 200,
                        "priority": "z" * 10,
                        "list_name": "w" * 100,
                    }
                ],
            },
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 200
