"""Login/registration throttling (slice-2 spec B4/B9.6)."""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.config import DEFAULT_TRUSTED_PROXY_CIDRS, Settings
from app.db.models import User
from app.db.session import create_sessionmaker
from app.deps import client_ip_from, get_session, is_trusted_proxy
from app.main import AUTH_RATE_LIMIT, AUTH_RATE_WINDOW_SECONDS, create_app
from app.ratelimit import RateLimiter
from tests.conftest import TEST_PASSWORD

LOGIN = "/api/auth/login"


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- unit ------------------------------------------------------------------
def test_allows_up_to_the_limit_then_refuses() -> None:
    limiter = RateLimiter(3, 60, clock=FakeClock())

    assert [limiter.hit("k").allowed for _ in range(3)] == [True, True, True]
    refused = limiter.hit("k")
    assert refused.allowed is False
    assert refused.retry_after == 60


def test_keys_are_independent() -> None:
    limiter = RateLimiter(1, 60, clock=FakeClock())

    assert limiter.hit("a").allowed is True
    assert limiter.hit("a").allowed is False
    assert limiter.hit("b").allowed is True


def test_the_window_slides() -> None:
    clock = FakeClock()
    limiter = RateLimiter(2, 60, clock=clock)

    limiter.hit("k")
    clock.advance(30)
    limiter.hit("k")
    assert limiter.hit("k").allowed is False

    # The first attempt ages out; one slot frees up, the second still counts.
    clock.advance(31)
    assert limiter.hit("k").allowed is True
    assert limiter.hit("k").allowed is False

    clock.advance(61)
    assert limiter.hit("k").allowed is True


def test_retry_after_counts_down_and_is_never_zero() -> None:
    clock = FakeClock()
    limiter = RateLimiter(1, 60, clock=clock)
    limiter.hit("k")

    assert limiter.hit("k").retry_after == 60
    clock.advance(30)
    assert limiter.hit("k").retry_after == 30
    clock.advance(29.5)
    # Still blocked, so the client must be told to wait at least one second.
    assert limiter.hit("k").retry_after == 1


def test_refused_attempts_do_not_extend_the_lockout() -> None:
    """Hammering must not let an attacker keep the real owner locked out."""
    clock = FakeClock()
    limiter = RateLimiter(1, 60, clock=clock)
    limiter.hit("k")

    for _ in range(20):
        clock.advance(1)
        assert limiter.hit("k").allowed is False

    clock.advance(41)  # 61 s after the single accepted attempt
    assert limiter.hit("k").allowed is True


def test_tracked_keys_are_bounded() -> None:
    """A flood of distinct emails must not grow the process without bound."""
    limiter = RateLimiter(1, 60, max_keys=10, clock=FakeClock())

    for index in range(100):
        limiter.hit(f"key-{index}")

    assert len(limiter._hits) == 10
    # The oldest keys were evicted, so they start fresh rather than staying blocked.
    assert limiter.hit("key-0").allowed is True


def test_reset_forgets_one_key_or_everything() -> None:
    limiter = RateLimiter(1, 60, clock=FakeClock())
    limiter.hit("a")
    limiter.hit("b")

    limiter.reset("a")
    assert limiter.hit("a").allowed is True
    assert limiter.hit("b").allowed is False

    limiter.reset()
    assert limiter.hit("b").allowed is True


def test_the_configured_auth_policy_is_ten_per_fifteen_minutes() -> None:
    assert AUTH_RATE_LIMIT == 10
    assert AUTH_RATE_WINDOW_SECONDS == 15 * 60


# --- through the API -------------------------------------------------------
async def test_failed_logins_are_throttled(anon_client: AsyncClient, user: User) -> None:
    attempt = {"email": user.email, "password": "wrong password"}

    for index in range(AUTH_RATE_LIMIT):
        response = await anon_client.post(LOGIN, json=attempt)
        assert response.status_code == 401, index

    limited = await anon_client.post(LOGIN, json=attempt)
    assert limited.status_code == 429
    assert limited.json() == {
        "detail": "Too many requests. Please wait and try again.",
        "code": "rate_limited",
    }
    retry_after = limited.headers["Retry-After"]
    assert retry_after.isdigit() and 0 < int(retry_after) <= AUTH_RATE_WINDOW_SECONDS


