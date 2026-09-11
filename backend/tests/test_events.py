"""The broker and the SSE framing (slice-4 spec A6.1).

Pure unit tests: no app, no database. The properties pinned here — per-user
isolation, bounded queues, unbreakable framing — are the ones a realtime bug
would otherwise surface as "sometimes another account's todo appears in my
tab", which is not a thing a test should be discovering late.
"""

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from uuid import uuid4

import pytest

from app.events import (
    CLOSE_SENTINEL,
    DEFAULT_MAX_STREAMS_PER_USER,
    QUEUE_MAXSIZE,
    Event,
    EventBroker,
    EventPublisher,
    format_sse,
    list_deleted_event,
    ready_event,
    tag_created_event,
    tag_deleted_event,
    tag_updated_event,
    todo_created_event,
    todo_deleted_event,
    todo_updated_event,
)


def an_event(name: str = "todo.created", **data) -> Event:
    return Event(name, {"origin": None, **data})


# --- framing ---------------------------------------------------------------


def test_format_sse_produces_one_named_frame() -> None:
    frame = format_sse(Event("todo.created", {"origin": None, "todo": {"id": "x"}}))
    assert frame == 'event: todo.created\ndata: {"origin":null,"todo":{"id":"x"}}\n\n'


def test_a_newline_in_the_payload_cannot_break_the_framing() -> None:
    """A title with a newline must not turn one frame into two.

    This is the failure mode the compact-JSON rule exists for: an unescaped
    newline inside ``data:`` ends the line, and the remainder is parsed as a
    new (bogus) field or a new frame entirely.
    """
    frame = format_sse(
        Event("todo.updated", {"origin": None, "todo": {"title": "line one\nline two"}})
    )

    body = frame.removesuffix("\n\n")
    assert body.count("\n") == 1, body  # exactly the split between event: and data:
    name_line, data_line = body.split("\n")
    assert name_line == "event: todo.updated"
    assert data_line.startswith("data: ")
    assert json.loads(data_line.removeprefix("data: "))["todo"]["title"] == (
        "line one\nline two"
    )


def test_format_sse_serialises_values_json_does_not_know() -> None:
    """Ids and timestamps reach the builders as objects; framing must not 500."""
    frame = format_sse(todo_deleted_event(uuid4(), uuid4(), None))
    payload = json.loads(frame.split("data: ", 1)[1])
    assert set(payload) == {"origin", "id", "list_id"}


# --- builders --------------------------------------------------------------


def test_origin_is_top_level_in_every_frame() -> None:
    """The frontend reads exactly one field for echo suppression (§7.2)."""
    client_id = str(uuid4())
    todo = {"id": str(uuid4()), "title": "Write the spec"}

    frames = [
        todo_created_event(todo, client_id),
        todo_updated_event(todo, client_id),
        todo_deleted_event(uuid4(), uuid4(), client_id),
        list_deleted_event(uuid4(), client_id),
    ]

    for event in frames:
        assert event.data["origin"] == client_id
        # Never nested inside the entity — a client would have to know the
        # frame's shape to find it.
        assert "origin" not in event.data.get("todo", {})


def test_a_missing_client_id_becomes_a_null_origin() -> None:
    assert todo_created_event({"id": "1"}, None).data["origin"] is None


def test_ready_event_names_the_user() -> None:
    user_id = uuid4()
    event = ready_event(user_id)
    assert event.name == "ready"
    assert event.data == {"user_id": str(user_id)}


# --- broker ----------------------------------------------------------------


async def test_a_subscriber_receives_published_events() -> None:
    broker = EventBroker()
    user_id = uuid4()

    async with broker.subscribe(user_id) as queue:
        await broker.publish(user_id, an_event())
        assert queue.get_nowait().name == "todo.created"


async def test_subscribe_registers_and_unsubscribes() -> None:
    broker = EventBroker()
    user_id = uuid4()
    assert broker.subscriber_count == 0

    async with broker.subscribe(user_id):
        assert broker.subscriber_count == 1
        async with broker.subscribe(user_id):
            assert broker.subscriber_count == 2

    assert broker.subscriber_count == 0
    # The per-user set is removed too, not left behind empty.
    assert broker.subscriber_count_for(user_id) == 0


async def test_unsubscribing_survives_an_exception_in_the_body() -> None:
    """A disconnect arrives as CancelledError; the queue must still go."""
    broker = EventBroker()
    user_id = uuid4()

    with pytest.raises(asyncio.CancelledError):
        async with broker.subscribe(user_id):
            raise asyncio.CancelledError()

    assert broker.subscriber_count == 0


