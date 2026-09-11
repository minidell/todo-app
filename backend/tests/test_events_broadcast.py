"""``GET /api/events`` end to end: the stream, and what mutations put on it.

**Why the stream is driven through raw ASGI here.** httpx's ``ASGITransport``
buffers: ``handle_async_request`` awaits the whole application call before it
returns a response, so ``client.stream("GET", "/api/events")`` on an endless
stream would simply never return. :class:`StreamProbe` therefore speaks ASGI to
the app directly — which is *more* precise for this endpoint, not less: it can
assert on the response headers before the first byte of body, read frames as
they are produced, and deliver a real ``http.disconnect``.

The mutations that produce the events still go through the ordinary
``AsyncClient``, so every assertion below is about the real routes, the real
publisher and the real broker.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.events import KEEPALIVE_FRAME
from app.routers.events import EXPIRY_FRAME
from tests.conftest import bearer_headers, create_user

TODOS = "/api/todos"
LISTS = "/api/lists"
TAGS = "/api/tags"
EVENTS = "/api/events"

#: Long enough that a slow CI machine does not fail on scheduling, short enough
#: that a genuinely missing frame fails the test rather than hanging it.
FRAME_TIMEOUT = 5.0


class StreamProbe:
    """One live ``GET /api/events`` connection, driven over raw ASGI."""

    def __init__(
        self, app, headers: dict[str, str], *, spec_version: str = "2.3"
    ) -> None:
        self._app = app
        self._headers = headers
        self._spec_version = spec_version
        self.status: int | None = None
        self.headers_out: dict[str, str] = {}
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._started = asyncio.Event()
        self._disconnect = asyncio.Event()
        self._request_sent = False
        self._buffer = ""
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "StreamProbe":
        self._task = asyncio.create_task(self._run())
        await asyncio.wait_for(self._started.wait(), FRAME_TIMEOUT)
        return self

    async def __aexit__(self, *_exc_info) -> None:
        await self.aclose()

    async def _run(self) -> None:
        scope = {
            "type": "http",
            # spec_version 2.3 makes Starlette watch `receive` for the
            # disconnect, which is how this probe signals one by default.
            # Under 2.4 Starlette does no listening at all — it waits for
            # `send` to raise OSError, which never happens without a socket —
            # so a 2.4 probe isolates *our* per-iteration disconnect check as
            # the only thing that can end the stream.
            "asgi": {"version": "3.0", "spec_version": self._spec_version},
            "http_version": "1.1",
            "method": "GET",
            "path": EVENTS,
            "raw_path": EVENTS.encode(),
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "headers": [
                (key.lower().encode(), value.encode())
                for key, value in self._headers.items()
            ],
        }
        try:
            await self._app(scope, self._receive, self._send)
        finally:
            self._started.set()
            await self._chunks.put(None)

    async def _receive(self) -> dict[str, Any]:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._disconnect.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers_out = {
                key.decode().lower(): value.decode()
                for key, value in message.get("headers", [])
            }
            self._started.set()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            if body:
                await self._chunks.put(body)
            if not message.get("more_body", False):
                await self._chunks.put(None)

    async def next_frame(self, timeout: float = FRAME_TIMEOUT) -> str:
        """The next complete SSE frame, including its trailing blank line."""

        async def _read() -> str:
            while True:
                if "\n\n" in self._buffer:
                    block, self._buffer = self._buffer.split("\n\n", 1)
                    return block + "\n\n"
                chunk = await self._chunks.get()
                if chunk is None:
                    raise AssertionError(
                        f"stream ended with {self._buffer!r} still buffered"
                    )
                self._buffer += chunk.decode()

        return await asyncio.wait_for(_read(), timeout)

    async def next_event(self, timeout: float = FRAME_TIMEOUT) -> tuple[str, dict]:
        """The next *named* frame, skipping keep-alive comments."""
        while True:
            frame = await self.next_frame(timeout)
            if frame.startswith(":"):
                continue
            return parse_frame(frame)

    async def drain_until_end(self, timeout: float = FRAME_TIMEOUT) -> int:
        """Consume whatever is left and assert the stream really ended.

        Returns the number of body chunks that arrived first: an evicted
        subscriber still gets the frames already in its queue, and then the
        close sentinel — it is cut off, not silenced retroactively.
        """

        async def _drain() -> int:
            chunks = 0
            while True:
                chunk = await self._chunks.get()
                if chunk is None:
                    return chunks
                chunks += 1

        return await asyncio.wait_for(_drain(), timeout)

    def signal_disconnect(self) -> None:
        """Make the next ``receive()`` report ``http.disconnect``.

        Separate from :meth:`aclose` so a test can model the client that goes
        away *without* the server cancelling anything — the case the loop's
        per-iteration check exists for.
        """
        self._disconnect.set()

    async def aclose(self) -> None:
        self._disconnect.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


def parse_frame(frame: str) -> tuple[str, dict]:
    """``(event name, parsed data)`` for one frame."""
    name = "message"
    data_lines: list[str] = []
    for line in frame.strip("\n").split("\n"):
        if line.startswith("event:"):
            name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
    return name, json.loads("\n".join(data_lines))


@pytest.fixture
async def stream(app, auth_headers) -> StreamProbe:
    async with StreamProbe(app, {**auth_headers, "accept": "text/event-stream"}) as probe:
        # Every stream opens with `ready`; consuming it here keeps each test
        # about the frame it actually cares about.
        name, _data = await probe.next_event()
        assert name == "ready"
        yield probe


# --- connecting ------------------------------------------------------------


async def test_the_stream_requires_a_token(app) -> None:
    async with StreamProbe(app, {}) as probe:
        assert probe.status == 401


async def test_an_invalid_token_does_not_open_a_stream(app) -> None:
    async with StreamProbe(app, {"authorization": "Bearer not-a-jwt"}) as probe:
        assert probe.status == 401


async def test_the_stream_sends_the_documented_headers(app, auth_headers) -> None:
    async with StreamProbe(app, auth_headers) as probe:
        assert probe.status == 200
        assert probe.headers_out["content-type"].startswith("text/event-stream")
        assert probe.headers_out["cache-control"] == "no-cache"
        # Without this nginx buffers the whole response and the browser sees
        # nothing at all until the connection closes (§7.1).
        assert probe.headers_out["x-accel-buffering"] == "no"


async def test_the_first_frame_is_ready(app, auth_headers, user: User) -> None:
    async with StreamProbe(app, auth_headers) as probe:
        name, data = await probe.next_event()
        assert name == "ready"
        assert data == {"user_id": str(user.id)}


async def test_a_connected_stream_registers_exactly_one_subscriber(
    app, stream: StreamProbe, user: User
) -> None:
    assert app.state.broker.subscriber_count_for(user.id) == 1


async def test_disconnecting_removes_the_subscriber(
    app, auth_headers, user: User
) -> None:
    async with StreamProbe(app, auth_headers) as probe:
        await probe.next_event()
        assert app.state.broker.subscriber_count == 1

    for _ in range(50):
        if app.state.broker.subscriber_count == 0:
            break
        await asyncio.sleep(0.02)
    assert app.state.broker.subscriber_count == 0


async def test_a_disconnect_is_noticed_between_events_not_only_at_a_heartbeat(
    app, client: AsyncClient, auth_headers, user: User, monkeypatch
) -> None:
    """A client that connects and never reads must still be reclaimed (SEC-2).

    The disconnect check sits at the top of every loop iteration rather than in
    the heartbeat branch alone: a silent client would otherwise keep its queue,
    its task and its slot against the per-user cap until the next comment frame
    failed to write — up to 25 seconds, per abandoned connection.

    The heartbeat is left long on purpose and the probe speaks ASGI 2.4, under
    which Starlette does no disconnect listening of its own: the per-iteration
    check is then the *only* thing that can end this stream, so the test fails
    if it moves back into the heartbeat branch.
    """
    monkeypatch.setattr("app.routers.events.HEARTBEAT_SECONDS", 3600.0)

    probe = StreamProbe(app, auth_headers, spec_version="2.4")
    await probe.__aenter__()
    await probe.next_frame()
    assert app.state.broker.subscriber_count == 1

    probe.signal_disconnect()
    # Wake the generator with an event; the loop's next iteration sees the
    # disconnect and unregisters instead of writing to a dead client.
    await client.post(TODOS, json={"title": "Nobody is listening"})

    for _ in range(100):
        if app.state.broker.subscriber_count == 0:
            break
        await asyncio.sleep(0.02)
    assert app.state.broker.subscriber_count == 0

    await probe.aclose()


async def test_an_idle_stream_sends_keep_alives(app, auth_headers, monkeypatch) -> None:
    """The 25 s comment frame, at 50 ms so the test does not sleep for a minute."""
    monkeypatch.setattr("app.routers.events.HEARTBEAT_SECONDS", 0.05)

    async with StreamProbe(app, auth_headers) as probe:
        assert (await probe.next_frame()).startswith("event: ready")
        assert await probe.next_frame() == KEEPALIVE_FRAME
        assert await probe.next_frame() == KEEPALIVE_FRAME


async def test_a_keep_alive_does_not_disturb_events(
    app, client: AsyncClient, auth_headers, monkeypatch
) -> None:
    monkeypatch.setattr("app.routers.events.HEARTBEAT_SECONDS", 0.05)

    async with StreamProbe(app, auth_headers) as probe:
        await probe.next_event()
        await asyncio.sleep(0.12)  # let a couple of keep-alives through

        await client.post(TODOS, json={"title": "After the silence"})
        name, data = await probe.next_event()
        assert name == "todo.created"
        assert data["todo"]["title"] == "After the silence"


async def test_a_ninth_stream_evicts_the_first(app, auth_headers, user: User) -> None:
    """The per-user cap, end to end over the route (SEC-2).

    The oldest connection is closed rather than the newest refused: the tab the
    user just opened is the one they are looking at, and the stale entries are
    typically streams whose sockets died without a FIN.
    """
    cap = app.state.broker.max_streams_per_user
    probes = [StreamProbe(app, auth_headers) for _ in range(cap + 1)]
    try:
        for probe in probes:
            await probe.__aenter__()
            assert probe.status == 200
            assert (await probe.next_frame()).startswith("event: ready")
            assert app.state.broker.subscriber_count <= cap

        assert app.state.broker.subscriber_count == cap
        # The first one was closed, not merely unregistered.
        await probes[0].drain_until_end()
        assert probes[-1].status == 200
    finally:
        for probe in probes:
            await probe.aclose()


async def test_an_evicted_stream_stops_receiving_events(
    app, client: AsyncClient, auth_headers, user: User
) -> None:
    cap = app.state.broker.max_streams_per_user
    probes = [StreamProbe(app, auth_headers) for _ in range(cap + 1)]
    try:
        for probe in probes:
            await probe.__aenter__()
            await probe.next_frame()

        await client.post(TODOS, json={"title": "Only for the survivors"})

        name, _data = await probes[-1].next_event()
        assert name == "todo.created"
        with pytest.raises(AssertionError):
            # Its stream already ended; there is no event to read.
            await probes[0].next_event(timeout=1.0)
    finally:
        for probe in probes:
            await probe.aclose()


async def test_the_stream_ends_when_the_token_expires(
    app, user: User, test_settings
) -> None:
    """A stream must not outlive the credential that opened it.

    Authentication on a long-lived connection happens once, at the start. A
    token minted with a two-second life is used here so the *real* path runs —
    ``get_token_expiry`` reads ``exp`` off the presented token — rather than a
    dependency override that would prove only that the parameter is wired.
    """
    import jwt

    from app.security import JWT_ALGORITHM

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=2)
    short_lived = jwt.encode(
        {
            "sub": str(user.id),
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int(expires_at.timestamp()),
        },
        test_settings.JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    async with StreamProbe(
        app, {"authorization": f"Bearer {short_lived}"}
    ) as probe:
        assert probe.status == 200
        assert (await probe.next_frame()).startswith("event: ready")

        # The last frame is the documented comment, and then the stream is over.
        frames: list[str] = []
        while True:
            frame = await probe.next_frame(timeout=10.0)
            frames.append(frame)
            if frame == EXPIRY_FRAME:
                break

        await probe.drain_until_end(timeout=10.0)
        assert app.state.broker.subscriber_count == 0


async def test_an_expiring_stream_still_serves_events_until_then(
    app, client: AsyncClient, user: User, test_settings
) -> None:
    """The deadline ends the stream; it does not cripple it beforehand."""
    import jwt

    from app.security import JWT_ALGORITHM

    token = jwt.encode(
        {
            "sub": str(user.id),
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int((datetime.now(timezone.utc) + timedelta(seconds=30)).timestamp()),
        },
        test_settings.JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    async with StreamProbe(app, {"authorization": f"Bearer {token}"}) as probe:
        await probe.next_event()
        await client.post(TODOS, json={"title": "Before the deadline"})

        name, data = await probe.next_event()
        assert name == "todo.created"
        assert data["todo"]["title"] == "Before the deadline"


async def test_an_already_expired_token_never_opens_a_stream(
    app, user: User, test_settings
) -> None:
    import jwt

    from app.security import JWT_ALGORITHM

    expired = jwt.encode(
        {
            "sub": str(user.id),
            "iat": int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()),
            "exp": int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),
        },
        test_settings.JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )

    async with StreamProbe(app, {"authorization": f"Bearer {expired}"}) as probe:
        assert probe.status == 401
    assert app.state.broker.subscriber_count == 0


async def test_the_body_size_middleware_does_not_buffer_the_stream(
    app, client: AsyncClient, auth_headers
) -> None:
    """Slice-4 A3: ``BodySizeLimitMiddleware`` is request-side only.

    It wraps ``send`` (to replace a response with a 413 once a body blows the
    limit), so a version of it that collected messages instead of forwarding
    them would turn every SSE frame into "nothing until the connection closes"
    — which is indistinguishable from a backend that never publishes.

    The proof is that frames arrive *while the response is still open*: the
    assertions below run before any disconnect, and the stack under test is the
    full app, middleware included.
    """
    from app.middleware import BodySizeLimitMiddleware

    assert any(
        middleware.cls is BodySizeLimitMiddleware for middleware in app.user_middleware
    ), "this test is meaningless if the middleware is not installed"

    async with StreamProbe(app, auth_headers) as probe:
        assert (await probe.next_frame()).startswith("event: ready")

        await client.post(TODOS, json={"title": "Arrives before the close"})
        name, data = await probe.next_event()

        assert name == "todo.created"
        assert data["todo"]["title"] == "Arrives before the close"


async def test_an_evicted_subscriber_gets_its_stream_closed(
    app, stream: StreamProbe, user: User
) -> None:
    """A client that stops reading is dropped, not buffered forever."""
    from app.events import QUEUE_MAXSIZE, Event

    broker = app.state.broker
    # Published without yielding to the loop, so the generator cannot keep up —
    # exactly the shape of a real stalled client.
    for index in range(QUEUE_MAXSIZE + 5):
        await broker.publish(user.id, Event("todo.updated", {"origin": None, "n": index}))

    assert broker.subscriber_count == 0
    delivered = await stream.drain_until_end()
    # It kept what it had already been handed, then the stream ended, rather
    # than the process holding an ever-growing backlog for a dead reader.
    assert 0 < delivered <= QUEUE_MAXSIZE


# --- todo events -----------------------------------------------------------


async def test_creating_a_todo_broadcasts_it(
    client: AsyncClient, stream: StreamProbe
) -> None:
    created = (await client.post(TODOS, json={"title": "Buy milk"})).json()

    name, data = await stream.next_event()
    assert name == "todo.created"
    assert data["origin"] is None
    assert data["todo"]["id"] == created["id"]
    assert data["todo"]["title"] == "Buy milk"
    # The payload is the same shape as a GET, so a receiver can use it directly.
    assert data["todo"] == created


async def test_the_client_id_becomes_the_origin(
    client: AsyncClient, stream: StreamProbe
) -> None:
    client_id = str(uuid4())
    await client.post(
        TODOS, json={"title": "From tab A"}, headers={"X-Client-Id": client_id}
    )

    _name, data = await stream.next_event()
    assert data["origin"] == client_id


async def test_a_malformed_client_id_becomes_a_null_origin(
    client: AsyncClient, stream: StreamProbe
) -> None:
    """A header value that is not a uuid must not be reflected to other tabs."""
    await client.post(
        TODOS, json={"title": "Bad header"}, headers={"X-Client-Id": "<script>"}
    )

    _name, data = await stream.next_event()
    assert data["origin"] is None


async def test_updating_a_todo_broadcasts_the_update(
    client: AsyncClient, stream: StreamProbe
) -> None:
    created = (await client.post(TODOS, json={"title": "Toggle me"})).json()
    await stream.next_event()

    await client.patch(f"{TODOS}/{created['id']}", json={"completed": True})

    name, data = await stream.next_event()
    assert name == "todo.updated"
    assert data["todo"]["completed"] is True


async def test_deleting_a_todo_broadcasts_id_and_list(
    client: AsyncClient, stream: StreamProbe
) -> None:
    created = (await client.post(TODOS, json={"title": "Delete me"})).json()
    await stream.next_event()

    await client.delete(f"{TODOS}/{created['id']}")

    name, data = await stream.next_event()
    assert name == "todo.deleted"
    assert data == {
        "origin": None,
        "id": created["id"],
        "list_id": created["list_id"],
    }


async def test_moving_a_todo_between_lists_sends_one_update(
    client: AsyncClient, stream: StreamProbe
) -> None:
    created = (await client.post(TODOS, json={"title": "Move me"})).json()
    await stream.next_event()
    work = (await client.post(LISTS, json={"name": "Work"})).json()
    await stream.next_event()  # list.created

    await client.patch(f"{TODOS}/{created['id']}", json={"list_id": work["id"]})

    name, data = await stream.next_event()
    assert name == "todo.updated"
    assert data["todo"]["list_id"] == work["id"]


# --- subtask events (§7.2: always the top-level parent) --------------------


async def test_creating_a_subtask_broadcasts_the_parent(
    client: AsyncClient, stream: StreamProbe
) -> None:
    parent = (await client.post(TODOS, json={"title": "Ship the feature"})).json()
    await stream.next_event()

    await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Write tests"})

    name, data = await stream.next_event()
    assert name == "todo.updated"
    assert data["todo"]["id"] == parent["id"]
    assert [sub["title"] for sub in data["todo"]["subtasks"]] == ["Write tests"]


async def test_updating_a_subtask_broadcasts_the_parent_with_it_embedded(
    client: AsyncClient, stream: StreamProbe
) -> None:
    parent = (await client.post(TODOS, json={"title": "Ship it"})).json()
    await stream.next_event()
    subtask = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Step one"})
    ).json()
    await stream.next_event()

    await client.patch(f"{TODOS}/{subtask['id']}", json={"completed": True})

    name, data = await stream.next_event()
    assert name == "todo.updated"
    assert data["todo"]["id"] == parent["id"]
    assert [(s["title"], s["completed"]) for s in data["todo"]["subtasks"]] == [
        ("Step one", True)
    ]


async def test_deleting_a_subtask_broadcasts_the_parent_without_it(
    client: AsyncClient, stream: StreamProbe
) -> None:
    parent = (await client.post(TODOS, json={"title": "Ship it"})).json()
    await stream.next_event()
    subtask = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Doomed"})
    ).json()
    await stream.next_event()

    await client.delete(f"{TODOS}/{subtask['id']}")

    name, data = await stream.next_event()
    assert name == "todo.updated"
    assert data["todo"]["id"] == parent["id"]
    # The stale cached collection is the bug this guards: the parent must come
    # back reloaded, not with the child it no longer has.
    assert data["todo"]["subtasks"] == []


async def test_promoting_a_subtask_announces_both_families(
    client: AsyncClient, stream: StreamProbe
) -> None:
    parent = (await client.post(TODOS, json={"title": "Parent"})).json()
    await stream.next_event()
    subtask = (
        await client.post(f"{TODOS}/{parent['id']}/subtasks", json={"title": "Child"})
    ).json()
    await stream.next_event()

    await client.patch(f"{TODOS}/{subtask['id']}", json={"parent_id": None})

    frames = [await stream.next_event(), await stream.next_event()]
    by_id = {data["todo"]["id"]: data["todo"] for _name, data in frames}
    assert set(by_id) == {parent["id"], subtask["id"]}
    assert by_id[parent["id"]]["subtasks"] == []
    assert by_id[subtask["id"]]["parent_id"] is None


# --- list events -----------------------------------------------------------


async def test_creating_a_list_broadcasts_it(
    client: AsyncClient, stream: StreamProbe
) -> None:
    created = (await client.post(LISTS, json={"name": "Work"})).json()

    name, data = await stream.next_event()
    assert name == "list.created"
    assert data["list"] == created


async def test_renaming_a_list_broadcasts_the_update(
    client: AsyncClient, stream: StreamProbe
) -> None:
    created = (await client.post(LISTS, json={"name": "Work"})).json()
    await stream.next_event()

    await client.patch(f"{LISTS}/{created['id']}", json={"name": "Office"})

    name, data = await stream.next_event()
    assert name == "list.updated"
    assert data["list"]["name"] == "Office"


async def test_a_rejected_list_rename_publishes_nothing(
    client: AsyncClient, stream: StreamProbe, monkeypatch
) -> None:
    """The 409 path rolls back — and must take its staged frame with it."""
    work = (await client.post(LISTS, json={"name": "Work"})).json()
    await stream.next_event()
    await client.post(LISTS, json={"name": "Home"})
    await stream.next_event()

    conflict = await client.patch(f"{LISTS}/{work['id']}", json={"name": "Home"})
    assert conflict.status_code == 409

    await client.post(TODOS, json={"title": "Marker"})
    name, _data = await stream.next_event()
    assert name == "todo.created"  # not a list.updated for the failed rename


async def test_deleting_a_list_broadcasts_it_and_its_todos(
    client: AsyncClient, stream: StreamProbe
) -> None:
    work = (await client.post(LISTS, json={"name": "Work"})).json()
    await stream.next_event()
    first = (
        await client.post(TODOS, json={"title": "One", "list_id": work["id"]})
    ).json()
    await stream.next_event()
    second = (
        await client.post(TODOS, json={"title": "Two", "list_id": work["id"]})
    ).json()
    await stream.next_event()

    await client.delete(f"{LISTS}/{work['id']}")

    frames = [await stream.next_event() for _ in range(3)]
    names = [name for name, _data in frames]
    assert names == ["list.deleted", "todo.deleted", "todo.deleted"]
    assert frames[0][1]["id"] == work["id"]
    assert {frames[1][1]["id"], frames[2][1]["id"]} == {first["id"], second["id"]}


# --- tag events (§7.2, iteration-4 delta) ----------------------------------


async def _drain_todo_with_tags(
    client: AsyncClient, stream: StreamProbe, title: str, tags: list[str]
) -> dict:
    """Create a todo and consume every frame it produced, returning the todo."""
    created = (await client.post(TODOS, json={"title": title, "tags": tags})).json()
    while True:
        name, _data = await stream.next_event()
        if name == "todo.created":
            return created


async def test_renaming_a_tag_broadcasts_the_update(
    client: AsyncClient, stream: StreamProbe
) -> None:
    await _drain_todo_with_tags(client, stream, "Buy milk", ["errand"])
    tag = (await client.get(TAGS)).json()[0]

    renamed = (await client.patch(f"{TAGS}/{tag['id']}", json={"name": "errands"})).json()

    name, data = await stream.next_event()
    assert name == "tag.updated"
    assert data["origin"] is None
    # The payload is the endpoint's own response, so a receiver can apply it
    # exactly as it would a GET.
    assert data["tag"] == renamed
    assert data["tag"] == {"id": tag["id"], "name": "errands", "todo_count": 1}


async def test_renaming_a_tag_broadcasts_exactly_one_frame(
    client: AsyncClient, stream: StreamProbe
) -> None:
    """D-IT4-2: no per-todo fan-out, however many todos carry the tag."""
    await _drain_todo_with_tags(client, stream, "One", ["errand"])
    await _drain_todo_with_tags(client, stream, "Two", ["errand"])
    await _drain_todo_with_tags(client, stream, "Three", ["errand"])
    tag = (await client.get(TAGS)).json()[0]

    await client.patch(f"{TAGS}/{tag['id']}", json={"name": "errands"})

    name, data = await stream.next_event()
    assert name == "tag.updated"
    assert data["tag"]["todo_count"] == 3

    # A marker proves the next frame on the stream is not a fourth todo.updated.
    await client.post(TODOS, json={"title": "Marker"})
    name, _data = await stream.next_event()
    assert name == "todo.created"


async def test_deleting_a_tag_broadcasts_the_id(
    client: AsyncClient, stream: StreamProbe
) -> None:
    await _drain_todo_with_tags(client, stream, "Buy milk", ["errand"])
    tag = (await client.get(TAGS)).json()[0]

    await client.delete(f"{TAGS}/{tag['id']}")

    name, data = await stream.next_event()
    assert name == "tag.deleted"
    assert data == {"origin": None, "id": tag["id"]}

    await client.post(TODOS, json={"title": "Marker"})
    name, _data = await stream.next_event()
    assert name == "todo.created"  # exactly one frame for the delete


async def test_the_client_id_becomes_the_origin_of_a_tag_frame(
    client: AsyncClient, stream: StreamProbe
) -> None:
    client_id = str(uuid4())
    await client.post(
        TODOS,
        json={"title": "Buy milk", "tags": ["errand"]},
        headers={"X-Client-Id": client_id},
    )

    name, data = await stream.next_event()
    assert name == "tag.created"
    assert data["origin"] == client_id
    name, data = await stream.next_event()
    assert (name, data["origin"]) == ("todo.created", client_id)


async def test_a_new_tag_on_a_todo_is_announced_before_the_todo(
    client: AsyncClient, stream: StreamProbe
) -> None:
    """§7.2: a receiver applying frames in order never sees a todo naming a tag
    it has not been told about."""
    created = (
        await client.post(TODOS, json={"title": "Buy milk", "tags": ["errand"]})
    ).json()

    frames = [await stream.next_event() for _ in range(2)]
    assert [name for name, _data in frames] == ["tag.created", "todo.created"]
    assert frames[0][1]["tag"]["name"] == "errand"
    # A tag created here can be attached to nothing else yet.
    assert frames[0][1]["tag"]["todo_count"] == 1
    assert frames[1][1]["todo"]["id"] == created["id"]


async def test_only_the_genuinely_new_tag_is_announced(
    client: AsyncClient, stream: StreamProbe
) -> None:
    await _drain_todo_with_tags(client, stream, "First", ["home"])

    await client.post(TODOS, json={"title": "Second", "tags": ["home", "errand"]})

    frames = [await stream.next_event() for _ in range(2)]
    assert [name for name, _data in frames] == ["tag.created", "todo.created"]
    assert frames[0][1]["tag"]["name"] == "errand"


async def test_reusing_existing_tags_announces_no_tag(
    client: AsyncClient, stream: StreamProbe
) -> None:
    await _drain_todo_with_tags(client, stream, "First", ["home", "errand"])

    await client.post(TODOS, json={"title": "Second", "tags": ["home", "errand"]})

    name, _data = await stream.next_event()
    assert name == "todo.created"  # no tag.created in front of it


async def test_a_todo_without_tags_announces_no_tag(
    client: AsyncClient, stream: StreamProbe
) -> None:
    await client.post(TODOS, json={"title": "Plain"})

    name, _data = await stream.next_event()
    assert name == "todo.created"


async def test_patching_a_todo_announces_the_tag_it_invents(
    client: AsyncClient, stream: StreamProbe
) -> None:
    created = await _drain_todo_with_tags(client, stream, "Task", ["home"])

    await client.patch(f"{TODOS}/{created['id']}", json={"tags": ["home", "errand"]})

    frames = [await stream.next_event() for _ in range(2)]
    assert [name for name, _data in frames] == ["tag.created", "todo.updated"]
    assert frames[0][1]["tag"] == {
        "id": frames[0][1]["tag"]["id"],
        "name": "errand",
        "todo_count": 1,
    }
    assert sorted(frames[1][1]["todo"]["tags"]) == ["errand", "home"]


async def test_a_subtask_announces_its_new_tag_with_a_zero_count(
    client: AsyncClient, stream: StreamProbe
) -> None:
    """Risk R2: ``todo_count`` counts top-level todos, and a subtask is not one."""
    parent = (await client.post(TODOS, json={"title": "Ship it"})).json()
    await stream.next_event()

    await client.post(
        f"{TODOS}/{parent['id']}/subtasks",
        json={"title": "Write tests", "tags": ["errand"]},
    )

    frames = [await stream.next_event() for _ in range(2)]
    assert [name for name, _data in frames] == ["tag.created", "todo.updated"]
    assert frames[0][1]["tag"]["name"] == "errand"
    assert frames[0][1]["tag"]["todo_count"] == 0
    # The count matches what GET /api/tags reports for the same tag.
    assert (await client.get(TAGS)).json()[0]["todo_count"] == 0


async def test_two_new_tags_are_announced_in_the_order_they_were_written(
    client: AsyncClient, stream: StreamProbe
) -> None:
    await client.post(TODOS, json={"title": "Task", "tags": ["zulu", "alpha"]})

    frames = [await stream.next_event() for _ in range(3)]
    assert [name for name, _data in frames] == [
        "tag.created",
        "tag.created",
        "todo.created",
    ]
    assert [frame[1]["tag"]["name"] for frame in frames[:2]] == ["zulu", "alpha"]


async def test_a_second_request_does_not_re_announce_the_first_ones_tags(
    client: AsyncClient, stream: StreamProbe
) -> None:
    """Risk R1: the outbox is drained, and it is per request."""
    await _drain_todo_with_tags(client, stream, "First", ["home"])

    await client.post(TODOS, json={"title": "Second", "tags": ["home"]})

    name, _data = await stream.next_event()
    assert name == "todo.created"


async def test_another_users_tag_rename_never_reaches_this_stream(
    app,
    client: AsyncClient,
    stream: StreamProbe,
    other_user: User,
    test_settings,
) -> None:
    """Criterion 6: no ``tag.*`` frame crosses accounts."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers=bearer_headers(other_user, test_settings),
    ) as intruder:
        await intruder.post(TODOS, json={"title": "Theirs", "tags": ["errand"]})
        theirs = (await intruder.get(TAGS)).json()[0]
        assert (
            await intruder.patch(f"{TAGS}/{theirs['id']}", json={"name": "errands"})
        ).status_code == 200
        assert (await intruder.delete(f"{TAGS}/{theirs['id']}")).status_code == 204

    with pytest.raises(asyncio.TimeoutError):
        await stream.next_event(timeout=0.3)


