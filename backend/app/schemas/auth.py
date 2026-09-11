"""Auth wire models (master §6.1, slice-2 spec B5).

``UserResponse`` is the only user-shaped thing that ever reaches the network,
and it has no ``password_hash`` field — the hash cannot leak through it even if
a router hands it a whole ORM entity. A test asserts that field set.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints, field_validator

#: Storage limit from master §3.1 (`users.email` is `String(320)`).
MAX_EMAIL_LENGTH = 320
#: NIST SP 800-63B: length is the only composition rule.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

Password = Annotated[
    str,
    StringConstraints(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH),
]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]


def normalize_email_value(value: str) -> str:
    """Trim + lowercase, and enforce the column width.

    Emails are stored normalized so a plain ``UNIQUE`` index gives
    case-insensitive uniqueness on both engines (D-T1); normalizing at the edge
    means every lookup and every insert agree on the same string.
    """
    normalized = value.strip().lower()
    if len(normalized) > MAX_EMAIL_LENGTH:
        raise ValueError(f"email must be at most {MAX_EMAIL_LENGTH} characters")
    return normalized


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    password: Password
    display_name: DisplayName | None = None

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email_value(value)

    @field_validator("password")
    @classmethod
    def _reject_blank_password(cls, value: str) -> str:
        # " " * 8 satisfies the length rule but is not a password.
        if not value.strip():
            raise ValueError("password must contain a non-whitespace character")
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    # No minimum here: rejecting a short password at login with 422 would tell
    # an attacker the difference between "malformed" and "wrong", and would
    # break accounts created under an older policy.
    password: Annotated[str, StringConstraints(min_length=1, max_length=MAX_PASSWORD_LENGTH)]

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, value: str) -> str:
        return normalize_email_value(value)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str | None
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    #: Seconds, matching ``JWT_EXPIRES_MINUTES``.
    expires_in: int
    user: UserResponse
