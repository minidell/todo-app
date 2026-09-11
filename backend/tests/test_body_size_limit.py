"""Carry-over C4: the body limit must survive a lying or absent Content-Length.

Three attack shapes are covered:
  1. honest oversized ``Content-Length``  — rejected before any body is read;
  2. chunked upload with no ``Content-Length`` — rejected while streaming;
  3. small, falsified ``Content-Length`` with a large body — rejected while
     streaming.

Case 3 is driven at the ASGI level because HTTP clients refuse to send a body
that contradicts the header they set.
"""

from typing import AsyncIterator

from httpx import AsyncClient

from app.middleware import MAX_BODY_BYTES, BodySizeLimitMiddleware

TOO_LARGE_BODY = {"detail": "Request body too large", "code": "request_too_large"}


async def test_oversized_content_length_returns_413(client: AsyncClient) -> None:
    oversized = b"a" * (MAX_BODY_BYTES + 1)
    response = await client.post(
        "/api/todos",
        content=oversized,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == TOO_LARGE_BODY


async def test_chunked_body_without_content_length_returns_413(
    client: AsyncClient,
) -> None:
    chunk = b"a" * 8192

    async def stream() -> AsyncIterator[bytes]:
        for _ in range((MAX_BODY_BYTES // len(chunk)) + 2):
            yield chunk

    response = await client.post(
        "/api/todos",
        content=stream(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == TOO_LARGE_BODY


async def test_lying_content_length_returns_413(app) -> None:
    """A hostile client declares 10 bytes and sends megabytes."""
    middleware = BodySizeLimitMiddleware(app, max_bytes=MAX_BODY_BYTES)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/api/todos",
        "raw_path": b"/api/todos",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", b"10"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }

    chunks = [b"a" * 16384] * ((MAX_BODY_BYTES // 16384) + 2)
    sent = iter(chunks)

    async def receive():
        try:
            return {"type": "http.request", "body": next(sent), "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    messages = []

    async def send(message):
        messages.append(message)

    await middleware(scope, receive, send)

    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 413
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    assert body == b'{"detail": "Request body too large", "code": "request_too_large"}'


async def test_413_carries_cors_headers(client: AsyncClient) -> None:
    """CORS must wrap the size limit.

    The 413 is produced before the request reaches the app, so if the limit
    middleware sat outside CORS the browser would drop the response as a CORS
    failure and the user would see a generic network error instead of "too
    large".
    """
    response = await client.post(
        "/api/todos",
        content=b"a" * (MAX_BODY_BYTES + 1),
        headers={
            "content-type": "application/json",
            "origin": "http://localhost:5173",
        },
    )
    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.json() == TOO_LARGE_BODY


async def test_successful_responses_still_carry_cors_headers(
    client: AsyncClient,
) -> None:
    response = await client.get(
        "/api/todos", headers={"origin": "http://localhost:5173"}
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-expose-headers"] == "X-Total-Count"


async def test_normal_sized_post_is_unaffected(client: AsyncClient) -> None:
    response = await client.post("/api/todos", json={"title": "Buy milk"})
    assert response.status_code == 201


async def test_body_just_under_the_limit_is_accepted(client: AsyncClient) -> None:
    padding = MAX_BODY_BYTES - 512
    response = await client.post("/api/todos", json={"title": "x" * 200, "pad": "y" * padding})
    # Rejected by validation (extra key), *not* by the size limit.
    assert response.status_code == 422


async def test_middleware_does_not_touch_responses(client: AsyncClient) -> None:
    """Request-side only, so streaming responses (SSE, slice 4) flow freely."""
    for _ in range(3):
        await client.post("/api/todos", json={"title": "x" * 200})
    response = await client.get("/api/todos")
    assert response.status_code == 200
    assert len(response.json()) == 3
