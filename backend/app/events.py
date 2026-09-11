"""In-process realtime event fan-out for ``GET /api/events`` (master §7.3).

**Single replica by construction.** The broker is a dictionary in this
process's memory, so a second backend replica would deliver each event to only
the clients that happen to be connected to the replica that served the write.
Scaling out means replacing :class:`EventBroker` with Postgres
``LISTEN/NOTIFY`` or Redis pub/sub — deliberately out of scope for iteration 2
and documented in the README.

Two rules shape everything here:

*Publish after commit.* An event is a promise that a row exists. Emitting it
before the transaction lands lets a receiver refetch, miss the row, and settle
into a state the server disagrees with — worse than no realtime at all.
:class:`EventPublisher` therefore *stages* events and only
:func:`commit_and_publish` lets them out.

*A stalled subscriber is dropped, never buffered.* Each queue is bounded; a
client that stops reading loses its stream instead of growing this process's
memory. It reconnects and refetches, which is exactly the recovery path §7.4
already specifies for a dropped stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Frames a subscriber may fall behind by before it is dropped. Large enough
#: that an ordinary burst (a list delete fanning out one event per todo) is
#: absorbed, small enough that a dead connection cannot cost much memory.
QUEUE_MAXSIZE = 100

#: Idle gap after which the stream emits a comment frame. Anything much longer
#: risks an idle-connection timeout in a proxy between us and the browser.
HEARTBEAT_SECONDS = 25.0

#: A comment frame: the client's parser ignores it, but it keeps the socket and
#: every intermediary awake, and a write failure is how we notice a vanished
#: client that never sent a FIN.
KEEPALIVE_FRAME = ": keep-alive\n\n"

#: Queue item meaning "this subscriber is finished" — see :meth:`EventBroker.publish`.
CLOSE_SENTINEL: None = None

#: Concurrent streams one account may hold. A person uses a handful of tabs; a
#: script opening thousands would otherwise pin a queue, a task and a socket
#: each, on an endpoint whose whole job is to stay open. Eight is generous for
#: the former and useless for the latter.
DEFAULT_MAX_STREAMS_PER_USER = 8


@dataclass(frozen=True, slots=True)
class Event:
    """One SSE frame: a name and an already-JSON-serializable payload."""

    name: str
    data: dict[str, Any]


def format_sse(event: Event) -> str:
    """Render an event as a single ``text/event-stream`` frame.

    ``separators`` is not cosmetic: SSE frames are newline-delimited, so a
    literal newline inside ``data`` would split one frame into two and the
    second half would be parsed as a fresh (malformed) frame. ``json.dumps``
    escapes newlines inside strings, and the compact separators guarantee the
    encoder never adds one of its own.
    """
    payload = json.dumps(event.data, separators=(",", ":"), default=str)
    return f"event: {event.name}\ndata: {payload}\n\n"


class EventBroker:
    """Per-user fan-out of :class:`Event` frames to live SSE subscribers.

    Subscribers are held in an insertion-ordered mapping used as an ordered
    set: "which of this user's streams is the oldest" has to be answerable to
    enforce the per-user cap, and a plain ``set`` cannot answer it.
    """

    def __init__(self, max_streams_per_user: int = DEFAULT_MAX_STREAMS_PER_USER) -> None:
        if max_streams_per_user < 1:
            raise ValueError("max_streams_per_user must be >= 1")
        self._max_streams = max_streams_per_user
        self._subscribers: dict[UUID, dict[asyncio.Queue[Event | None], None]] = {}

    @property
    def max_streams_per_user(self) -> int:
        return self._max_streams

    @property
    def subscriber_count(self) -> int:
        """Total live subscribers across all users (tests and diagnostics)."""
        return sum(len(queues) for queues in self._subscribers.values())

    def subscriber_count_for(self, user_id: UUID) -> int:
        return len(self._subscribers.get(user_id, ()))

    @asynccontextmanager
    async def subscribe(
        self, user_id: UUID
    ) -> AsyncIterator[asyncio.Queue[Event | None]]:
        """Register a queue for ``user_id`` for the life of the ``async with``.

        Unregistering in ``finally`` is what keeps a disconnect from leaking:
        the SSE generator is closed by an ``asyncio.CancelledError`` when the
        client goes away, and that must still remove the queue.

        Past :attr:`max_streams_per_user` the **oldest** stream is evicted
        rather than the new one refused. Refusing would punish the wrong tab:
        the browser the user is looking at is the one that just connected, and
        the stale entries are typically streams whose sockets died without a
        FIN. An evicted client reconnects and refetches like any other dropped
        stream (§7.4), so the cap costs a round trip, not correctness.
        """
        queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        queues = self._subscribers.setdefault(user_id, {})
        queues[queue] = None
        self._enforce_cap(user_id)
        try:
            yield queue
        finally:
            self._discard(user_id, queue)

    def _enforce_cap(self, user_id: UUID) -> None:
        queues = self._subscribers.get(user_id)
        if queues is None:  # pragma: no cover - the caller just inserted one
            return
        while len(queues) > self._max_streams:
            oldest = next(iter(queues))
            logger.info(
                "User %s reached the %d-stream limit: evicting the oldest stream",
                user_id,
                self._max_streams,
            )
            self._close(user_id, oldest)

    def _discard(self, user_id: UUID, queue: asyncio.Queue[Event | None]) -> None:
        queues = self._subscribers.get(user_id)
        if queues is None:
            return
        queues.pop(queue, None)
        if not queues:
            # Drop the empty mapping too: a user who logs in from a thousand
            # tabs over a week must not leave a thousand empty entries behind.
            del self._subscribers[user_id]

    def _close(self, user_id: UUID, queue: asyncio.Queue[Event | None]) -> None:
        """Unregister a subscriber and tell its generator to stop.

        Unregistering alone would leave the generator parked on ``queue.get()``
        until its next heartbeat, still holding a task and a socket. A slot is
        freed if necessary and the close sentinel put in its place, so the
        stream ends promptly and the client reconnects (§7.4).
        """
        self._discard(user_id, queue)
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - it was full above
                pass
        try:
            queue.put_nowait(CLOSE_SENTINEL)
        except asyncio.QueueFull:  # pragma: no cover - a slot was just freed
            pass

    async def publish(self, user_id: UUID, event: Event) -> None:
        """Deliver ``event`` to every live subscriber of ``user_id``.

        Never blocks and never raises: publishing happens on the request path
        of a mutation that has *already committed*, so a slow reader must not
        be able to hold up (or fail) somebody else's write.

        Iterating over a copy matters — dropping an overflowing subscriber
        mutates the set we are walking.
        """
        for queue in list(self._subscribers.get(user_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._drop_stalled(user_id, queue)

    def _drop_stalled(self, user_id: UUID, queue: asyncio.Queue[Event | None]) -> None:
        """Evict a subscriber that stopped draining its queue.

        A stalled client must never grow this process's memory: it keeps the
        backlog it already has, loses the stream, reconnects and refetches.
        """
        self._close(user_id, queue)
        logger.warning(
            "Dropped a stalled event subscriber for user %s: queue full at %d frames",
            user_id,
            QUEUE_MAXSIZE,
        )


# --------------------------------------------------------------------------- #
# Frame builders (master §7.2)
# --------------------------------------------------------------------------- #
#
# ``origin`` lives at the *top level* of every payload, next to the entity, and
# never inside it: the client checks one field for echo suppression without
# knowing which kind of frame it holds.


def todo_created_event(todo: dict[str, Any], origin: str | None) -> Event:
    return Event("todo.created", {"origin": origin, "todo": todo})


def todo_updated_event(todo: dict[str, Any], origin: str | None) -> Event:
    return Event("todo.updated", {"origin": origin, "todo": todo})


def todo_deleted_event(
    todo_id: UUID | str, list_id: UUID | str, origin: str | None
) -> Event:
    return Event(
        "todo.deleted",
        {"origin": origin, "id": str(todo_id), "list_id": str(list_id)},
    )


def list_created_event(todo_list: dict[str, Any], origin: str | None) -> Event:
    return Event("list.created", {"origin": origin, "list": todo_list})


def list_updated_event(todo_list: dict[str, Any], origin: str | None) -> Event:
    return Event("list.updated", {"origin": origin, "list": todo_list})


def list_deleted_event(list_id: UUID | str, origin: str | None) -> Event:
    return Event("list.deleted", {"origin": origin, "id": str(list_id)})


# The ``tag.*`` family (master §7.2, iteration-4 delta). ``tag.created`` has no
# endpoint of its own: a tag comes into existence only by being named on a todo
# write (D-A3), so its frame is staged by the todo handlers — **before** their
# own ``todo.*`` frame, so a receiver applying frames in order never sees a todo
# naming a tag it has not been told about.


def tag_created_event(tag: dict[str, Any], origin: str | None) -> Event:
    return Event("tag.created", {"origin": origin, "tag": tag})


def tag_updated_event(tag: dict[str, Any], origin: str | None) -> Event:
    return Event("tag.updated", {"origin": origin, "tag": tag})


def tag_deleted_event(tag_id: UUID | str, origin: str | None) -> Event:
    return Event("tag.deleted", {"origin": origin, "id": str(tag_id)})


def ready_event(user_id: UUID) -> Event:
    """The first frame of every stream; the client refetches when it lands."""
    return Event("ready", {"user_id": str(user_id)})


# --------------------------------------------------------------------------- #
# Publish-after-commit
# --------------------------------------------------------------------------- #


class EventPublisher:
    """Per-request staging area for the events a handler wants to emit.

    Staged events are held until :meth:`flush`, which
    :func:`commit_and_publish` calls only after the transaction has committed.
    A handler that raises — or whose commit fails on a unique index — is
    therefore incapable of announcing a change that never happened: the
    publisher is discarded with the request and the frames are simply never
    sent.
    """

    def __init__(
        self, broker: EventBroker, user_id: UUID, origin: str | None
    ) -> None:
        self._broker = broker
        self._user_id = user_id
        self._origin = origin
        self._staged: list[Event] = []

    @property
    def origin(self) -> str | None:
        """The mutating tab's ``X-Client-Id``, or ``None``."""
        return self._origin

    @property
    def staged(self) -> tuple[Event, ...]:
        """What is waiting to be published (tests read this)."""
        return tuple(self._staged)

    def stage(self, event: Event) -> None:
        self._staged.append(event)

    def stage_todo_created(self, todo: dict[str, Any]) -> None:
        self.stage(todo_created_event(todo, self._origin))

    def stage_todo_updated(self, todo: dict[str, Any]) -> None:
        self.stage(todo_updated_event(todo, self._origin))

    def stage_todo_deleted(self, todo_id: UUID | str, list_id: UUID | str) -> None:
        self.stage(todo_deleted_event(todo_id, list_id, self._origin))

    def stage_list_created(self, todo_list: dict[str, Any]) -> None:
        self.stage(list_created_event(todo_list, self._origin))

    def stage_list_updated(self, todo_list: dict[str, Any]) -> None:
        self.stage(list_updated_event(todo_list, self._origin))

    def stage_list_deleted(self, list_id: UUID | str) -> None:
        self.stage(list_deleted_event(list_id, self._origin))

    def stage_tag_created(self, tag: dict[str, Any]) -> None:
        self.stage(tag_created_event(tag, self._origin))

    def stage_tag_updated(self, tag: dict[str, Any]) -> None:
        self.stage(tag_updated_event(tag, self._origin))

    def stage_tag_deleted(self, tag_id: UUID | str) -> None:
        self.stage(tag_deleted_event(tag_id, self._origin))

    async def flush(self) -> None:
        """Hand every staged event to the broker, exactly once."""
        staged, self._staged = self._staged, []
        for event in staged:
            await self._broker.publish(self._user_id, event)

    def discard(self) -> None:
        """Forget the staged events (a handler that decided not to publish)."""
        self._staged.clear()


async def commit_and_publish(session: AsyncSession, publisher: EventPublisher) -> None:
    """Commit the request's work, **then** announce it. Never the reverse.

    The single place the ordering is expressed, so it is the single place a
    reviewer has to check. Committing before the response is itself deliberate
    (see :func:`app.deps.get_session`); publishing after the commit is what
    makes the announcement true by the time any receiver acts on it.

    If the commit raises, the staged events stay staged and are dropped with
    the request — a caller that recovers from an ``IntegrityError`` (list-name
    conflicts do) therefore cannot leak a frame for work that was rolled back.
    """
    await session.commit()
    await publisher.flush()
