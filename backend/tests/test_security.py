"""Unit tests for the auth primitives (slice-2 spec B9.3/B9.7/B9.8).

These are the properties an attacker probes first: what a token must prove
before it is believed, what a wrong password does, and whether the app can be
started with a weak or absent signing key.
"""

import base64
import json
import threading
from datetime import datetime, timedelta, timezone
from unittest import mock
from uuid import uuid4

import jwt
import pytest
from anyio import to_thread
from pydantic import ValidationError

from app.config import MIN_JWT_SECRET_LENGTH, Settings, get_settings
from app.main import create_app, require_jwt_secret
from app.schemas.auth import UserResponse
from app.security import (
    ARGON2_MEMORY_COST,
    ARGON2_PARALLELISM,
    ARGON2_TIME_COST,
    FAST_ARGON2_MEMORY_COST,
    JWT_ALGORITHM,
    InvalidToken,
    _argon2_limiter,
    _build_hasher,
    _hasher,
    configured_app_env,
    create_access_token,
    decode_access_token,
    fast_hashing_enabled,
    hash_password,
    needs_rehash,
    using_fast_hashing,
    verify_dummy_password,
    verify_password,
)

OTHER_SECRET = "another-test-only-secret-0123456789abcdef"  # test-only, not a secret


# --- password hashing ------------------------------------------------------
async def test_hash_is_argon2id_and_never_the_plaintext() -> None:
    hashed = await hash_password("correct horse battery")
    assert hashed.startswith("$argon2id$")
    assert "correct horse battery" not in hashed
    # Salted: the same password hashes differently every time.
    assert hashed != await hash_password("correct horse battery")


async def test_verify_password_round_trip() -> None:
    hashed = await hash_password("correct horse battery")
    assert await verify_password("correct horse battery", hashed) is True
    assert await verify_password("wrong password", hashed) is False


@pytest.mark.parametrize(
    "corrupt",
    ["", "!", "not-a-hash", "$argon2id$v=19$m=8,t=1,p=1$truncated"],
)
async def test_verify_password_never_raises_on_a_corrupt_hash(corrupt: str) -> None:
    """A damaged row must fail the login, not 500 the endpoint."""
    assert await verify_password("anything", corrupt) is False


async def test_verify_dummy_password_does_the_work_and_returns_nothing() -> None:
    """Login verifies against this when the email is unknown, so that the
    response time does not distinguish "no such user" from "wrong password".
    It yields no verdict — not even for the dummy string itself."""
    assert await verify_dummy_password("anything") is None
    assert await verify_dummy_password("dummy-password-for-timing-equalisation") is None


@pytest.mark.parametrize(
    ("app_env", "flag", "expected"),
    [
        ("dev", "1", True),
        ("dev", "true", True),
        ("dev", None, False),
        ("dev", "0", False),
        ("dev", "false", False),
        ("dev", "", False),
        # The whole point: a stray flag in production must not weaken hashing.
        ("prod", "1", False),
        ("prod", "true", False),
        # APP_ENV unset is NOT dev: failing closed here is the difference
        # between a slow dev box and a weak production password file.
        (None, "1", False),
        (None, "true", False),
        ("", "1", False),
        ("staging", "1", False),
    ],
)
def test_fast_argon2_profile_needs_an_explicit_dev_env(
    app_env, flag, expected: bool
) -> None:
    assert fast_hashing_enabled(app_env, flag) is expected


@pytest.fixture
def settings_env(monkeypatch):
    """Drive ``get_settings`` from a controlled environment, cache cleared."""
    monkeypatch.setenv("JWT_SECRET", "z" * 64)  # test-only, not a secret
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def test_app_env_is_none_when_it_was_never_configured(settings_env) -> None:
    """``Settings.APP_ENV`` defaults to "dev"; the hashing gate must not."""
    settings_env.delenv("APP_ENV", raising=False)
    assert get_settings().APP_ENV == "dev"  # the model default is still there
    assert configured_app_env() is None  # …but it is not treated as configured