# --- isolation & ordering --------------------------------------------------


async def test_another_users_mutation_never_reaches_this_stream(
    app,
    stream: StreamProbe,
    db_session: AsyncSession,
    other_user: User,
    test_settings,
) -> None:
    """Acceptance criterion 4: events are scoped per user."""
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers=bearer_headers(other_user, test_settings),
    ) as intruder:
        assert (
            await intruder.post(TODOS, json={"title": "Not yours"})
        ).status_code == 201

    with pytest.raises(asyncio.TimeoutError):
        await stream.next_event(timeout=0.3)


async def test_a_handler_that_fails_after_the_write_publishes_nothing(
    app, client: AsyncClient, stream: StreamProbe, monkeypatch
) -> None:
    """Slice-4 A6.4: no event may escape a transaction that did not commit."""
    import app.routers.todos as todos_router

    async def explode(*_args, **_kwargs) -> None:
        raise RuntimeError("boom, after the write")

    monkeypatch.setattr(todos_router, "commit_and_publish", explode)

    with pytest.raises(RuntimeError):
        await client.post(TODOS, json={"title": "Never announced"})

    with pytest.raises(asyncio.TimeoutError):
        await stream.next_event(timeout=0.3)


#: Streams opened per account by the pool test. Deliberately well under
#: ``MAX_STREAMS_PER_USER`` (8) — see the test's docstring for why that matters.
POOL_TEST_STREAMS_PER_USER = 4

