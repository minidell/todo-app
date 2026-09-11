"""Pure-ASGI middleware enforcing a request body size limit.

Carry-over C4: checking ``Content-Length`` alone is not enough — a chunked
request declares no length at all, and a hostile client can simply lie. The
middleware therefore keeps the cheap header rejection *and* counts the bytes
it actually forwards, aborting with 413 as soon as the running total exceeds
the limit.

Request side only: responses are never touched, so streaming responses (SSE in
slice 4) flow freely.
"""

import json

from app.errors import RequestTooLarge, error_body

MAX_BODY_BYTES = 64 * 1024  # 64 KiB

_REJECTION_BODY = json.dumps(error_body(RequestTooLarge())).encode("utf-8")


class _BodyTooLarge(Exception):
    """Raised from the wrapped ``receive`` once the limit is exceeded."""


def _contains_body_too_large(exc: BaseException) -> bool:
    if isinstance(exc, _BodyTooLarge):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_body_too_large(sub) for sub in exc.exceptions)
    return False


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
            # Fast path: reject before a single body byte is read off the wire.
            await self._reject(send)
            return

        received = 0
        too_large = False
        rejected = False

        async def counting_receive():
            nonlocal received, too_large
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b"") or b"")
                if received > self.max_bytes:
                    too_large = True
                    raise _BodyTooLarge()
            return message

        async def guarding_send(message):
            # Once the limit is blown, the application's own response is
            # discarded and replaced by the 413. FastAPI turns *any* failure
            # while reading the body into a 400 "error parsing the body", so
            # relying on the exception alone would leak the wrong status.
            nonlocal rejected
            if too_large:
                if not rejected:
                    rejected = True
                    await self._reject(send)
                return
            await send(message)

        try:
            await self.app(scope, counting_receive, guarding_send)
        except BaseException as exc:  # noqa: BLE001 - re-raised unless it's ours
            if not _contains_body_too_large(exc):
                raise
        if too_large and not rejected:
            rejected = True
            await self._reject(send)

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
                "status": RequestTooLarge.status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(_REJECTION_BODY)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": _REJECTION_BODY})