@pytest.mark.parametrize("value", ["dev", "prod"])
def test_app_env_comes_from_the_settings_source(settings_env, value: str) -> None:
    settings_env.setenv("APP_ENV", value)
    assert configured_app_env() == value


def test_app_env_is_none_when_the_configuration_is_unparseable(
    settings_env,
) -> None:
    settings_env.setenv("APP_ENV", "dev")
    settings_env.setenv("JWT_SECRET", "too-short")  # test-only, not a secret
    assert configured_app_env() is None


def test_the_hasher_uses_production_parameters_without_an_explicit_dev_env(
    settings_env,
) -> None:
    settings_env.delenv("APP_ENV", raising=False)
    settings_env.setenv("TEST_ARGON2_FAST", "1")

    hasher = _build_hasher()

    assert hasher.memory_cost == ARGON2_MEMORY_COST
    assert hasher.time_cost == ARGON2_TIME_COST


def test_the_hasher_uses_the_fast_profile_only_in_an_explicit_dev_env(
    settings_env,
) -> None:
    settings_env.setenv("APP_ENV", "dev")
    settings_env.setenv("TEST_ARGON2_FAST", "1")

    assert _build_hasher().memory_cost == FAST_ARGON2_MEMORY_COST

    settings_env.setenv("APP_ENV", "prod")
    get_settings.cache_clear()
    assert _build_hasher().memory_cost == ARGON2_MEMORY_COST


# --- rehash on login -------------------------------------------------------
def test_needs_rehash_flags_a_weaker_hash() -> None:
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("pw")
    assert needs_rehash(weak) is True


async def test_needs_rehash_is_false_for_a_current_hash() -> None:
    assert needs_rehash(await hash_password("pw")) is False


@pytest.mark.parametrize("corrupt", ["", "!", "not-a-hash"])
def test_needs_rehash_never_raises_on_a_corrupt_hash(corrupt: str) -> None:
    assert needs_rehash(corrupt) is False


def test_using_fast_hashing_reports_the_live_profile() -> None:
    """It reads the hasher actually in force, not the environment.

    The hasher is a singleton chosen at first use; an answer derived from
    ``APP_ENV``/``TEST_ARGON2_FAST`` could disagree with the parameters the
    process is really writing, which is precisely the disagreement the
    no-downgrade rule in login depends on being right about.
    """
    hasher = _hasher()
    assert using_fast_hashing() is (hasher.memory_cost == FAST_ARGON2_MEMORY_COST)
    # The suite sets TEST_ARGON2_FAST (risk R3), so this process is the fast one.
    assert using_fast_hashing() is True


# --- argon2 concurrency cap ------------------------------------------------
def test_hashing_uses_its_own_bounded_limiter() -> None:
    """The memory ceiling of password hashing is a chosen number, not AnyIO's
    40-slot default thread pool (~2.5 GiB of argon2 working memory)."""
    limiter = _argon2_limiter()
    assert limiter.total_tokens == Settings.model_fields[
        "ARGON2_MAX_CONCURRENCY"
    ].default
    assert limiter.total_tokens == 4
    # A singleton: a fresh limiter per call would cap nothing.
    assert _argon2_limiter() is limiter


async def test_concurrent_hashing_never_exceeds_the_cap() -> None:
    import anyio

    from app import security

    in_flight = 0
    peak = 0
    lock = threading.Lock()

    real_hash = security.hash_password_sync

    def instrumented(raw: str) -> str:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        try:
            return real_hash(raw)
        finally:
            with lock:
                in_flight -= 1

    limit = security._argon2_limiter().total_tokens
    with mock.patch.object(security, "hash_password_sync", instrumented):
        async with anyio.create_task_group() as group:
            for index in range(int(limit) * 4):
                group.start_soon(security.hash_password, f"pw-{index}")

    assert peak <= limit