#: Accounts the pool test spreads those streams over. Three is the floor on
#: both lanes so the two engines exercise the same shape; the test scales it up
#: if a bigger pool needs more streams to be interesting.
POOL_TEST_MIN_USERS = 3


async def test_many_streams_do_not_exhaust_the_connection_pool(
    engine, db_session: AsyncSession, test_settings
) -> None:
    """Slice-4 A6.5: a stream must not hold a DB session for its lifetime.

    Built on a *real* sessionmaker rather than the suite's shared-session
    fixture, which would make the whole question vacuous: with the override in
    place no request ever takes a connection from the pool.

    ``get_current_user`` resolves the session with ``scope="function"``, so its
    teardown runs before the response is sent — which for a ``StreamingResponse``
    means before the first byte of the body. If that ever regressed, the streams
    alone would hold every connection and the ordinary request at the end would
    block until it timed out.

    **The streams are spread across several accounts, and that is load-bearing.**
    Two limits meet here and they are not the same limit: the connection pool is
    per *process* (``pool_size + max_overflow``), while ``MAX_STREAMS_PER_USER``
    is per *account*. Opening every stream as one user — which this test used to
    do — means the broker evicts the oldest past 8, so the process never holds
    enough streams at once to say anything about the pool, and the count assert
    fails at ``8 == 12``. Enough users to stay under the cap, enough streams in
    total to exceed the pool: that is the only combination that tests the pool.
    """
    from math import ceil

    from app.db.session import create_sessionmaker
    from app.main import create_app

    application = create_app(test_settings)
    application.state.engine = engine
    application.state.sessionmaker = create_sessionmaker(engine)

    # SQLite's StaticPool has neither attribute — it shares one connection — so
    # a capacity of 1 is the honest answer there, and the floors below keep both
    # lanes running the same 3 × 4 shape regardless.
    pool_size = getattr(engine.pool, "size", lambda: 1)()
    overflow = getattr(engine.pool, "_max_overflow", 0)
    pool_capacity = pool_size + max(overflow, 0)

    cap = application.state.broker.max_streams_per_user
    assert POOL_TEST_STREAMS_PER_USER <= cap, (
        "this test must not trip the per-user stream cap; that is a different "
        f"limit with its own tests (cap={cap})"
    )

    user_count = max(
        POOL_TEST_MIN_USERS,
        ceil((pool_capacity + 2) / POOL_TEST_STREAMS_PER_USER),
    )
    total_streams = user_count * POOL_TEST_STREAMS_PER_USER
    assert total_streams > pool_capacity, (
        f"{total_streams} streams would fit in a pool of {pool_capacity}; the "
        "test would prove nothing"
    )

    users = [
        await create_user(db_session, f"pool-{index}@example.com")
        for index in range(user_count)
    ]

    probes: list[StreamProbe] = []
    for account in users:
        headers = {
            **bearer_headers(account, test_settings),
            "accept": "text/event-stream",
        }
        probes.extend(
            StreamProbe(application, headers)
            for _ in range(POOL_TEST_STREAMS_PER_USER)
        )

    try:
        for probe in probes:
            await probe.__aenter__()
            name, _data = await probe.next_event()
            assert name == "ready"

        # Every stream is still registered: none was evicted, so the process
        # really is holding more open streams than the pool has connections.
        assert application.state.broker.subscriber_count == total_streams
        for account in users:
            assert (
                application.state.broker.subscriber_count_for(account.id)
                == POOL_TEST_STREAMS_PER_USER
            )

        transport = ASGITransport(app=application)
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers=bearer_headers(users[0], test_settings),
        ) as ordinary:
            response = await asyncio.wait_for(ordinary.get(LISTS), timeout=10)
        assert response.status_code == 200
    finally:
        for probe in probes:
            await probe.aclose()


