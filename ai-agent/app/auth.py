"""Shared-secret authentication for the internal AI service (slice-4 spec B5).

``ai-agent`` is never exposed to browsers (master D-AI2): only the backend calls
it, over the compose network, carrying ``X-Internal-Token``. The comparison uses
``hmac.compare_digest`` so a wrong token cannot be discovered byte by byte, and
the token itself is never logged or echoed.

The comparison runs on **bytes**, not on ``str``: ``hmac.compare_digest`` raises
``TypeError`` for a ``str`` containing a code point above U+007F, and Starlette
decodes headers as latin-1, so any non-ASCII byte in the header would otherwise
turn an unauthenticated request into a 500.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header

from app.config import Settings
from app.deps import get_app_settings
from app.errors import unauthorized


def _as_bytes(value: str) -> bytes:
    """Encode a header/secret for a constant-time comparison.

    ``surrogateescape`` round-trips the lone surrogates that latin-1 header
    decoding can produce instead of raising on them.
    """
    return value.encode("utf-8", "surrogateescape")


async def verify_internal_token(
    settings: Annotated[Settings, Depends(get_app_settings)],
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    """Reject any ``/ai/*`` request without the exact shared secret."""
    if x_internal_token is None or not hmac.compare_digest(
        _as_bytes(x_internal_token), _as_bytes(settings.AI_AGENT_TOKEN)
    ):
        raise unauthorized()