async def test_the_dummy_verification_shares_the_hashing_limiter() -> None:
    """It must queue exactly like the branch it imitates, or the wait itself
    becomes the enumeration oracle it exists to remove."""
    from app import security

    seen: list[object] = []
    real_run_sync = to_thread.run_sync

    async def spy(func, *args, **kwargs):
        seen.append(kwargs.get("limiter"))
        return await real_run_sync(func, *args, **kwargs)

    with mock.patch.object(security.to_thread, "run_sync", spy):
        await verify_dummy_password("anything")

    assert seen == [security._argon2_limiter()]


def test_needs_rehash_alone_would_downgrade_a_strong_hash() -> None:
    """Why login needs the second condition, stated as an executable fact."""
    from argon2 import PasswordHasher

    strong = PasswordHasher(
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
    ).hash("pw")

    assert needs_rehash(strong) is True  # "different", not "weaker"
    assert using_fast_hashing() is True  # …so acting on it would weaken it


async def test_hashing_runs_off_the_event_loop_thread() -> None:
    """argon2 must not run on the loop thread, or it stalls every request."""
    import threading

    loop_thread = threading.current_thread().ident
    hashing_thread: list[int | None] = []

    def spy(raw: str) -> str:
        hashing_thread.append(threading.current_thread().ident)
        return "$argon2id$fake"

    import app.security as security_module

    original = security_module.hash_password_sync
    security_module.hash_password_sync = spy
    try:
        await security_module.hash_password("x")
    finally:
        security_module.hash_password_sync = original

    assert hashing_thread == [hashing_thread[0]]
    assert hashing_thread[0] != loop_thread


async def test_a_login_in_flight_does_not_block_other_requests(
    anon_client, user, monkeypatch
) -> None:
    """The property that matters end to end: while one request is hashing, the
    health probe (and therefore every other request) still answers."""
    import asyncio
    import threading

    import app.security as security_module

    release = threading.Event()

    def blocking_verify(raw: str, hashed: str) -> bool:
        release.wait(timeout=10)
        return False

    monkeypatch.setattr(security_module, "verify_password_sync", blocking_verify)

    login = asyncio.create_task(
        anon_client.post(
            "/api/auth/login", json={"email": user.email, "password": "wrong"}
        )
    )
    try:
        # Give the login task time to reach the hash before probing.
        await asyncio.sleep(0.05)
        assert not login.done()

        health = await asyncio.wait_for(anon_client.get("/api/health"), timeout=2)
        assert health.status_code == 200
        assert not login.done(), "the hash should still be running in its thread"
    finally:
        release.set()
        assert (await login).status_code == 401


# --- tokens ----------------------------------------------------------------
@pytest.fixture
def settings(test_settings: Settings) -> Settings:
    return test_settings


def test_token_round_trip_carries_the_subject(settings: Settings) -> None:
    user_id = uuid4()
    token, expires_in = create_access_token(user_id, settings)

    assert expires_in == settings.JWT_EXPIRES_MINUTES * 60
    assert decode_access_token(token, settings) == user_id


def test_token_claims_are_sub_iat_exp_jti(settings: Settings) -> None:
    token, expires_in = create_access_token(uuid4(), settings)
    payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[JWT_ALGORITHM])

    assert set(payload) == {"sub", "iat", "exp", "jti"}
    assert payload["exp"] - payload["iat"] == expires_in
    assert jwt.get_unverified_header(token)["alg"] == JWT_ALGORITHM


def test_two_tokens_for_one_user_differ(settings: Settings) -> None:
    first, _ = create_access_token(uuid4(), settings)
    second, _ = create_access_token(uuid4(), settings)
    assert first != second


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "Bearer x"])
def test_malformed_tokens_are_rejected(settings: Settings, token: str) -> None:
    with pytest.raises(InvalidToken):
        decode_access_token(token, settings)


