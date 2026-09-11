"""Password hashing and access tokens (slice-2 spec B2).

Two primitives live here and nowhere else:

* **argon2id password hashing** — the only thing that ever sees a raw password.
* **HS256 JWTs** — minted on login, verified by ``get_current_user``.

Both are deliberately small, pure functions so the auth router stays free of
crypto details and the properties below can be unit-tested directly:

- verification never raises, so a corrupt stored hash is a failed login rather
  than a 500;
- decoding always verifies the signature *and* ``exp`` and accepts exactly one
  algorithm, so an ``alg: none`` or RS256-confusion token can never validate.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from uuid import UUID, uuid4

import jwt
from anyio import CapacityLimiter, to_thread
from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError
from pydantic import ValidationError

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: The one algorithm this application issues and accepts.
JWT_ALGORITHM = "HS256"

#: argon2id production parameters (the argon2-cffi defaults, spelled out so a
#: reviewer can see them and so the fast test profile is an obvious deviation).
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST = 64 * 1024  # KiB
ARGON2_PARALLELISM = 4

#: Test-only profile (risk R3): the production parameters cost ~64 MiB and tens
#: of milliseconds per hash, which turns the auth suite into minutes. Selected
#: **only** in dev and only when TEST_ARGON2_FAST is set — see
#: :func:`fast_hashing_enabled`.
FAST_ARGON2_TIME_COST = 1
FAST_ARGON2_MEMORY_COST = 8 * 1024  # KiB
FAST_ARGON2_PARALLELISM = 1

_FALSEY = {"", "0", "false", "no", "off"}


class InvalidToken(Exception):
    """A bearer token that must not be trusted (any reason)."""


def fast_hashing_enabled(app_env: str | None, flag: str | None) -> bool:
    """Whether the cheap argon2 profile may be used.

    Both conditions must be **explicitly** true. ``app_env`` is compared
    without a default on purpose: an unset ``APP_ENV`` used to mean "dev", so a
    deployment that forgot to set it and happened to carry ``TEST_ARGON2_FAST``
    would silently write every password with test-strength parameters. Failing
    closed costs a misconfigured dev box some milliseconds; failing open costs
    the production password file.
    """
    if app_env != "dev":
        return False
    if flag is None:
        return False
    return flag.strip().lower() not in _FALSEY


def configured_app_env() -> str | None:
    """``APP_ENV`` as the application itself resolves it, or ``None`` if unset.

    Two properties, both load-bearing:

    * **Same source as the app.** Reading ``os.environ`` directly would ignore
      the ``.env`` file that ``Settings`` also loads, so the hasher could
      disagree with the rest of the process about which environment it is in.
    * **No default.** ``Settings.APP_ENV`` falls back to ``"dev"``, which is
      exactly the fallback that must not weaken hashing, so an environment that
      never stated its ``APP_ENV`` is reported as ``None`` rather than dev.
    """
    try:
        settings = get_settings()
    except ValidationError:
        # Unparseable configuration: fail closed. (The app will refuse to start
        # for the same reason moments later.)
        return None
    if "APP_ENV" not in settings.model_fields_set:
        return None
    return settings.APP_ENV


def _build_hasher() -> PasswordHasher:
    # The hasher is a module-level singleton: whichever configuration is in
    # force at the first hash decides the parameters for the process' lifetime.
    if fast_hashing_enabled(
        configured_app_env(), os.environ.get("TEST_ARGON2_FAST")
    ):
        logger.warning(
            "TEST_ARGON2_FAST is set: using weakened argon2 parameters. "
            "This is for tests only and is refused outside APP_ENV=dev."
        )
        return PasswordHasher(
            time_cost=FAST_ARGON2_TIME_COST,
            memory_cost=FAST_ARGON2_MEMORY_COST,
            parallelism=FAST_ARGON2_PARALLELISM,
        )
    return PasswordHasher(
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST,
        parallelism=ARGON2_PARALLELISM,
    )


@lru_cache(maxsize=1)
def _hasher() -> PasswordHasher:
    return _build_hasher()


def using_fast_hashing() -> bool:
    """Whether this process' hasher is the weakened test profile.

    Derived from the live hasher's parameters rather than from the environment:
    the hasher is a singleton chosen at first use, so a later change to
    ``APP_ENV``/``TEST_ARGON2_FAST`` would make an environment-based answer
    disagree with the hashes actually being written.
    """
    hasher = _hasher()
    return (
        hasher.time_cost,
        hasher.memory_cost,
        hasher.parallelism,
    ) == (FAST_ARGON2_TIME_COST, FAST_ARGON2_MEMORY_COST, FAST_ARGON2_PARALLELISM)


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """A real hash of a fixed string, verified against when no user exists.

    Login must take the same code path — and therefore roughly the same time —
    whether the email is unknown or the password is wrong, or the response
    timing becomes an account-enumeration oracle (master §6.1).
    """
    return _hasher().hash("dummy-password-for-timing-equalisation")


def hash_password_sync(raw: str) -> str:
    """Blocking argon2id hash. Prefer the async :func:`hash_password`."""
    return _hasher().hash(raw)


def verify_password_sync(raw: str, hashed: str) -> bool:
    """Constant-shape verification that **never raises**.

    argon2-cffi signals a mismatch, a malformed hash and a backend failure as
    three different exceptions; to a caller they all mean "this login fails".
    Letting one escape would turn a corrupt row into a 500 (and a distinguishable
    response).
    """
    try:
        return _hasher().verify(hashed, raw)
    except (Argon2Error, InvalidHashError, TypeError, ValueError):
        return False


def _verify_dummy_sync(raw: str) -> None:
    verify_password_sync(raw, _dummy_hash())


# --- async wrappers --------------------------------------------------------
# argon2id is *designed* to be slow and memory-hungry (~50 ms and 64 MiB per
# call with the production parameters). Called directly from an ``async def``
# handler it would block the single event loop thread for that whole time, so
# a handful of concurrent logins would stall **every** other request in the
# process — including /api/health, the compose readiness probe. Every hashing
# call therefore runs on the thread pool; argon2-cffi releases the GIL inside
# its C implementation, so this is real parallelism, not just yielding.


@lru_cache(maxsize=1)
def _argon2_limiter() -> CapacityLimiter:
    """Cap how many argon2 hashes may run at once.

    Each production-parameter hash holds ~64 MiB for its duration, so the
    memory this component can claim is ``limit × 64 MiB`` — a number that must
    be chosen, not inherited. AnyIO's shared thread pool defaults to 40 slots,
    which would be ~2.5 GiB of argon2 working memory on a machine that also has
    to run the database: the process would be OOM-killed rather than merely
    slow, and the rate limiter cannot prevent it on its own because 40 requests
    is well under any per-address budget.

    Queueing is the desired behaviour past the cap: a login that waits is a
    login that still succeeds, and the surrounding auth rate limits bound how
    long the queue can get. It is its own limiter rather than the default pool
    so that slow hashing can never starve the *other* things that need a
    thread.
    """
    return CapacityLimiter(get_settings().ARGON2_MAX_CONCURRENCY)


async def hash_password(raw: str) -> str:
    return await to_thread.run_sync(
        hash_password_sync, raw, limiter=_argon2_limiter()
    )


async def verify_password(raw: str, hashed: str) -> bool:
    return await to_thread.run_sync(
        verify_password_sync, raw, hashed, limiter=_argon2_limiter()
    )


def needs_rehash(hashed: str) -> bool:
    """Whether ``hashed`` was written with weaker parameters than we use now.

    Hashes are immutable once written, so a password stored under the fast test
    profile (or under older, cheaper production parameters) stays weak forever
    unless something notices. Login is the only moment the plaintext is
    available to re-derive it, so that is where the upgrade happens.
    """
    try:
        return _hasher().check_needs_rehash(hashed)
    except (Argon2Error, InvalidHashError, TypeError, ValueError):
        # An unparseable hash cannot be upgraded — and no password verifies
        # against it either, so there is nothing to rewrite.
        return False


async def verify_dummy_password(raw: str) -> None:
    """Burn the same work a real verification would, and discard the outcome.

    Returns nothing on purpose: the only caller is the "no such user" branch of
    login, where the answer is already decided. If this returned a bool, a
    caller submitting the fixed dummy string would get ``True`` back, and some
    future refactor could believe it.
    """
    # Same limiter as a real verification: the timing-equalisation branch must
    # queue exactly like the branch it is imitating, or waiting behind the cap
    # would itself become the enumeration oracle this function exists to close.
    await to_thread.run_sync(_verify_dummy_sync, raw, limiter=_argon2_limiter())


def _require_secret(settings: Settings) -> str:
    secret = settings.JWT_SECRET
    if not secret:
        # create_app refuses to build an app without it, so reaching this is a
        # programming error rather than a configuration one.
        raise RuntimeError("JWT_SECRET is not configured")
    return secret


def create_access_token(user_id: UUID, settings: Settings) -> tuple[str, int]:
    """Mint an access token; returns ``(token, expires_in_seconds)``."""
    expires_in = settings.JWT_EXPIRES_MINUTES * 60
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        # A unique id per token: nothing consumes it yet, but it is what a
        # future revocation list would key on and it keeps two tokens minted in
        # the same second distinct.
        "jti": str(uuid4()),
    }
    token = jwt.encode(payload, _require_secret(settings), algorithm=JWT_ALGORITHM)
    return token, expires_in


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """The parts of a verified access token the application acts on."""

    user_id: UUID
    #: When the token stops being valid, as an aware UTC datetime. Slice 4's
    #: SSE stream needs it: a connection held open for hours must not outlive
    #: the credential that opened it.
    expires_at: datetime


def decode_access_token_claims(token: str, settings: Settings) -> TokenClaims:
    """Verify a token and return its claims, or raise :class:`InvalidToken`.

    ``algorithms=[JWT_ALGORITHM]`` is what rejects ``alg: none`` and algorithm
    confusion; ``require`` makes a token without ``exp``/``sub`` invalid rather
    than eternally valid.
    """
    try:
        payload = jwt.decode(
            token,
            _require_secret(settings),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "sub"], "verify_exp": True},
        )
    except jwt.PyJWTError as exc:
        raise InvalidToken(f"token rejected: {exc}") from exc

    subject = payload.get("sub")
    try:
        user_id = UUID(subject)
    except (AttributeError, TypeError, ValueError) as exc:
        raise InvalidToken("token subject is not a user id") from exc

    try:
        expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
    except (KeyError, OverflowError, OSError, TypeError, ValueError) as exc:
        # ``require: ["exp"]`` guarantees the claim is present and pyjwt has
        # already compared it to the clock, so anything unconvertible here is a
        # malformed token rather than an expired one.
        raise InvalidToken("token expiry is not a timestamp") from exc

    return TokenClaims(user_id=user_id, expires_at=expires_at)


def decode_access_token(token: str, settings: Settings) -> UUID:
    """Verify a token and return its subject.

    The narrow view of :func:`decode_access_token_claims`, kept because almost
    every caller only ever wants "who is this?" and should not have to reach
    past an object to say so.
    """
    return decode_access_token_claims(token, settings).user_id
