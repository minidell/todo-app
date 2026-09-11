"""Application settings from the environment (master spec §9.1).

No secret ever has a hardcoded fallback: ``JWT_SECRET`` has **no default** and
must come from the environment. It is typed optional only so that a missing
value produces the actionable error :func:`app.main.require_jwt_secret` raises
("set JWT_SECRET…") instead of a bare pydantic traceback at import time; a
value that *is* present is validated here and rejected when it is too short to
be a credible HS256 key.
"""

import logging
from functools import lru_cache
from ipaddress import ip_network
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: Imported rather than duplicated: the behaviour and its default belong
#: together in ``app.events``, which imports nothing from this module (so there
#: is no cycle) and is where a reader looking at the eviction logic will be.
from app.events import DEFAULT_MAX_STREAMS_PER_USER

#: Anything shorter is trivially brute-forceable for an HS256 signing key.
MIN_JWT_SECRET_LENGTH = 32

#: The one implemented realtime fan-out (master §7.3, §9.1). See
#: ``Settings.REALTIME_BACKEND``.
REALTIME_BACKEND_MEMORY = "memory"

#: Logged once at startup, always. The single-replica constraint used to be a
#: comment in a source file; nothing stopped ``--scale backend=3`` and the
#: failure was silent (some tabs simply stop updating), so it is said out loud
#: where an operator reading the logs will meet it.
REALTIME_STARTUP_NOTICE = (
    "Realtime fan-out: in-process broker (REALTIME_BACKEND=%s) — run exactly "
    "ONE backend replica; with more, some tabs miss events."
)

# --------------------------------------------------------------------------- #
# Placeholder secret table (iteration 4, IT3-3)
#
# KEEP IN SYNC WITH ``ai-agent/app/config.py`` — the three tables below and
# ``is_placeholder_secret`` are duplicated **verbatim** in that file. The two
# services are separate ``uv`` projects with no shared package (decision
# D-IT4-3), so the duplication is deliberate; it is kept honest by an identical
# test corpus in both suites (``backend/tests/test_config.py`` and
# ``ai-agent/tests/test_config.py``), so drift fails a test rather than a
# deployment. Edit both files or neither.
#
# All three are matched on ``value.strip().casefold()``: retyping a placeholder
# in capitals, or leaving a stray space when copying ``.env.example``, does not
# make it a secret.
# --------------------------------------------------------------------------- #

#: Prefix every documented placeholder in this repository shares. Kept as its
#: own name because the error copy and older tests refer to it.
PLACEHOLDER_PREFIX = "change-me"

#: Whole-value matches: every placeholder literal this repository has shipped,
#: from *both* services, so the table is identical on both sides. The first is
#: ``.env.example``'s ``AI_AGENT_TOKEN`` and is exactly MIN_TOKEN_LENGTH
#: characters long — which is precisely why a length floor alone is not enough.
#: Both are also caught by the ``change-me`` prefix; they stay listed so the
#: table still documents what the repository actually publishes.
PLACEHOLDER_EXACT = frozenset(
    {
        "change-me-internal-shared-secret",
        "change-me-generate-with-openssl-rand-hex-32",
    }
)

#: A value *starting* with any of these was copied from documentation, not
#: generated. Prefixes only — no entropy heuristic — so ``openssl rand -hex 32``
#: output cannot match one by accident.
PLACEHOLDER_PREFIXES = ("change-me", "change_me", "changeme",
                        "replace-me", "replace_me", "replaceme",
                        "your-", "your_", "yoursecret",
                        "example", "placeholder", "insert-",
                        "dummy", "sample", "secret-")

#: Caught anywhere in the value, for the operator who prefixed or suffixed a
#: placeholder instead of replacing it ("prod-change-me-please").
PLACEHOLDER_SUBSTRINGS = ("change-me", "change_me", "changeme",
                          "replace-me", "replaceme", "placeholder",
                          "your-secret", "your_secret", "insert-your",
                          "do-not-use", "donotuse")

#: Signing keys that are published in this repository and must never sign a
#: real token. The one ``.env.example`` ships is **43 characters** — comfortably
#: past :data:`MIN_JWT_SECRET_LENGTH` — so length alone accepts it, and an
#: unedited env file would mint tokens anyone reading this repo can forge.
#: Length answers "is this key big enough to brute-force?"; this answers "does
#: everyone already know it?", and only the second one catches a copied file.
PLACEHOLDER_JWT_SECRETS = frozenset(
    {"change-me-generate-with-openssl-rand-hex-32"}
)