async def test_events_for_one_user_never_reach_another() -> None:
    broker = EventBroker()
    alice, bob = uuid4(), uuid4()

    async with broker.subscribe(alice) as alice_queue:
        async with broker.subscribe(bob) as bob_queue:
            await broker.publish(alice, an_event(todo={"title": "Alice's secret"}))

            assert alice_queue.qsize() == 1
            assert bob_queue.qsize() == 0


async def test_publishing_to_a_user_with_no_subscribers_is_a_no_op() -> None:
    broker = EventBroker()
    await broker.publish(uuid4(), an_event())  # must not raise


async def test_a_full_queue_drops_only_that_subscriber() -> None:
    broker = EventBroker()
    user_id = uuid4()

    async with broker.subscribe(user_id) as stalled:
        async with broker.subscribe(user_id) as healthy:
            for _ in range(QUEUE_MAXSIZE):
                await broker.publish(user_id, an_event())
            assert stalled.full()

            # Keep the healthy subscriber draining, as a live stream would.
            while not healthy.empty():
                healthy.get_nowait()

            await broker.publish(user_id, an_event(todo={"title": "one more"}))

            assert broker.subscriber_count_for(user_id) == 1
            assert healthy.get_nowait().data["todo"] == {"title": "one more"}


async def test_a_dropped_subscriber_is_told_to_stop() -> None:
    """The eviction must end the stream, not park it until the next keep-alive."""
    broker = EventBroker()
    user_id = uuid4()

    async with broker.subscribe(user_id) as queue:
        for _ in range(QUEUE_MAXSIZE + 1):
            await broker.publish(user_id, an_event())

        frames = []
        while not queue.empty():
            frames.append(queue.get_nowait())

        assert frames[-1] is CLOSE_SENTINEL
        assert broker.subscriber_count == 0


async def test_a_dropped_subscriber_does_not_grow_beyond_its_bound() -> None:
    broker = EventBroker()
    user_id = uuid4()

    async with broker.subscribe(user_id) as queue:
        for _ in range(QUEUE_MAXSIZE * 3):
            await broker.publish(user_id, an_event())

        assert queue.qsize() <= QUEUE_MAXSIZE


# --- per-user stream cap ---------------------------------------------------


async def test_the_stream_cap_defaults_to_eight() -> None:
    assert EventBroker().max_streams_per_user == DEFAULT_MAX_STREAMS_PER_USER == 8


async def test_a_cap_below_one_is_refused() -> None:
    """A zero cap would evict every stream the moment it connected."""
    with pytest.raises(ValueError, match="max_streams_per_user"):
        EventBroker(0)


async def test_the_oldest_stream_is_evicted_past_the_cap() -> None:
    broker = EventBroker(max_streams_per_user=3)
    user_id = uuid4()

    async with broker.subscribe(user_id) as first:
        async with broker.subscribe(user_id) as second:
            async with broker.subscribe(user_id) as third:
                assert broker.subscriber_count_for(user_id) == 3

                async with broker.subscribe(user_id) as fourth:
                    # The newest tab wins: it is the one the user is looking at.
                    assert broker.subscriber_count_for(user_id) == 3
                    assert first.get_nowait() is CLOSE_SENTINEL

                    await broker.publish(user_id, an_event())
                    assert second.qsize() == 1
                    assert third.qsize() == 1
                    assert fourth.qsize() == 1
                    # The evicted one received nothing but its sentinel.
                    assert first.empty()


async def test_the_subscriber_count_never_exceeds_the_cap() -> None:
    broker = EventBroker(max_streams_per_user=4)
    user_id = uuid4()
    counts: list[int] = []

    async with AsyncExitStack() as stack:
        for _ in range(25):
            await stack.enter_async_context(broker.subscribe(user_id))
            counts.append(broker.subscriber_count_for(user_id))

    assert max(counts) == 4
    assert broker.subscriber_count == 0


async def test_the_cap_is_per_user_not_global() -> None:
    broker = EventBroker(max_streams_per_user=2)
    alice, bob = uuid4(), uuid4()

    async with AsyncExitStack() as stack:
        for _ in range(2):
            await stack.enter_async_context(broker.subscribe(alice))
            await stack.enter_async_context(broker.subscribe(bob))

        assert broker.subscriber_count_for(alice) == 2
        assert broker.subscriber_count_for(bob) == 2
        assert broker.subscriber_count == 4


