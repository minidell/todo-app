"""Tag wire models and the shared tag-name normalization (master §3.1, §6.4).

Tags are stored **normalized** — trimmed, lowercased, inner whitespace collapsed
— so a plain ``UNIQUE (user_id, name)`` gives case-insensitive uniqueness on
both engines (D-T1/D-D2). Normalizing at the edge means every write, every
lookup and every filter agree on the same string; ``Home``, ``home`` and
``"  home  "`` are one tag, not three.

``normalize_tag`` lives here rather than in the repository because the wire is
where user input arrives, and both the repository and ``app/query.py`` need the
*same* function — two copies would drift and silently split a user's vocabulary.
"""

from __future__ import annotations

import re
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

#: ``tags.name`` is ``String(30)``.
TAG_MAX_LENGTH = 30

#: Master §3.1: a normalized tag starts with a letter or digit and may then
#: contain letters, digits, spaces, underscores and hyphens.
TAG_NAME_PATTERN = r"^[a-z0-9][a-z0-9 _-]{0,29}$"
TAG_NAME_RE = re.compile(TAG_NAME_PATTERN)

TAG_FORMAT_MESSAGE = (
    "tags can use letters, numbers, spaces, hyphens and underscores, must start "
    f"with a letter or number and be at most {TAG_MAX_LENGTH} characters"
)

#: The raw wire type: length is bounded before normalization so a megabyte of
#: whitespace never reaches the normalizer.
RawTagName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=TAG_MAX_LENGTH),
]


def normalize_tag(name: str) -> str:
    """Trim, lowercase and collapse inner whitespace. Never raises."""
    return " ".join(name.strip().lower().split())


def validate_tag(name: str) -> str:
    """Normalize and enforce the documented pattern.

    Raises ``ValueError`` — inside a Pydantic validator that becomes the
    standard 422 body carrying the offending value, so the client sees which
    tag it got wrong rather than a generic rejection.
    """
    normalized = normalize_tag(name)
    if not TAG_NAME_RE.match(normalized):
        raise ValueError(f"invalid tag name {name!r}: {TAG_FORMAT_MESSAGE}")
    return normalized


def normalize_tag_list(names: list[str]) -> list[str]:
    """Validate every name and collapse duplicates, preserving first-seen order.

    Duplicates are collapsed *silently* (spec B2): ``["Home", "home"]`` is one
    tag by construction, not a client error worth a 422.
    """
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(validate_tag(name), None)
    return list(seen)


class TagUpdate(BaseModel):
    """``PATCH /api/tags/{id}`` — renaming is the only supported change."""

    model_config = ConfigDict(extra="forbid")

    name: RawTagName

    @field_validator("name")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return validate_tag(value)


class TagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    #: Top-level todos carrying this tag — computed per request from one
    #: grouped query (never N+1), like ``ListResponse.todo_count``. Subtasks are
    #: excluded so the number matches what filtering by the tag actually
    #: returns: ``?tag=`` matches parents only.
    todo_count: int
