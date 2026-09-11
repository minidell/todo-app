"""``GET /api/events`` — the Server-Sent Events stream (master §7.1, D-R1).

SSE rather than WebSocket because the requirement is strictly one-way: it rides
on ordinary HTTP/1.1, so no proxy needs ``Upgrade`` handling, there is no second
protocol to authenticate, and a dropped stream degrades to "this tab is stale"
instead of a broken app.

**The token is never in the URL.** Native ``EventSource`` cannot set headers,
which is why the frontend opens this stream with ``fetch()`` and an
``Authorization`` header instead. There is deliberately no ``?token=`` query
parameter: a credential in a URL lands in access logs, proxy logs, browser
history and ``Referer`` headers, and adding one here would undo that for the
one endpoint a browser holds open for hours.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.db.models import User
from app.deps import get_broker, get_current_user, get_token_expiry
from app.events import (
    HEARTBEAT_SECONDS,
    KEEPALIVE_FRAME,
    EventBroker,
    format_sse,
    ready_event,
)
from app.schemas.common import ErrorResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/events", tags=["events"])

CurrentUser = Annotated[User, Depends(get_current_user)]
Broker = Annotated[EventBroker, Depends(get_broker)]
TokenExpiry = Annotated[datetime, Depends(get_token_expiry)]

#: Sent instead of a keep-alive when the token behind the stream has run out.
#: A comment frame, so an SSE parser ignores it: the *close* is the signal, and
#: the client's ordinary reconnect handles it. It exists to make the reason
#: visible in a packet capture or a curl session.
EXPIRY_FRAME = ": bye\n\n"

#: Headers that keep a stream a stream. ``X-Accel-Buffering: no`` is the one
#: that matters in front of nginx: without it the proxy buffers the response
#: and the browser receives nothing until the connection closes, which looks
#: exactly like a backend that never sends events.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def event_stream(
    request: Request,
    broker: EventBroker,
    user: User,
    deadline: datetime,
    *,
    heartbeat_seconds: float | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames for ``user`` until the client — or its token — goes away.

    ``ready`` goes out first and unconditionally, for two reasons: it proves to
    the client that the stream is authenticated and live (so its reconnect
    backoff can reset), and it is the client's cue to refetch once — which is
    what closes the gap of events missed while it was disconnected.

    The loop then races the queue against the heartbeat. A comment frame is not
    decoration: it is the only way to notice a client that vanished without a
    FIN, because writing to a dead socket is what raises.

    **The stream never outlives the token that opened it.** Authentication on a
    long-lived connection happens once, at the start; without ``deadline`` a
    stream opened at 09:00 with a 60-minute token would still be delivering that
    user's data at 17:00, and letting a token expire would stop revoking
    anything. At the deadline the generator closes cleanly with a ``: bye``
    comment — the client's existing reconnect path then opens a new stream with
    a fresh token, so the user sees nothing.

    Nothing here touches the database. The session dependency behind
    ``get_current_user`` is function-scoped and has already been closed by the
    time this generator runs, so a browser holding a stream open for hours does
    not hold a connection out of the pool for hours.

    ``heartbeat_seconds`` is read from the module at call time rather than
    bound as a default, so a test can shorten it without waiting 25 real
    seconds to see one comment frame.
    """
    interval = HEARTBEAT_SECONDS if heartbeat_seconds is None else heartbeat_seconds
    async with broker.subscribe(user.id) as queue:
        yield format_sse(ready_event(user.id))
        try:
            while True:
                # Checked every iteration, not only after a heartbeat: a client
                # that connects and never reads would otherwise sit in the
                # broker's registry — holding a queue and a slot against the
                # per-user cap — until the next comment frame failed to write.
                if await request.is_disconnected():
                    break

                remaining = (deadline - _now()).total_seconds()
                if remaining <= 0:
                    logger.debug(
                        "Ending the event stream of user %s: token expired", user.id
                    )
                    yield EXPIRY_FRAME
                    break

                try:
                    event = await asyncio.wait_for(
                        queue.get(), timeout=min(interval, remaining)
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    # Either the heartbeat or the deadline fired; the top of the
                    # next iteration decides which, so this only has to avoid
                    # sending a keep-alive to a stream that is about to close.
                    if (deadline - _now()).total_seconds() > 0:
                        yield KEEPALIVE_FRAME
                    continue

                if event is None:
                    # The broker evicted this subscriber (it fell too far behind,
                    # or a newer tab of the same user took its slot). End the
                    # stream; the client reconnects, gets a fresh `ready`, and
                    # refetches — §7.4.
                    logger.info(
                        "Ending the event stream of user %s: subscriber evicted",
                        user.id,
                    )
                    break
                yield format_sse(event)
        except asyncio.CancelledError:
            # The normal way a stream ends: the client closed the connection and
            # the server cancelled this task. Logged at debug — a disconnect is
            # not an error — and then **re-raised**, because swallowing a
            # cancellation is how a task survives its own cancel(): anyio's
            # cancel scope would have to re-deliver it, and the scope Starlette
            # runs this under expects it to propagate. Unsubscribing does not
            # depend on catching it either way; ``subscribe``'s ``finally``
            # runs as the exception unwinds.
            logger.debug("Event stream cancelled for user %s", user.id)
            raise


@router.get(
    "",
    responses={
        200: {
            "content": {"text/event-stream": {}},
            "description": "A live event stream for the authenticated user.",
        },
        401: {"model": ErrorResponse, "description": "Not authenticated"},
    },
)
async def stream_events(
    request: Request, user: CurrentUser, broker: Broker, expires_at: TokenExpiry
) -> StreamingResponse:
    """Open the caller's event stream.

    Authentication happens here, in the ordinary dependency, *before* the
    response starts: an unauthenticated caller gets a plain 401 JSON body
    rather than a stream that immediately closes, which is what makes the
    frontend's global session-expiry path work for this endpoint too.
    """
    return StreamingResponse(
        event_stream(request, broker, user, expires_at),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