def test_a_token_signed_with_another_secret_is_rejected(settings: Settings) -> None:
    foreign = settings.model_copy(update={"JWT_SECRET": OTHER_SECRET})
    token, _ = create_access_token(uuid4(), foreign)

    with pytest.raises(InvalidToken):
        decode_access_token(token, settings)


def test_an_expired_token_is_rejected(settings: Settings) -> None:
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "iat": int((past - timedelta(minutes=60)).timestamp()),
            "exp": int(past.timestamp()),
        },
        settings.JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )
    with pytest.raises(InvalidToken):
        decode_access_token(token, settings)


def test_an_alg_none_token_is_rejected(settings: Settings) -> None:
    """The classic JWT bypass: strip the signature and claim it is not needed."""
    def _segment(data: dict) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=")

    exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    forged = b".".join(
        [
            _segment({"alg": "none", "typ": "JWT"}),
            _segment({"sub": str(uuid4()), "exp": exp}),
            b"",
        ]
    ).decode()

    with pytest.raises(InvalidToken):
        decode_access_token(forged, settings)


@pytest.mark.parametrize(
    "payload",
    [
        {"sub": "not-a-uuid"},
        {"sub": None},
        {"sub": 12345},
    ],
)
def test_a_token_whose_subject_is_not_a_user_id_is_rejected(
    settings: Settings, payload: dict
) -> None:
    exp = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())
    token = jwt.encode(
        {**payload, "exp": exp}, settings.JWT_SECRET, algorithm=JWT_ALGORITHM
    )
    with pytest.raises(InvalidToken):
        decode_access_token(token, settings)


@pytest.mark.parametrize("missing", ["exp", "sub"])
def test_a_token_missing_a_required_claim_is_rejected(
    settings: Settings, missing: str
) -> None:
    """Without ``exp`` a token would never expire; without ``sub`` it names nobody."""
    claims = {
        "sub": str(uuid4()),
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
    }
    del claims[missing]
    token = jwt.encode(claims, settings.JWT_SECRET, algorithm=JWT_ALGORITHM)

    with pytest.raises(InvalidToken):
        decode_access_token(token, settings)


# --- configuration ---------------------------------------------------------
def test_a_missing_jwt_secret_refuses_to_build_an_app(test_settings) -> None:
    settings = test_settings.model_copy(update={"JWT_SECRET": None})
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        require_jwt_secret(settings)


def test_create_app_refuses_to_start_without_a_jwt_secret(test_settings) -> None:
    """The gate must hold where it actually runs — uvicorn imports the module
    and calls create_app, so this is the failure an operator sees."""
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        create_app(test_settings.model_copy(update={"JWT_SECRET": None}))


def test_create_app_starts_with_a_valid_jwt_secret(test_settings) -> None:
    app = create_app(test_settings)
    assert app.state.settings.JWT_SECRET == test_settings.JWT_SECRET


def test_an_empty_jwt_secret_is_treated_as_missing(db_url: str) -> None:
    settings = Settings(DATABASE_URL=db_url, JWT_SECRET="")
    assert settings.JWT_SECRET is None
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        require_jwt_secret(settings)


def test_a_short_jwt_secret_is_refused(db_url: str) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(DATABASE_URL=db_url, JWT_SECRET="x" * (MIN_JWT_SECRET_LENGTH - 1))


def test_a_long_enough_jwt_secret_is_accepted(db_url: str) -> None:
    secret = "y" * MIN_JWT_SECRET_LENGTH  # test-only, not a secret
    assert Settings(DATABASE_URL=db_url, JWT_SECRET=secret).JWT_SECRET == secret


def test_there_is_no_default_jwt_secret(db_url: str) -> None:
    """A default signing key is a published signing key."""
    assert Settings.model_fields["JWT_SECRET"].default is None


# --- response shape --------------------------------------------------------
def test_user_response_cannot_carry_a_password_hash() -> None:
    assert "password_hash" not in UserResponse.model_fields
    assert set(UserResponse.model_fields) == {
        "id",
        "email",
        "display_name",
        "created_at",
    }