#: The shared secret between backend and ai-agent is a bearer credential for a
#: service that will run a language model on your hardware. It gets the same
#: floor as the signing key — ``openssl rand -hex 32`` produces 64.
MIN_AI_AGENT_TOKEN_LENGTH = 32

#: The value ``.env.example`` ships (master §9.2). It is **exactly 32
#: characters**, so the length rule above waves it straight through — which is
#: the whole reason it is named here. A deployment that copied .env.example and
#: never edited it would otherwise pass every check while running a shared
#: secret that is published in this repository.
PLACEHOLDER_AI_AGENT_TOKEN = "change-me-internal-shared-secret"

def is_placeholder_secret(value: str, known: frozenset[str] = frozenset()) -> bool:
    """Whether ``value`` is documentation copy rather than a generated secret.

    True on an exact match against ``known | PLACEHOLDER_EXACT``, on any
    :data:`PLACEHOLDER_PREFIXES` prefix, or on any
    :data:`PLACEHOLDER_SUBSTRINGS` substring — all casefolded and
    whitespace-stripped.

    ``known`` lets a call site add the literals specific to its own variable
    without widening the shared table.
    """
    candidate = value.strip().casefold()
    if candidate in {known_value.casefold() for known_value in known | PLACEHOLDER_EXACT}:
        return True
    if candidate.startswith(PLACEHOLDER_PREFIXES):
        return True
    return any(fragment in candidate for fragment in PLACEHOLDER_SUBSTRINGS)

#: Networks a reverse proxy of ours may plausibly live in: the private ranges
#: a container network hands out, plus the loopback a host-mode proxy uses.
#: Nothing routable from the internet is trusted by default.
DEFAULT_TRUSTED_PROXY_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "127.0.0.1/32",
)


