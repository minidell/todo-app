"""Helpers shared by the routers."""

from uuid import UUID


def parse_uuid(value: str | None) -> UUID | None:
    """Parse an id from a path or query string, or ``None`` if it is not one.

    Iteration-1 decision D1: ids arrive as ``str`` and a malformed one is
    reported as "not found", never 422. A caller therefore cannot tell a
    syntactically invalid id from one that exists but is not theirs (D-E2).
    """
    if value is None:
        return None
    try:
        return UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
