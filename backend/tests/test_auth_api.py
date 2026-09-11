"""API tests for ``/api/auth`` (master §6.1, slice-2 spec B9.1–B9.3)."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import jwt
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.models import TodoList, User
from app.security import JWT_ALGORITHM, verify_password
from tests.conftest import TEST_PASSWORD

REGISTER = "/api/auth/register"
LOGIN = "/api/auth/login"
ME = "/api/auth/me"

UNAUTHORIZED_BODY = {"detail": "Not authenticated", "code": "unauthorized"}
INVALID_CREDENTIALS_BODY = {
    "detail": "Invalid email or password",
    "code": "invalid_credentials",
}

USER_RESPONSE_KEYS = {"id", "email", "display_name", "created_at"}


def registration(**overrides) -> dict:
    return {"email": "new@example.com", "password": "correct horse battery"} | overrides


# --- register --------------------------------------------------------------
async def test_register_returns_201_and_the_user(anon_client: AsyncClient) -> None:
    response = await anon_client.post(
        REGISTER, json=registration(display_name="  Me  ")
    )
    assert response.status_code == 201
    body = response.json()

    assert set(body) == USER_RESPONSE_KEYS
    assert body["email"] == "new@example.com"
    assert body["display_name"] == "Me"
    assert body["created_at"].endswith("Z")


async def test_register_never_leaks_the_password(anon_client: AsyncClient) -> None:
    response = await anon_client.post(REGISTER, json=registration())
    assert "password" not in response.text
    assert "password_hash" not in response.text
    assert "argon2" not in response.text


async def test_register_stores_an_argon2_hash_not_the_password(
    anon_client: AsyncClient, db_session
) -> None:
    await anon_client.post(REGISTER, json=registration())

    stored = (
        await db_session.execute(select(User).where(User.email == "new@example.com"))
    ).scalar_one()
    assert stored.password_hash.startswith("$argon2id$")
    assert "correct horse battery" not in stored.password_hash
    assert await verify_password("correct horse battery", stored.password_hash) is True


async def test_register_creates_an_inbox_default_list(
    anon_client: AsyncClient, db_session
) -> None:
    created = (await anon_client.post(REGISTER, json=registration())).json()

    rows = (
        await db_session.execute(
            select(TodoList).where(TodoList.user_id == UUID(created["id"]))
        )
    ).scalars().all()
    assert [(row.name, row.is_default) for row in rows] == [("Inbox", True)]


async def test_register_normalizes_the_email(anon_client: AsyncClient) -> None:
    body = (
        await anon_client.post(REGISTER, json=registration(email="  MiXeD@Example.COM "))
    ).json()
    assert body["email"] == "mixed@example.com"


@pytest.mark.parametrize(
    "duplicate",
    ["new@example.com", "NEW@example.com", "  new@Example.com  "],
)
async def test_register_duplicate_email_is_409(
    anon_client: AsyncClient, duplicate: str
) -> None:
    assert (await anon_client.post(REGISTER, json=registration())).status_code == 201

    response = await anon_client.post(REGISTER, json=registration(email=duplicate))
    assert response.status_code == 409
    assert response.json() == {
        "detail": "That email is already registered",
        "code": "email_taken",
    }


async def test_register_translates_a_unique_violation_into_409(
    anon_client: AsyncClient, monkeypatch
) -> None:
    """The pre-check can lose a race; the UNIQUE index is the real decider."""
    from app.repositories.users import SqlAlchemyUserRepository

    assert (await anon_client.post(REGISTER, json=registration())).status_code == 201

    async def blind(self, email: str):  # the "nobody has this email" answer
        return None

    monkeypatch.setattr(SqlAlchemyUserRepository, "get_by_email", blind)

    response = await anon_client.post(REGISTER, json=registration())
    assert response.status_code == 409
    assert response.json()["code"] == "email_taken"


@pytest.mark.parametrize(
    "payload",
    [
        registration(password="short12"),  # 7 characters
        registration(password="x" * 129),
        registration(password="         "),  # whitespace only
        registration(email="not-an-email"),
        registration(email=""),
        {"email": "new@example.com"},  # no password
        {"password": "correct horse battery"},  # no email
        registration(nope=1),  # extra="forbid" (D-E3)
        registration(display_name="x" * 101),
        registration(display_name="   "),
    ],
)
async def test_register_validation_failures_are_422(
    anon_client: AsyncClient, payload: dict
) -> None:
    response = await anon_client.post(REGISTER, json=payload)
    assert response.status_code == 422, payload
    assert "code" not in response.json()


@pytest.mark.parametrize(
    ("path", "payload", "secret"),
    [
        (REGISTER, registration(password="short12"), "short12"),
        (REGISTER, registration(password="   " * 3), "   " * 3),
        (REGISTER, registration(password="x" * 129), "x" * 129),
        (REGISTER, {**registration(), "password": ["hunter2"]}, "hunter2"),
        (LOGIN, {"email": "a@example.com", "password": ""}, None),
        (LOGIN, {"email": "a@example.com", "password": "y" * 129}, "y" * 129),
    ],
)
async def test_422_never_echoes_the_submitted_password(
    anon_client: AsyncClient, path: str, payload: dict, secret: str | None
) -> None:
    """Pydantic reports the rejected value in ``input``; for a password that
    would send the plaintext straight back to the client (and its logs)."""
    response = await anon_client.post(path, json=payload)
    assert response.status_code == 422
    if secret is not None:
        assert secret not in response.text

    password_errors = [
        error for error in response.json()["detail"] if "password" in error["loc"]
    ]
    assert password_errors, response.text
    for error in password_errors:
        assert error.get("input", "[redacted]") == "[redacted]"


async def test_422_still_reports_non_sensitive_inputs(
    anon_client: AsyncClient,
) -> None:
    """Only the password is redacted — the rest of the developer-facing detail
    is untouched, so the frozen 422 shape keeps its diagnostic value."""
    response = await anon_client.post(REGISTER, json=registration(email="nope"))
    assert response.status_code == 422

    email_errors = [
        error for error in response.json()["detail"] if "email" in error["loc"]
    ]
    assert email_errors
    assert email_errors[0]["input"] == "nope"


async def test_422_body_shape_is_unchanged_by_redaction(
    anon_client: AsyncClient,
) -> None:
    response = await anon_client.post(REGISTER, json=registration(password="short"))
    body = response.json()

    assert set(body) == {"detail"}  # no "code" on a 422 (D-E1)
    assert isinstance(body["detail"], list)
    for error in body["detail"]:
        assert {"loc", "msg", "type"} <= set(error)


async def test_register_accepts_an_8_character_password(
    anon_client: AsyncClient,
) -> None:
    assert (
        await anon_client.post(REGISTER, json=registration(password="12345678"))
    ).status_code == 201


# --- login -----------------------------------------------------------------
async def test_login_returns_a_usable_token(
    anon_client: AsyncClient, user: User, test_settings
) -> None:
    response = await anon_client.post(
        LOGIN, json={"email": user.email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["token_type"] == "bearer"
    assert body["expires_in"] == test_settings.JWT_EXPIRES_MINUTES * 60
    assert body["user"]["id"] == str(user.id)
    assert set(body["user"]) == USER_RESPONSE_KEYS

    payload = jwt.decode(
        body["access_token"], test_settings.JWT_SECRET, algorithms=[JWT_ALGORITHM]
    )
    assert payload["sub"] == str(user.id)

    me = await anon_client.get(
        ME, headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["email"] == user.email


async def test_login_is_case_insensitive_in_the_email(
    anon_client: AsyncClient, user: User
) -> None:
    response = await anon_client.post(
        LOGIN, json={"email": "  A@Example.COM  ", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200


async def test_unknown_email_and_wrong_password_are_indistinguishable(
    anon_client: AsyncClient, user: User
) -> None:
    """No enumeration oracle: both answers are byte-identical."""
    wrong_password = await anon_client.post(
        LOGIN, json={"email": user.email, "password": "not my password"}
    )
    unknown_email = await anon_client.post(
        LOGIN, json={"email": "nobody@example.com", "password": TEST_PASSWORD}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.content == unknown_email.content
    assert wrong_password.json() == INVALID_CREDENTIALS_BODY
    assert "WWW-Authenticate" not in wrong_password.headers


async def test_login_does_not_leak_the_token_of_another_account(
    anon_client: AsyncClient, user: User, other_user: User, test_settings
) -> None:
    body = (
        await anon_client.post(
            LOGIN, json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).json()
    payload = jwt.decode(
        body["access_token"], test_settings.JWT_SECRET, algorithms=[JWT_ALGORITHM]
    )
    assert payload["sub"] == str(user.id) != str(other_user.id)


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "a@example.com"},
        {"password": "x"},
        {"email": "not-an-email", "password": "x"},
        {"email": "a@example.com", "password": ""},
        {"email": "a@example.com", "password": TEST_PASSWORD, "extra": 1},
    ],
)
async def test_login_validation_failures_are_422(
    anon_client: AsyncClient, payload: dict
) -> None:
    assert (await anon_client.post(LOGIN, json=payload)).status_code == 422, payload


async def test_login_upgrades_a_hash_written_with_weaker_parameters(
    anon_client: AsyncClient, db_session, monkeypatch
) -> None:
    """Login is the only moment the plaintext exists, so it is where a stale
    hash gets rewritten with today's parameters.

    The suite runs the fast argon2 profile (risk R3), under which the upgrade
    is deliberately suppressed — rewriting there would *downgrade* a real hash
    to test strength. The profile check is therefore pinned to "off" for this
    test, which is exactly what a production process reports.
    """
    from argon2 import PasswordHasher

    from app.repositories.users import SqlAlchemyUserRepository
    from app.security import needs_rehash

    monkeypatch.setattr("app.routers.auth.using_fast_hashing", lambda: False)

    weak_hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    created = await SqlAlchemyUserRepository(db_session).create(
        "legacy@example.com", weak_hasher.hash(TEST_PASSWORD), None
    )
    user_id, weak_hash = created.id, created.password_hash
    await db_session.commit()
    assert needs_rehash(weak_hash) is True

    response = await anon_client.post(
        LOGIN, json={"email": "legacy@example.com", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200

    stored = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    await db_session.refresh(stored)
    assert stored.password_hash != weak_hash
    assert needs_rehash(stored.password_hash) is False
    # The upgrade must not lock the owner out.
    assert (
        await anon_client.post(
            LOGIN, json={"email": "legacy@example.com", "password": TEST_PASSWORD}
        )
    ).status_code == 200


async def test_login_never_downgrades_a_strong_hash_under_the_fast_profile(
    anon_client: AsyncClient, db_session
) -> None:
    """A dev flag must not be able to weaken a stored credential.

    ``check_needs_rehash`` answers "these parameters differ from mine", not
    "these are weaker", so under the fast test profile every production-strength
    hash looks stale. Rewriting one would replace a 64 MiB argon2 hash with an
    8 MiB one — permanently, in the real password column — because someone ran
    the process with ``TEST_ARGON2_FAST`` set.
    """
    from argon2 import PasswordHasher

    from app.repositories.users import SqlAlchemyUserRepository
    from app.security import (
        ARGON2_MEMORY_COST,
        ARGON2_PARALLELISM,
        ARGON2_TIME_COST,
        needs_rehash,
        using_fast_hashing,
    )

    assert using_fast_hashing() is True, "the suite runs the fast profile (R3)"

    strong = PasswordHasher(
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
    ).hash(TEST_PASSWORD)
    created = await SqlAlchemyUserRepository(db_session).create(
        "strong@example.com", strong, None
    )
    user_id = created.id
    await db_session.commit()
    # The naive check would say "rewrite this" — in the downgrading direction.
    assert needs_rehash(strong) is True

    response = await anon_client.post(
        LOGIN, json={"email": "strong@example.com", "password": TEST_PASSWORD}
    )
    assert response.status_code == 200

    stored = (
        await db_session.execute(select(User).where(User.id == user_id))
    ).scalar_one()
    await db_session.refresh(stored)
    assert stored.password_hash == strong


async def test_login_leaves_a_current_hash_alone(
    anon_client: AsyncClient, db_session, user: User
) -> None:
    user_id, before = user.id, user.password_hash

    assert (
        await anon_client.post(
            LOGIN, json={"email": user.email, "password": TEST_PASSWORD}
        )
    ).status_code == 200

    stored = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    await db_session.refresh(stored)
    assert stored.password_hash == before


async def test_a_failed_login_never_rewrites_the_hash(
    anon_client: AsyncClient, db_session
) -> None:
    """Only a *verified* password may be re-derived — otherwise a guess could
    overwrite the stored credential."""
    from argon2 import PasswordHasher

    from app.repositories.users import SqlAlchemyUserRepository

    weak_hasher = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    created = await SqlAlchemyUserRepository(db_session).create(
        "legacy2@example.com", weak_hasher.hash(TEST_PASSWORD), None
    )
    user_id, weak_hash = created.id, created.password_hash
    await db_session.commit()

    response = await anon_client.post(
        LOGIN, json={"email": "legacy2@example.com", "password": "not the password"}
    )
    assert response.status_code == 401

    stored = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    await db_session.refresh(stored)
    assert stored.password_hash == weak_hash


# --- me / bearer handling --------------------------------------------------
async def test_me_returns_the_caller(client: AsyncClient, user: User) -> None:
    response = await client.get(ME)
    assert response.status_code == 200
    assert response.json()["id"] == str(user.id)
    assert "password_hash" not in response.text


async def test_me_without_a_token_is_401(anon_client: AsyncClient) -> None:
    response = await anon_client.get(ME)
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


@pytest.mark.parametrize(
    "header",
    [
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Bearer ",
        "Bearer garbage",
        "Bearer a.b.c",
        "bearer",
        "Token abc",
        "",
    ],
)
async def test_bad_authorization_headers_are_401(
    anon_client: AsyncClient, header: str
) -> None:
    response = await anon_client.get(ME, headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


async def test_a_token_signed_with_another_secret_is_401(
    anon_client: AsyncClient, user: User
) -> None:
    forged = jwt.encode(
        {
            "sub": str(user.id),
            "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        },
        "attacker-chosen-secret-0123456789abcdef",  # test-only, not a secret
        algorithm=JWT_ALGORITHM,
    )
    response = await anon_client.get(ME, headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


async def test_an_expired_token_is_401(
    anon_client: AsyncClient, user: User, test_settings
) -> None:
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired = jwt.encode(
        {"sub": str(user.id), "iat": int(past.timestamp()) - 3600, "exp": int(past.timestamp())},
        test_settings.JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    response = await anon_client.get(ME, headers={"Authorization": f"Bearer {expired}"})
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


async def test_an_alg_none_token_is_401(anon_client: AsyncClient, user: User) -> None:
    import base64
    import json as json_module

    def segment(data: dict) -> bytes:
        return base64.urlsafe_b64encode(json_module.dumps(data).encode()).rstrip(b"=")

    exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    forged = b".".join(
        [
            segment({"alg": "none", "typ": "JWT"}),
            segment({"sub": str(user.id), "exp": exp}),
            b"",
        ]
    ).decode()

    response = await anon_client.get(ME, headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


async def test_a_token_for_a_deleted_user_is_401(
    client: AsyncClient, db_session, user: User
) -> None:
    """A signature alone is not identity: the subject must still exist."""
    assert (await client.get(ME)).status_code == 200

    await db_session.delete(user)
    await db_session.commit()

    response = await client.get(ME)
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED_BODY


async def test_a_token_for_an_unknown_subject_is_401(
    anon_client: AsyncClient, test_settings
) -> None:
    from app.security import create_access_token

    token, _ = create_access_token(uuid4(), test_settings)
    response = await anon_client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


# --- the protected surface -------------------------------------------------
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/todos", None),
        ("post", "/api/todos", {"title": "Task"}),
        ("get", f"/api/todos/{uuid4()}", None),
        ("patch", f"/api/todos/{uuid4()}", {"completed": True}),
        ("delete", f"/api/todos/{uuid4()}", None),
        ("get", "/api/lists", None),
        ("post", "/api/lists", {"name": "Work"}),
        ("patch", f"/api/lists/{uuid4()}", {"name": "Work"}),
        ("delete", f"/api/lists/{uuid4()}", None),
        ("get", ME, None),
    ],
)
async def test_every_protected_endpoint_requires_a_token(
    anon_client: AsyncClient, method: str, path: str, body: dict | None
) -> None:
    response = await getattr(anon_client, method)(
        path, **({"json": body} if body is not None else {})
    )
    assert response.status_code == 401, (method, path)
    assert response.json() == UNAUTHORIZED_BODY


@pytest.mark.parametrize(
    "path", ["/api/health", "/api/health/ready", REGISTER, LOGIN]
)
async def test_the_public_endpoints_do_not_require_a_token(
    anon_client: AsyncClient, path: str
) -> None:
    if path in {REGISTER, LOGIN}:
        response = await anon_client.post(path, json=registration())
        assert response.status_code in (200, 201, 401)
        # 401 here would be invalid_credentials (login), never unauthorized.
        assert response.json().get("code") != "unauthorized"
    else:
        assert (await anon_client.get(path)).status_code == 200


async def test_authentication_precedes_body_validation(
    anon_client: AsyncClient,
) -> None:
    """An anonymous caller must not be able to probe the request schema."""
    response = await anon_client.post("/api/todos", json={"title": ""})
    assert response.status_code == 401
