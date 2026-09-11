"""``GET /ai/ping`` — the authenticated liveness echo (master §8.2, IT3-2).

The negative half (missing / wrong / empty token) is covered by the shared auth
matrix in ``test_auth.py::ROUTES``, on purpose: this endpoint's whole point is
that it behaves like every other ``/ai/*`` route when the token is wrong. What
is asserted here is the positive half and the one property that makes it safe
to call on every backend probe — it never touches Ollama.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import AUTH_HEADERS, TEST_TOKEN, FakeOllama


def test_a_valid_token_gets_ok(client: TestClient) -> None:
    response = client.get("/ai/ping", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ping_makes_no_ollama_call(
    client: TestClient, fake_ollama: FakeOllama
) -> None:
    """No chat, no /api/tags: the backend probes this on every status refresh.

    ``fake_ollama`` fails loudly on an unscripted call, so an accidental client
    dependency would already redden the test above; these counters state the
    requirement explicitly rather than relying on that side effect.
    """
    for _ in range(3):
        assert client.get("/ai/ping", headers=AUTH_HEADERS).status_code == 200

    assert fake_ollama.chat_calls == 0
    assert fake_ollama.tags_calls == 0


def test_ping_never_echoes_the_token(client: TestClient) -> None:
    """A liveness endpoint must not become a token oracle."""
    ok = client.get("/ai/ping", headers=AUTH_HEADERS)
    denied = client.get("/ai/ping", headers={"X-Internal-Token": "wrong-" + TEST_TOKEN})

    for response in (ok, denied):
        assert TEST_TOKEN not in response.text
        assert TEST_TOKEN not in str(dict(response.headers))


def test_ping_is_not_reachable_without_the_prefix(client: TestClient) -> None:
    """It lives under ``/ai`` so it inherits the router's token dependency.

    A ``/ping`` sitting next to unauthenticated ``/health`` would be reachable
    by anyone who can route to the container, which defeats the purpose.
    """
    assert client.get("/ping", headers=AUTH_HEADERS).status_code == 404
