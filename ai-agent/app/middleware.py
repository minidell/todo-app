"""Pure-ASGI middleware enforcing a request body size limit.

Ported from ``backend/app/middleware.py`` so the internal service has the same
floor as the public one: neither a buggy backend nor anything that gets hold of
the shared secret should be able to make ``ai-agent`` buffer an unbounded body.
Pydantic's per-field limits only apply *after* the whole body has been read, so
they are no defence here.

Includes the master-spec C4 fix: a declared ``Content-Length`` over the limit is
rejected before the body is read, **and** streamed bytes are counted as they
arrive, so a chunked body with no (or a lying) ``Content-Length`` is aborted the
moment it crosses the limit.

Once the limit is crossed the 413 is written immediately and the application is
handed an ``http.disconnect``; anything the application then tries to send is
swallowed, because the response has already been committed.
"""

import json

MAX_BODY_BYTES = 64 * 1024  # 64 KiB

_REJECTION_BODY = json.dumps(
    {"detail": "Request body too large", "code": "request_too_large"}
).encode("utf-8")


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = self._get_content_length(scope)
        if content_length is not None and content_length > self.max_bytes:
            await self._reject(send)
            return

        rejected = False
        received = 0

        async def counting_receive():
            nonlocal rejected, received
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    rejected = True
                    await self._reject(send)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message) -> None:
            # We already committed a 413; drop whatever the app produces after
            # seeing the disconnect (typically a 500 from the error middleware).
            if not rejected:
                await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            # A disconnect mid-body surfaces as ClientDisconnect (or whatever the
            # framework raises). Only swallow it when we are the cause.
            if not rejected:
                raise

    @staticmethod
    def _get_content_length(scope) -> int | None:
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None

    @staticmethod
    async def _reject(send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_REJECTION_BODY)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _REJECTION_BODY})