async def test_throttling_also_blocks_the_correct_password(
    anon_client: AsyncClient, user: User
) -> None:
    """Otherwise the limit is trivially bypassed by the attempt that matters."""
    # Read the email up front: a failed login rolls the shared test session
    # back, which expires every loaded entity.
    email = user.email
    for _ in range(AUTH_RATE_LIMIT):
        await anon_client.post(LOGIN, json={"email": email, "password": "wrong"})

    response = await anon_client.post(
        LOGIN, json={"email": email, "password": TEST_PASSWORD}
    )
    assert response.status_code == 429


async def test_another_account_is_unaffected(
    anon_client: AsyncClient, user: User, other_user: User
) -> None:
    attacked, bystander = user.email, other_user.email

    for _ in range(AUTH_RATE_LIMIT + 1):
        await anon_client.post(LOGIN, json={"email": attacked, "password": "wrong"})

    response = await anon_client.post(
        LOGIN, json={"email": bystander, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200


async def test_fresh_emails_from_one_address_hit_the_ip_limit(
    anon_client: AsyncClient, test_settings
) -> None:
    """The per-email key never repeats here, so only the address-wide limiter
    can stop it — and it must, because every attempt costs a full argon2 hash."""
    limit = test_settings.AUTH_IP_RATE_LIMIT
    statuses = []
    for index in range(limit + 1):
        response = await anon_client.post(
            LOGIN, json={"email": f"nobody-{index}@example.com", "password": "guess"}
        )
        statuses.append(response.status_code)

    assert statuses[:limit] == [401] * limit
    assert statuses[limit] == 429
    assert int(statuses.count(429)) == 1


async def test_the_ip_limit_spans_register_and_login(
    anon_client: AsyncClient, test_settings
) -> None:
    limit = test_settings.AUTH_IP_RATE_LIMIT

    for index in range(limit):
        await anon_client.post(
            "/api/auth/register",
            json={
                "email": f"fresh-{index}@example.com",
                "password": "correct horse battery",
            },
        )

    blocked = await anon_client.post(
        LOGIN, json={"email": "someone@example.com", "password": "guess"}
    )
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"].isdigit()


async def test_the_ip_limit_is_checked_before_the_per_email_one(
    app, anon_client: AsyncClient, user: User
) -> None:
    """A blocked address must not consume anyone's per-account budget, or an
    attacker could lock accounts out from behind a limit they already hit."""
    per_email = RateLimiter(AUTH_RATE_LIMIT, AUTH_RATE_WINDOW_SECONDS)
    app.state.auth_rate_limiter = per_email
    app.state.auth_ip_rate_limiter = RateLimiter(1, AUTH_RATE_WINDOW_SECONDS)
    email = user.email

    assert (
        await anon_client.post(LOGIN, json={"email": email, "password": "wrong"})
    ).status_code == 401
    for _ in range(5):
        assert (
            await anon_client.post(LOGIN, json={"email": email, "password": "wrong"})
        ).status_code == 429

    # One attempt was charged to the account, not six.
    assert len(per_email._hits[f"127.0.0.1|{email}"]) == 1


async def test_the_ip_limit_is_configurable(
    app, anon_client: AsyncClient, test_settings
) -> None:
    from app.main import create_app

    tight = create_app(test_settings.model_copy(update={"AUTH_IP_RATE_LIMIT": 2}))
    assert tight.state.auth_ip_rate_limiter.limit == 2
    assert app.state.auth_ip_rate_limiter.limit == test_settings.AUTH_IP_RATE_LIMIT == 30


async def test_registration_shares_the_limiter(anon_client: AsyncClient) -> None:
    payload = {"email": "flood@example.com", "password": "correct horse battery"}

    first = await anon_client.post("/api/auth/register", json=payload)
    assert first.status_code == 201

    for _ in range(AUTH_RATE_LIMIT - 1):
        assert (
            await anon_client.post("/api/auth/register", json=payload)
        ).status_code == 409

    assert (
        await anon_client.post("/api/auth/register", json=payload)
    ).status_code == 429


# --- client address resolution ---------------------------------------------
class FakeRequest:
    def __init__(self, host: str | None, forwarded: str | None = None) -> None:
        self.client = SimpleNamespace(host=host) if host is not None else None
        self.headers = {} if forwarded is None else {"X-Forwarded-For": forwarded}


@pytest.mark.parametrize(
    "forwarded",
    [None, "203.0.113.7", "203.0.113.7, 198.51.100.9", "  "],
)
def test_untrusted_mode_always_uses_the_socket_address(forwarded) -> None:
    """The header is client-controlled; believing it here would hand every
    caller an unlimited supply of fresh rate-limit keys."""
    request = FakeRequest("10.0.0.5", forwarded)
    assert client_ip_from(request, trust_proxy_headers=False) == "10.0.0.5"


@pytest.mark.parametrize(
    ("forwarded", "expected"),
    [
        ("203.0.113.7", "203.0.113.7"),
        # Only the last hop was written by our own proxy; the earlier entries
        # came from the caller and can claim anything.
        ("1.2.3.4, 203.0.113.7", "203.0.113.7"),
        ("spoofed, 1.2.3.4 , 203.0.113.7", "203.0.113.7"),
        ("  203.0.113.7  ", "203.0.113.7"),
    ],
)
def test_trusted_mode_takes_the_last_forwarded_hop(
    forwarded: str, expected: str
) -> None:
    request = FakeRequest("10.0.0.5", forwarded)
    assert client_ip_from(request, trust_proxy_headers=True) == expected


@pytest.mark.parametrize("forwarded", [None, "", "   ", " , "])
def test_trusted_mode_falls_back_to_the_socket_address(forwarded) -> None:
    """A misconfigured proxy degrades to one shared key, not to no limit."""
    request = FakeRequest("10.0.0.5", forwarded)
    assert client_ip_from(request, trust_proxy_headers=True) == "10.0.0.5"


@pytest.mark.parametrize("trust", [True, False])
def test_a_missing_socket_peer_is_named_not_crashed(trust: bool) -> None:
    assert client_ip_from(FakeRequest(None), trust_proxy_headers=trust) == "unknown"


def test_proxy_headers_are_not_trusted_by_default() -> None:
    assert Settings.model_fields["TRUST_PROXY_HEADERS"].default is False


# --- the socket peer must itself be a proxy of ours ------------------------
@pytest.mark.parametrize(
    "peer", ["203.0.113.7", "8.8.8.8", "unknown", "::1", "not-an-address"]
)
def test_an_untrusted_peer_cannot_forge_its_address_even_when_trusting(
    peer: str,
) -> None:
    """The flag says "our proxy sets this header", not "nothing else reaches
    me". A published port or a sidecar is enough to connect directly, so the
    header is believed only when the *connection* comes from a trusted network.
    """
    request = FakeRequest(peer, "198.51.100.9")
    assert client_ip_from(request, trust_proxy_headers=True) == peer


@pytest.mark.parametrize("peer", ["10.0.0.5", "172.16.4.1", "192.168.1.2", "127.0.0.1"])
def test_the_default_trusted_networks_are_the_private_ranges(peer: str) -> None:
    request = FakeRequest(peer, "198.51.100.9")
    assert client_ip_from(request, trust_proxy_headers=True) == "198.51.100.9"


def test_the_trusted_networks_are_configurable() -> None:
    request = FakeRequest("203.0.113.7", "198.51.100.9")
    assert (
        client_ip_from(
            request,
            trust_proxy_headers=True,
            trusted_proxy_cidrs=["203.0.113.0/24"],
        )
        == "198.51.100.9"
    )
    # An empty list trusts nobody, which is the safe direction.
    assert (
        client_ip_from(request, trust_proxy_headers=True, trusted_proxy_cidrs=[])
        == "203.0.113.7"
    )


@pytest.mark.parametrize(
    ("peer", "cidrs", "trusted"),
    [
        ("10.0.0.5", DEFAULT_TRUSTED_PROXY_CIDRS, True),
        ("11.0.0.5", DEFAULT_TRUSTED_PROXY_CIDRS, False),
        ("127.0.0.1", DEFAULT_TRUSTED_PROXY_CIDRS, True),
        ("127.0.0.2", DEFAULT_TRUSTED_PROXY_CIDRS, False),
        ("unknown", DEFAULT_TRUSTED_PROXY_CIDRS, False),
        ("10.0.0.5", ["nonsense"], False),
    ],
)
def test_is_trusted_proxy(peer: str, cidrs, trusted: bool) -> None:
    assert is_trusted_proxy(peer, cidrs) is trusted


def test_an_invalid_trusted_cidr_is_refused_at_startup() -> None:
    """Silently dropping a typo would leave a deployment believing it trusts a
    proxy it does not — or a range wider than intended."""
    with pytest.raises(ValidationError):
        Settings(JWT_SECRET="x" * 40, TRUSTED_PROXY_CIDRS=["10.0.0.0/8", "oops"])


def test_trusted_cidrs_accept_a_comma_separated_environment_value() -> None:
    settings = Settings(
        JWT_SECRET="x" * 40, TRUSTED_PROXY_CIDRS="10.0.0.0/8, 192.168.0.0/16"
    )
    assert settings.TRUSTED_PROXY_CIDRS == ["10.0.0.0/8", "192.168.0.0/16"]


async def test_forwarded_clients_are_limited_independently_when_trusted(
    engine, db_session, test_settings, user: User
) -> None:
    """Behind nginx every request shares the proxy's socket address; with the
    header trusted, one attacker must not exhaust everyone else's budget."""
    trusting = test_settings.model_copy(update={"TRUST_PROXY_HEADERS": True})
    app = create_app(trusting)
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)

    async def override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    email = user.email
    attempt = {"email": email, "password": "wrong"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        attacker = {"X-Forwarded-For": "203.0.113.7"}
        for _ in range(AUTH_RATE_LIMIT):
            response = await client.post(LOGIN, json=attempt, headers=attacker)
            assert response.status_code == 401
        assert (
            await client.post(LOGIN, json=attempt, headers=attacker)
        ).status_code == 429

        # A different forwarded client, same socket address, still gets through.
        bystander = {"X-Forwarded-For": "198.51.100.9"}
        assert (
            await client.post(LOGIN, json=attempt, headers=bystander)
        ).status_code == 401


async def test_forwarded_headers_are_ignored_when_untrusted(
    anon_client: AsyncClient, user: User
) -> None:
    """Rotating the header must not reset the counter in the default mode."""
    attempt = {"email": user.email, "password": "wrong"}

    for index in range(AUTH_RATE_LIMIT):
        response = await anon_client.post(
            LOGIN, json=attempt, headers={"X-Forwarded-For": f"203.0.113.{index}"}
        )
        assert response.status_code == 401

    blocked = await anon_client.post(
        LOGIN, json=attempt, headers={"X-Forwarded-For": "203.0.113.200"}
    )
    assert blocked.status_code == 429


async def test_the_window_expires_for_the_api_too(
    app, anon_client: AsyncClient, user: User
) -> None:
    """Same limiter instance, but with a clock the test controls."""
    clock = FakeClock()
    app.state.auth_rate_limiter = RateLimiter(
        AUTH_RATE_LIMIT, AUTH_RATE_WINDOW_SECONDS, clock=clock
    )
    attempt = {"email": user.email, "password": "wrong password"}

    for _ in range(AUTH_RATE_LIMIT):
        await anon_client.post(LOGIN, json=attempt)
    assert (await anon_client.post(LOGIN, json=attempt)).status_code == 429

    clock.advance(AUTH_RATE_WINDOW_SECONDS + 1)
    assert (await anon_client.post(LOGIN, json=attempt)).status_code == 401
