"""Error contract v2 (master §5): every deliberate error carries a `code`."""

from uuid import uuid4

from httpx import AsyncClient

import pytest

from app.errors import (
    REDACTED,
    AppError,
    RequestTooLarge,
    TodoNotFound,
    error_body,
    is_sensitive_location,
    redact_errors,
    scrub_value,
)


async def test_404_body_is_detail_plus_code(client: AsyncClient) -> None:
    response = await client.get(f"/api/todos/{uuid4()}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Todo not found", "code": "todo_not_found"}


async def test_422_body_has_no_code(client: AsyncClient) -> None:
    response = await client.post("/api/todos", json={"title": ""})
    assert response.status_code == 422
    assert "code" not in response.json()


async def test_unknown_route_keeps_the_framework_404_body(client: AsyncClient) -> None:
    """A routing 404 is not an application error and gets no invented code."""
    response = await client.get("/api/nope")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_app_error_serialisation() -> None:
    assert error_body(TodoNotFound()) == {
        "detail": "Todo not found",
        "code": "todo_not_found",
    }
    assert RequestTooLarge.status_code == 413
    assert TodoNotFound.status_code == 404


def test_app_error_detail_can_be_overridden_without_changing_the_code() -> None:
    error = TodoNotFound("Todo not found")
    assert isinstance(error, AppError)
    assert error.code == "todo_not_found"


@pytest.mark.parametrize(
    ("location", "sensitive"),
    [
        (["body", "password"], True),
        (["body", "accounts", 0, "password"], True),  # nested, not just the tail
        (["body", "password", "reason"], True),
        (["body", "email"], False),
        (["body", "title"], False),
        ([], False),
        (None, False),
        ("password", False),  # a bare string loc is not a path
    ],
)
def test_sensitive_locations_are_recognised(location, sensitive: bool) -> None:
    assert is_sensitive_location(location) is sensitive


class _FakeValidationError:
    def __init__(self, errors: list[dict]) -> None:
        self._errors = errors

    def errors(self) -> list[dict]:
        return self._errors


def test_redaction_replaces_only_the_sensitive_input() -> None:
    redacted = redact_errors(
        _FakeValidationError(
            [
                {
                    "type": "string_too_short",
                    "loc": ["body", "password"],
                    "msg": "too short",
                    "input": "hunter2",
                },
                {
                    "type": "value_error",
                    "loc": ["body", "email"],
                    "msg": "not an email",
                    "input": "nope",
                },
                {"type": "missing", "loc": ["body", "password"], "msg": "required"},
            ]
        )
    )

    assert redacted[0]["input"] == REDACTED
    assert redacted[0]["msg"] == "too short"  # everything else survives
    assert redacted[1]["input"] == "nope"
    # An entry that never carried an input does not grow one.
    assert "input" not in redacted[2]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"email": "a@b.c", "password": "hunter2"}, {"email": "a@b.c", "password": REDACTED}),
        ({"Password": "hunter2"}, {"Password": REDACTED}),
        ({"user": {"password": "hunter2"}}, {"user": {"password": REDACTED}}),
        (
            [{"password": "one"}, {"password": "two"}],
            [{"password": REDACTED}, {"password": REDACTED}],
        ),
        ({"title": "Buy milk"}, {"title": "Buy milk"}),
        ("a bare string", "a bare string"),
        (None, None),
    ],
)
def test_scrub_value_replaces_password_values_at_any_depth(value, expected) -> None:
    assert scrub_value(value) == expected


def test_redaction_reaches_a_password_echoed_under_a_harmless_loc() -> None:
    """Pydantic reports a *missing* field with the surrounding object as
    ``input``, so the password rides along under ``loc == ["body", "email"]``."""
    redacted = redact_errors(
        _FakeValidationError(
            [
                {
                    "type": "missing",
                    "loc": ["body", "email"],
                    "msg": "Field required",
                    "input": {"password": "hunter2", "display_name": "Me"},
                },
                {
                    "type": "model_attributes_type",
                    "loc": ["body"],
                    "msg": "Input should be a valid dictionary",
                    "input": {"password": "hunter2"},
                },
            ]
        )
    )

    assert redacted[0]["input"] == {"password": REDACTED, "display_name": "Me"}
    assert redacted[1]["input"] == {"password": REDACTED}


async def test_a_rejected_registration_never_echoes_the_password(
    anon_client: AsyncClient,
) -> None:
    """End to end, on the request most likely to carry a reused password."""
    secret = "correct-horse-battery-staple"

    missing_email = await anon_client.post(
        "/api/auth/register", json={"password": secret}
    )
    assert missing_email.status_code == 422
    assert secret not in missing_email.text

    bad_password = await anon_client.post(
        "/api/auth/register", json={"email": "a@example.com", "password": "pw12"}
    )
    assert bad_password.status_code == 422
    assert "pw12" not in bad_password.text
    assert REDACTED in bad_password.text

    extra_key = await anon_client.post(
        "/api/auth/register",
        json={"email": "a@example.com", "password": secret, "nope": 1},
    )
    assert extra_key.status_code == 422
    assert secret not in extra_key.text