async def test_events_are_published_after_the_commit_not_before(
    app, client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    """The ordering itself, recorded rather than inferred.

    A frame that goes out first is a promise the database has not kept yet: the
    receiving tab refetches, does not find the row, and settles on a view the
    server disagrees with.
    """
    order: list[str] = []

    original_commit = AsyncSession.commit

    async def recording_commit(self, *args, **kwargs):
        result = await original_commit(self, *args, **kwargs)
        order.append("commit")
        return result

    original_publish = type(app.state.broker).publish

    async def recording_publish(self, user_id, event):
        order.append(f"publish:{event.name}")
        return await original_publish(self, user_id, event)

    monkeypatch.setattr(AsyncSession, "commit", recording_commit)
    monkeypatch.setattr(type(app.state.broker), "publish", recording_publish)

    assert (await client.post(TODOS, json={"title": "Ordered"})).status_code == 201

    assert order == ["commit", "publish:todo.created"], order


async def test_tag_events_are_published_after_the_commit_too(
    app, client: AsyncClient, monkeypatch
) -> None:
    """Criterion 5, for the frame family added in iteration 4.

    Both the tag and the todo frame of one request go out after the single
    commit, and in staging order — so a receiver is told about the tag before
    it is told about the todo that names it.
    """
    await client.post(TODOS, json={"title": "Seed", "tags": ["errand"]})
    tag = (await client.get(TAGS)).json()[0]

    order: list[str] = []
    original_commit = AsyncSession.commit

    async def recording_commit(self, *args, **kwargs):
        result = await original_commit(self, *args, **kwargs)
        order.append("commit")
        return result

    original_publish = type(app.state.broker).publish

    async def recording_publish(self, user_id, event):
        order.append(f"publish:{event.name}")
        return await original_publish(self, user_id, event)

    monkeypatch.setattr(AsyncSession, "commit", recording_commit)
    monkeypatch.setattr(type(app.state.broker), "publish", recording_publish)

    assert (
        await client.patch(f"{TAGS}/{tag['id']}", json={"name": "errands"})
    ).status_code == 200
    assert (
        await client.post(TODOS, json={"title": "Second", "tags": ["chore"]})
    ).status_code == 201
    assert (await client.delete(f"{TAGS}/{tag['id']}")).status_code == 204

    assert order == [
        "commit",
        "publish:tag.updated",
        "commit",
        "publish:tag.created",
        "publish:todo.created",
        "commit",
        "publish:tag.deleted",
    ], order