async def test_evicting_the_oldest_frees_a_slot_even_when_its_queue_is_full() -> None:
    """A stalled tab is exactly the kind that gets evicted; it must still exit."""
    broker = EventBroker(max_streams_per_user=1)
    user_id = uuid4()

    async with broker.subscribe(user_id) as stalled:
        for _ in range(QUEUE_MAXSIZE):
            await broker.publish(user_id, an_event())
        assert stalled.full()

        async with broker.subscribe(user_id):
            frames = []
            while not stalled.empty():
                frames.append(stalled.get_nowait())
            assert frames[-1] is CLOSE_SENTINEL


# --- publisher -------------------------------------------------------------


async def test_staged_events_are_invisible_until_flushed() -> None:
    broker = EventBroker()
    user_id = uuid4()
    publisher = EventPublisher(broker, user_id, origin=None)

    async with broker.subscribe(user_id) as queue:
        publisher.stage_todo_created({"id": "1"})
        assert queue.qsize() == 0
        assert len(publisher.staged) == 1

        await publisher.flush()
        assert queue.get_nowait().name == "todo.created"


async def test_flush_is_idempotent() -> None:
    """A second flush must not re-send: events are not "at least once" here."""
    broker = EventBroker()
    user_id = uuid4()
    publisher = EventPublisher(broker, user_id, origin=None)

    async with broker.subscribe(user_id) as queue:
        publisher.stage_todo_created({"id": "1"})
        await publisher.flush()
        await publisher.flush()

        assert queue.qsize() == 1


async def test_the_publisher_stamps_its_origin_on_everything_it_stages() -> None:
    broker = EventBroker()
    user_id = uuid4()
    client_id = str(uuid4())
    publisher = EventPublisher(broker, user_id, origin=client_id)

    publisher.stage_todo_created({"id": "1"})
    publisher.stage_todo_updated({"id": "1"})
    publisher.stage_todo_deleted(uuid4(), uuid4())
    publisher.stage_list_created({"id": "2"})
    publisher.stage_list_updated({"id": "2"})
    publisher.stage_list_deleted(uuid4())
    publisher.stage_tag_created({"id": "3"})
    publisher.stage_tag_updated({"id": "3"})
    publisher.stage_tag_deleted(uuid4())

    assert [event.data["origin"] for event in publisher.staged] == [client_id] * 9
    assert [event.name for event in publisher.staged] == [
        "todo.created",
        "todo.updated",
        "todo.deleted",
        "list.created",
        "list.updated",
        "list.deleted",
        "tag.created",
        "tag.updated",
        "tag.deleted",
    ]


def test_the_tag_frames_carry_origin_outside_the_entity() -> None:
    """§7.2: ``origin`` is top level on every frame, never inside the payload,
    so a client checks one field without knowing which frame it holds."""
    tag = {"id": "1", "name": "errand", "todo_count": 1}
    tag_id = uuid4()

    assert tag_created_event(tag, "abc").data == {"origin": "abc", "tag": tag}
    assert tag_updated_event(tag, None).data == {"origin": None, "tag": tag}
    assert tag_deleted_event(tag_id, "abc").data == {
        "origin": "abc",
        "id": str(tag_id),
    }
    assert "origin" not in tag


async def test_discard_drops_staged_events() -> None:
    broker = EventBroker()
    user_id = uuid4()
    publisher = EventPublisher(broker, user_id, origin=None)

    async with broker.subscribe(user_id) as queue:
        publisher.stage_todo_created({"id": "1"})
        publisher.discard()
        await publisher.flush()

        assert queue.qsize() == 0


# --- the single-replica notice (IT3-4) -------------------------------------


async def test_the_startup_line_names_the_single_replica_constraint(
    test_settings,
) -> None:
    """Criterion 21. The constraint is invisible otherwise, and the way it
    breaks — some tabs quietly stop updating — is not one an operator would
    trace back to a ``--scale backend=N``.

    Deliberately **not** ``caplog.at_level``: forcing the level is what let
    this line be silently dropped in the container for a whole iteration (QA,
    iteration 4). The record has to arrive at the level ``create_app``
    configured, or the test is testing pytest rather than the application.
    """
    from app.main import create_app

    records: list[logging.LogRecord] = []

    class Recorder(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    application = create_app(test_settings)

    handler = Recorder()
    logging.getLogger("app").addHandler(handler)
    try:
        async with application.router.lifespan_context(application):
            pass
    finally:
        logging.getLogger("app").removeHandler(handler)

    notices = [record for record in records if "REALTIME_BACKEND" in record.getMessage()]
    assert len(notices) == 1
    assert notices[0].levelno == logging.INFO
    assert "REALTIME_BACKEND=memory" in notices[0].getMessage()
    assert "ONE backend replica" in notices[0].getMessage()
