"""``/api/auth`` — register, login, me (master §6.1).

Three properties this module exists to guarantee:

1. **No account enumeration.** Registering a taken email is the only place that
   admits an account exists, and that is unavoidable; login answers with one
   byte-identical 401 whether the email is unknown or the password is wrong,
   and burns the same argon2 work in both cases.
2. **No credential ever reaches a log.** Only ``user_id`` is logged.
3. **Registration is atomic**: a user without its ``Inbox`` list would be an
   account that cannot hold a todo, so both rows are created — and committed —
   in one transaction, before the 201 is sent.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db.models import User
from app.deps import (
    SessionDep,
    get_app_settings,
    get_auth_ip_rate_limiter,
    get_auth_rate_limiter,
    get_client_ip,
    get_current_user,
    get_list_repository,
    get_user_repository,
)
from app.errors import EmailTaken, InvalidCredentials, RateLimited
from app.ratelimit import RateLimiter
from app.repositories.lists import DEFAULT_LIST_NAME, SqlAlchemyListRepository
from app.repositories.protocols import UserRepository
from app.schemas.auth import (
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.schemas.common import ErrorResponse
from app.security import (
    create_access_token,
    hash_password,
    needs_rehash,
    using_fast_hashing,
    verify_dummy_password,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

Users = Annotated[UserRepository, Depends(get_user_repository)]
Lists = Annotated[SqlAlchemyListRepository, Depends(get_list_repository)]
Session = SessionDep
AppSettings = Annotated[Settings, Depends(get_app_settings)]
Limiter = Annotated[RateLimiter, Depends(get_auth_rate_limiter)]
IpLimiter = Annotated[RateLimiter, Depends(get_auth_ip_rate_limiter)]
ClientIp = Annotated[str, Depends(get_client_ip)]
CurrentUser = Annotated[User, Depends(get_current_user)]

RATE_LIMITED_RESPONSE = {
    429: {"model": ErrorResponse, "description": "Too many attempts"}
}


def _enforce_rate_limits(
    ip_limiter: RateLimiter,
    limiter: RateLimiter,
    client_ip: str,
    email: str,
) -> None:
    """Two tiers, address-wide first, then per (address, email).

    The per-email tier protects one account from guessing, but it is keyed on
    the email, so a caller that never repeats an address never trips it — and
    every attempt still costs the server a full argon2 hash (~50 ms, 64 MiB).
    The address-wide tier is the ceiling on that work, and it is checked
    **first**: once an address is over its budget its attempts must not even
    reach — let alone consume — the per-account counters, which would otherwise
    let one attacker lock accounts out by proxy.
    """
    ip_result = ip_limiter.hit(client_ip)
    if not ip_result.allowed:
        raise RateLimited(headers={"Retry-After": str(ip_result.retry_after)})

    result = limiter.hit(f"{client_ip}|{email.lower()}")
    if not result.allowed:
        raise RateLimited(headers={"Retry-After": str(result.retry_after)})


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"model": ErrorResponse, "description": "That email is already registered"},
        **RATE_LIMITED_RESPONSE,
    },
)
async def register(
    payload: RegisterRequest,
    users: Users,
    lists: Lists,
    session: Session,
    limiter: Limiter,
    ip_limiter: IpLimiter,
    client_ip: ClientIp,
) -> UserResponse:
    """Create an account and its default ``Inbox`` list.

    No token is returned: the client calls login next, so session
    establishment has exactly one code path (master §6.1).
    """
    _enforce_rate_limits(ip_limiter, limiter, client_ip, payload.email)

    if await users.get_by_email(payload.email) is not None:
        raise EmailTaken()

    try:
        user = await users.create(
            email=payload.email,
            password_hash=await hash_password(payload.password),
            display_name=payload.display_name,
        )
        await lists.create(user.id, DEFAULT_LIST_NAME, is_default=True)
        # Commit here, not in the session dependency's teardown: the client
        # follows a 201 straight into POST /api/auth/login, and a transaction
        # that lands after the response has been sent makes that login 401.
        await session.commit()
    except IntegrityError as exc:
        # Two concurrent registrations of the same address: the pre-check above
        # passed in both, and the UNIQUE index is what actually decides. The
        # loser gets the same 409 as a sequential duplicate.
        await session.rollback()
        raise EmailTaken() from exc

    logger.info("Registered user %s", user.id)
    return UserResponse.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Invalid email or password"},
        **RATE_LIMITED_RESPONSE,
    },
)
async def login(
    payload: LoginRequest,
    users: Users,
    session: Session,
    settings: AppSettings,
    limiter: Limiter,
    ip_limiter: IpLimiter,
    client_ip: ClientIp,
) -> TokenResponse:
    _enforce_rate_limits(ip_limiter, limiter, client_ip, payload.email)

    user = await users.get_by_email(payload.email)
    if user is None:
        # Verify against a dummy hash anyway: skipping the work here would make
        # "unknown email" measurably faster than "wrong password" and turn the
        # response time into an account-enumeration oracle.
        await verify_dummy_password(payload.password)
        logger.info("Failed login for an unknown email from %s", client_ip)
        raise InvalidCredentials()

    if not await verify_password(payload.password, user.password_hash):
        logger.info("Failed login for user %s", user.id)
        raise InvalidCredentials()

    if needs_rehash(user.password_hash) and not using_fast_hashing():
        # The one moment the plaintext exists: an account whose hash was
        # written under weaker parameters (an older cost setting, or the fast
        # test profile) is upgraded in place.
        #
        # ``check_needs_rehash`` answers "these parameters differ from mine",
        # not "these are weaker" — so under the fast test profile it reports
        # True for every production-strength hash, and rewriting it would
        # *downgrade* a real password to test-strength parameters. A dev flag
        # must never be able to weaken a stored credential, so the upgrade is
        # skipped entirely whenever the cheap profile is in force.
        user.password_hash = await hash_password(payload.password)
        await session.commit()
        logger.info("Upgraded the password hash of user %s", user.id)

    token, expires_in = create_access_token(user.id, settings)
    logger.info("Issued an access token for user %s", user.id)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user=UserResponse.model_validate(user),
    )


@router.get(
    "/me",
    response_model=UserResponse,
    responses={401: {"model": ErrorResponse, "description": "Not authenticated"}},
)
async def me(user: CurrentUser) -> UserResponse:
    return UserResponse.model_validate(user)