def _split_csv(value: object) -> object:
    """Accept ``a,b`` as well as a real list for the list-valued settings."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


#: ``NoDecode`` is load-bearing, not decoration: pydantic-settings treats a
#: ``list`` field as "complex" and JSON-decodes the raw environment value
#: *before* any validator runs, so the plain
#: ``CORS_ORIGINS=http://localhost:5173`` that compose passes would fail to
#: parse as JSON and the app would die at import. ``NoDecode`` hands the raw
#: string to ``_split_csv`` instead.
CorsOrigins = Annotated[list[str], NoDecode, BeforeValidator(_split_csv)]
CidrList = Annotated[list[str], NoDecode, BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        #: Two of these fields are secrets, and a startup ``ValidationError`` is
        #: printed straight into the container log. By default pydantic appends
        #: ``input_value='…'`` to each error, so a rejected JWT_SECRET or
        #: AI_AGENT_TOKEN would be logged in full. That was harmless while the
        #: placeholder guard only matched values that were already public;
        #: since iteration 4 (IT3-3) it also fires on *near* misses — an
        #: operator's real secret that merely contains "sample" — so the echo
        #: has to go (acceptance criterion 19). Messages are unaffected.
        hide_input_in_errors=True,
    )

    DATABASE_URL: str = "postgresql+asyncpg://todo:todo@localhost:5433/todo"
    TEST_DATABASE_URL: str | None = None
    APP_ENV: Literal["dev", "prod"] = "dev"
    CORS_ORIGINS: CorsOrigins = Field(default_factory=lambda: ["http://localhost:5173"])
    MAX_BODY_BYTES: int = 64 * 1024
    #: HS256 signing key. Required — there is deliberately no default.
    JWT_SECRET: str | None = None
    JWT_EXPIRES_MINUTES: int = Field(default=60, gt=0)
    #: Credential attempts allowed per client address across the whole auth
    #: surface, per 15-minute window. This is the ceiling on how much argon2
    #: work one address can make the server do; the per-email limit (10) sits
    #: underneath it and protects an individual account.
    AUTH_IP_RATE_LIMIT: int = Field(default=30, gt=0)
    #: How many argon2 hashes may run concurrently. Each one holds ~64 MiB for
    #: its duration, so this number *is* the memory ceiling of password
    #: hashing (4 × 64 MiB ≈ 256 MiB). Without it the calls would inherit
    #: AnyIO's 40-slot default thread pool — ~2.5 GiB — and a burst well inside
    #: the rate limits could OOM-kill the process instead of merely queueing.
    ARGON2_MAX_CONCURRENCY: int = Field(default=4, gt=0)
    #: Whether ``X-Forwarded-For`` may be believed. **Only** enable this when a
    #: proxy you control (the compose nginx) is the sole way in and it sets the
    #: header itself — a directly reachable app that trusts it lets any client
    #: forge its own address and walk around the rate limiter.
    TRUST_PROXY_HEADERS: bool = False
    #: Which socket peers may speak for someone else. ``TRUST_PROXY_HEADERS``
    #: alone is not enough: if the app is reachable directly as well as through
    #: the proxy — a published port, a sidecar, a misrouted health checker —
    #: then a client that connects straight to it can still forge its own
    #: address. The header is honoured only when the *connection* comes from
    #: one of these networks.
    TRUSTED_PROXY_CIDRS: CidrList = Field(
        default_factory=lambda: list(DEFAULT_TRUSTED_PROXY_CIDRS)
    )
    #: Concurrent SSE streams one account may hold open. A person uses a
    #: handful of tabs; past the cap the oldest stream is evicted rather than
    #: the newest refused, so the tab the user is actually looking at always
    #: wins. See ``app/events.py``.
    MAX_STREAMS_PER_USER: int = Field(
        default=DEFAULT_MAX_STREAMS_PER_USER, gt=0
    )
    #: Master switch for ``/api/ai/*`` (master §9.1). Off means the endpoints
    #: answer 503 ``ai_disabled`` and ``/api/ai/status`` reports
    #: ``enabled: false``, which hides the AI section entirely — the app is
    #: fully usable with no AI at all, and that is a supported configuration
    #: rather than a degraded one.
    AI_ENABLED: bool = True
    #: Where the private ai-agent service lives. Never reachable from a browser.
    AI_AGENT_URL: str = "http://ai-agent:8000"
    #: Shared secret sent as ``X-Internal-Token``. Required when AI is enabled;
    #: no default, like every other secret here.
    AI_AGENT_TOKEN: str | None = None
    #: Backend → ai-agent read budget. Sits between ai-agent's 45 s wait on
    #: Ollama and the frontend's 60 s abort so each layer fails before the one
    #: above it (master §8.3).
    AI_TIMEOUT_SECONDS: int = Field(default=50, gt=0)
    #: AI calls allowed per user per :data:`AI_RATE_WINDOW_SECONDS` (§6.6).
    AI_RATE_LIMIT: int = Field(default=20, gt=0)
    #: Level for this application's own loggers (``app.*``). Nothing else reads
    #: it: uvicorn keeps its own configuration, and we deliberately do not touch
    #: the root logger's level — see ``app.main.configure_logging``.
    LOG_LEVEL: str = "INFO"

    @field_validator("LOG_LEVEL")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        """Reject a level name the logging module does not know.

        ``Logger.setLevel`` raises on an unknown name, and it would do so from
        inside application startup — a misspelled ``LOG_LEVEL=INF`` would take
        the whole service down with a traceback about a logging internal.
        Failing here instead names the variable and lists the valid values.
        """
        candidate = value.strip().upper()
        known = logging.getLevelNamesMapping()
        if candidate not in known:
            raise ValueError(
                f"LOG_LEVEL={value!r} is not a logging level. Use one of: "
                + ", ".join(sorted(known, key=lambda name: known[name]))
            )
        return candidate
    #: How realtime frames are fanned out (master §7.3, §9.1 — iteration 4,
    #: D-IT4-4). ``memory`` is the only implementation: the broker is a
    #: dictionary in this process, so **exactly one backend replica may run** —
    #: with more, each write reaches only the tabs connected to the replica
    #: that served it and the others silently stop updating. The name is
    #: reserved now so that a future ``redis`` or ``postgres`` fan-out is an
    #: additive change rather than a name invented under pressure.
    REALTIME_BACKEND: str = "memory"

    @field_validator("REALTIME_BACKEND")
    @classmethod
    def _validate_realtime_backend(cls, value: str) -> str:
        """Fail loudly on anything else, rather than typing it as a ``Literal``.

        Pydantic's own ``Literal`` error ("Input should be 'memory'") tells an
        operator what the parser wanted, not what they lost: the whole point of
        this setting is that scaling out silently breaks realtime, so the
        message has to say so and point at the README.
        """
        normalized = value.strip().casefold()
        if normalized != REALTIME_BACKEND_MEMORY:
            raise ValueError(
                f"REALTIME_BACKEND={value!r} is not supported. The only "
                f"implemented value is {REALTIME_BACKEND_MEMORY!r}: realtime "
                "fan-out is in-process, so exactly one backend replica may run "
                "(with more, some tabs stop receiving events). Distributed "
                "fan-out — Postgres LISTEN/NOTIFY or Redis pub/sub — is not "
                "implemented; see the README section \"Realtime\"."
            )
        return normalized

    @field_validator("AI_AGENT_TOKEN")
    @classmethod
    def _empty_token_is_missing(cls, value: str | None) -> str | None:
        """``AI_AGENT_TOKEN: ${AI_AGENT_TOKEN:-}`` in compose arrives as ``""``.

        Treating that as "set" would send an empty shared secret and produce a
        401 from ai-agent that reads like a network fault; treating it as
        missing produces the actionable startup error instead.
        """
        return value or None

    @model_validator(mode="after")
    def _validate_ai_agent_token(self) -> "Settings":
        """A shared secret that is *present* must be a real one.

        Only reachable when ``AI_ENABLED`` is true: with AI off the value is
        never sent anywhere, and refusing to start over an inert string would
        break the "fully usable with no AI at all" guarantee — someone who
        copied ``.env.example`` and set ``AI_ENABLED=false`` has nothing to fix.

        The *absence* of a token is deliberately not checked here. It cannot be:
        ``AI_ENABLED`` defaults to true, so a bare ``Settings()`` — which every
        dev script and half the test suite constructs — would explode. That case
        belongs to :func:`app.main.require_ai_agent_token`, which knows it is
        building an application and can say so.
        """
        if not self.AI_ENABLED or self.AI_AGENT_TOKEN is None:
            return self

        # Shared with JWT_SECRET: same convention, same normalisation, and the
        # ``change-me`` prefix keeps covering placeholders reworded later.
        if is_placeholder_secret(
            self.AI_AGENT_TOKEN, frozenset({PLACEHOLDER_AI_AGENT_TOKEN})
        ):
            raise ValueError(
                "AI_AGENT_TOKEN is a placeholder, not a secret: it matches an "
                "example value published in this repository or an obvious "
                "template (change-me…, replace-me…, your-secret…, "
                "placeholder…). Anyone who can reach the ai-agent service can "
                "guess it. Generate a real one with: openssl rand -hex 32 — or "
                "set AI_ENABLED=false to run without AI."
            )

        if len(self.AI_AGENT_TOKEN) < MIN_AI_AGENT_TOKEN_LENGTH:
            raise ValueError(
                "AI_AGENT_TOKEN must be at least "
                f"{MIN_AI_AGENT_TOKEN_LENGTH} characters (got "
                f"{len(self.AI_AGENT_TOKEN)}); it is the only thing standing "
                "between a caller and a language model running on your "
                "hardware. Generate one with: openssl rand -hex 32 — or set "
                "AI_ENABLED=false to run without AI."
            )
        return self

    @field_validator("TRUSTED_PROXY_CIDRS")
    @classmethod
    def _validate_cidrs(cls, value: list[str]) -> list[str]:
        """Reject an unparseable network at startup.

        Silently dropping a typo would leave the deployment believing it trusts
        its proxy while every forwarded address is ignored (or, worse, while a
        wider range than intended is trusted).
        """
        for entry in value:
            try:
                ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"TRUSTED_PROXY_CIDRS entry {entry!r} is not a valid network: {exc}"
                ) from exc
        return value

    @field_validator("JWT_SECRET")
    @classmethod
    def _validate_jwt_secret(cls, value: str | None) -> str | None:
        """Reject a present-but-weak key.

        An empty value (``JWT_SECRET: ${JWT_SECRET:-}`` in compose when the
        variable is unset) is treated as missing so the caller gets the
        "set JWT_SECRET" message rather than "too short".

        The placeholder check is not redundant with the length check and is the
        more important of the two: the value ``.env.example`` ships is 43
        characters, so it passes the length rule outright. A deployment that
        copied the file and edited nothing would sign every access token with a
        key published in this repository — anyone could mint a token for any
        account. Length asks whether the key is big enough to resist brute
        force; this asks whether everyone already knows it.
        """
        if value is None or value == "":
            return None
        if is_placeholder_secret(value, PLACEHOLDER_JWT_SECRETS):
            raise ValueError(
                "JWT_SECRET is a placeholder, not a key: it matches an example "
                "value published in this repository or an obvious template "
                "(change-me…, replace-me…, your-secret…, placeholder…). It "
                "signs every access token, so anyone who can guess it could "
                "mint a token for any account. Generate a real one with: "
                "openssl rand -hex 32"
            )
        if len(value) < MIN_JWT_SECRET_LENGTH:
            raise ValueError(
                "JWT_SECRET must be at least "
                f"{MIN_JWT_SECRET_LENGTH} characters (got {len(value)}); "
                "generate one with: openssl rand -hex 32"
            )
        return value

    @property
    def docs_enabled(self) -> bool:
        return self.APP_ENV == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
