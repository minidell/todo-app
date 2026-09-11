"""Settings loaded **through the environment source**.

These tests deliberately go through ``Settings()`` with monkeypatched env vars
rather than ``Settings(CORS_ORIGINS=[...])``: keyword construction skips the
environment source entirely, which is exactly how a crash-on-boot parsing bug
stayed invisible. Everything here mirrors what compose actually passes.
"""

import logging
from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from app.config import (
    MIN_AI_AGENT_TOKEN_LENGTH,
    MIN_JWT_SECRET_LENGTH,
    PLACEHOLDER_AI_AGENT_TOKEN,
    PLACEHOLDER_EXACT,
    PLACEHOLDER_JWT_SECRETS,
    PLACEHOLDER_PREFIXES,
    PLACEHOLDER_SUBSTRINGS,
    REALTIME_BACKEND_MEMORY,
    Settings,
    get_settings,
    is_placeholder_secret,
)

# --------------------------------------------------------------------------- #
# The shared corpora (identical in ai-agent/tests/test_config.py)
# --------------------------------------------------------------------------- #
#
# KEEP IN SYNC WITH ``ai-agent/tests/test_config.py``. ``PLACEHOLDER_CORPUS``
# and ``REAL_SECRET_CORPUS`` below are duplicated **verbatim** in that file, as
# are the three tables in ``app/config.py``. The services are separate ``uv``
# projects with no shared package (decision D-IT4-3), so this pair of identical
# corpora is what keeps the duplication honest: if somebody widens one service's
# table and not the other's, ``test_every_prefix_is_represented_in_the_corpus``
# or one of the corpus rows fails here rather than a deployment failing later.
#
# The corpora are shared, but the *variables* are not: this file asserts
# ``JWT_SECRET`` and its own ``AI_AGENT_TOKEN``, the ai-agent copy asserts
# ``AI_AGENT_TOKEN``.

#: Values that must never start a service. Every entry is at least
#: MIN_TOKEN_LENGTH characters, so each one is rejected *as a placeholder* and
#: not incidentally by the length floor — which would prove nothing about this
#: guard. Between them they cover every prefix and every substring in the
#: table, both cases, and leading/trailing whitespace.
PLACEHOLDER_CORPUS = [
    # -- the literals this repository actually ships -------------------------
    "change-me-internal-shared-secret",
    "change-me-generate-with-openssl-rand-hex-32",
    # -- one per prefix ------------------------------------------------------
    "change_me_this_is_not_a_real_secret",
    "changeme-before-you-deploy-anything",
    "replace-me-with-openssl-rand-hex-32",
    "replace_me_with_a_generated_secret!",
    "replaceme-before-the-first-deploy-x",
    "your-secret-goes-right-here-please!",
    "your_secret_goes_right_here_please!",
    "yoursecret-goes-right-here-ok-then!",
    "example-token-for-the-documentation",
    "placeholder-value-do-not-deploy-it!",
    "insert-your-generated-secret-here!!",
    "dummy-token-used-only-in-the-docs!!",
    "sample-token-from-the-readme-file!!",
    "secret-goes-here-generate-a-real-1!",
    # -- one per substring, none of them at the start ------------------------
    "prod-change-me-please-before-deploy",
    "prod-change_me_please_before_deploy",
    "prod-changeme-please-before-deploy!",
    "prod-replace-me-before-deploying-it",
    "prod-replaceme-before-you-deploy-it",
    "prod-placeholder-value-fix-before-1",
    "prod-your-secret-goes-right-in-here",
    "prod-your_secret_goes_right_in_here",
    "prod-insert-your-real-secret-here!!",
    "prod-do-not-use-this-token-anywhere",
    "prod-donotuse-this-token-anywhere!!",
    # -- case and whitespace -------------------------------------------------
    "  change-me-internal-shared-secret  ",
    "CHANGE-ME-GENERATE-WITH-OPENSSL-RAND-HEX-32",
    "PlaceHolder-Value-Do-Not-Deploy-It!",
    "\tDummy-Token-Used-Only-In-The-Docs\n",
]

#: Values that must keep starting the services. A false positive here is worse
#: than a false negative: it refuses to boot a correctly configured deployment
#: (risk R4). The last two are the harness constants both suites run on, so a
#: rule that would redden the suites fails here first, with a clear name.
REAL_SECRET_CORPUS = [
    # `openssl rand -hex 32` output, as a fixed literal so both suites are equal
    "9f2c41a7be0d38e5c6147a90fbd2e83c5a71904de6b823fc1d05e7a94b6c38af",
    # a 40-character base64-ish key, the other common shape
    "kQ8vZ2mT4rXw9LpB6nHc3JdF7yGsA1eR5uYiO0Pz",
    # ai-agent/tests/conftest.py::TEST_TOKEN
    "test-internal-token-0123456789abcdef",
    # backend/tests/conftest.py::TEST_AI_AGENT_TOKEN
    "test-only-internal-token-not-a-real-secret",
    # backend/tests/conftest.py::TEST_JWT_SECRET
    "test-only-jwt-secret-not-a-real-key-0123456789",
    # ai-agent/tests/test_live_ollama.py::LIVE_TOKEN
    "live-test-token-0123456789abcdef0123",
]


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """``get_settings`` is lru_cached; never leak one test's env into another."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch):
    """A clean slate: no .env file, no inherited variables."""
    for name in (
        "DATABASE_URL",
        "TEST_DATABASE_URL",
        "APP_ENV",
        "CORS_ORIGINS",
        "MAX_BODY_BYTES",
        "JWT_SECRET",
        "JWT_EXPIRES_MINUTES",
        "AI_ENABLED",
        "AI_AGENT_URL",
        "AI_AGENT_TOKEN",
        "AI_TIMEOUT_SECONDS",
        "AI_RATE_LIMIT",
        "REALTIME_BACKEND",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _settings(env, **values: str) -> Settings:
    for name, value in values.items():
        env.setenv(name, value)
    return Settings(_env_file=None)


def test_single_origin_is_not_json_decoded(env) -> None:
    """The regression: compose passes exactly this, unquoted."""
    settings = _settings(env, CORS_ORIGINS="http://localhost:5173")
    assert settings.CORS_ORIGINS == ["http://localhost:5173"]


def test_comma_separated_origins_become_a_list(env) -> None:
    settings = _settings(
        env, CORS_ORIGINS="http://localhost:5173,http://localhost:4173"
    )
    assert settings.CORS_ORIGINS == [
        "http://localhost:5173",
        "http://localhost:4173",
    ]


def test_origins_are_trimmed_and_empties_dropped(env) -> None:
    settings = _settings(
        env, CORS_ORIGINS=" http://a.example , , http://b.example ,"
    )
    assert settings.CORS_ORIGINS == ["http://a.example", "http://b.example"]


def test_origins_default_when_unset(env) -> None:
    assert _settings(env).CORS_ORIGINS == ["http://localhost:5173"]


def test_the_format_is_comma_separated_not_json(env) -> None:
    """Pins the documented format (master §9.1).

    With ``NoDecode`` the value is never JSON-parsed, so a JSON array is taken
    literally as one origin rather than crashing. It simply never matches a
    real ``Origin`` header, which is the safe failure direction.
    """
    settings = _settings(env, CORS_ORIGINS='["http://a.example"]')
    assert settings.CORS_ORIGINS == ['["http://a.example"]']


def test_max_body_bytes_is_coerced_from_a_string(env) -> None:
    assert _settings(env, MAX_BODY_BYTES="1024").MAX_BODY_BYTES == 1024


def test_max_body_bytes_defaults_to_64_kib(env) -> None:
    assert _settings(env).MAX_BODY_BYTES == 64 * 1024


def test_jwt_secret_is_read_from_the_environment(env) -> None:
    secret = "e" * 64  # test-only, not a secret
    assert _settings(env, JWT_SECRET=secret).JWT_SECRET == secret


def test_jwt_secret_is_absent_by_default(env) -> None:
    """No fallback anywhere: an unset variable must stay unset, not become a
    built-in signing key."""
    assert _settings(env).JWT_SECRET is None


def test_an_empty_jwt_secret_from_the_environment_reads_as_missing(env) -> None:
    """Compose passes ``JWT_SECRET: ${JWT_SECRET:-}``, i.e. an empty string when
    the operator forgot it; that must fail as "missing", not as "too short"."""
    assert _settings(env, JWT_SECRET="").JWT_SECRET is None


def test_a_short_jwt_secret_from_the_environment_is_refused(env) -> None:
    with pytest.raises(Exception, match="JWT_SECRET"):
        _settings(env, JWT_SECRET="x" * 31)


def test_the_shipped_jwt_placeholder_is_refused_despite_being_long_enough(
    env,
) -> None:
    """The one the length rule cannot catch.

    ``.env.example`` ships a 43-character placeholder, so ``MIN_JWT_SECRET_LENGTH``
    accepts it outright. A deployment that copied the file and edited nothing
    would sign every access token with a key published in this repository —
    anyone reading it could mint a token for any account.
    """
    shipped = "change-me-generate-with-openssl-rand-hex-32"
    assert len(shipped) >= MIN_JWT_SECRET_LENGTH  # the length rule says yes

    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _settings(env, JWT_SECRET=shipped)


@pytest.mark.parametrize(
    "spelling",
    [
        "CHANGE-ME-GENERATE-WITH-OPENSSL-RAND-HEX-32",
        "Change-Me-Generate-With-Openssl-Rand-Hex-32",
        "  change-me-generate-with-openssl-rand-hex-32  ",
        "change-me-something-else-entirely-but-long-enough",
    ],
)
def test_any_change_me_jwt_secret_is_refused(env, spelling: str) -> None:
    """Normalised on case and whitespace, and matched on the ``change-me``
    prefix so a reworded placeholder is still caught."""
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        _settings(env, JWT_SECRET=spelling)


def test_a_real_looking_jwt_secret_is_still_accepted(env) -> None:
    """The guard must not reject the value operators are told to generate."""
    generated = "9f" * 32  # what `openssl rand -hex 32` produces
    assert _settings(env, JWT_SECRET=generated).JWT_SECRET == generated


# --- the widened placeholder guard (IT3-3) ---------------------------------


@pytest.mark.parametrize("value", PLACEHOLDER_CORPUS)
def test_the_placeholder_corpus_is_rejected_as_a_jwt_secret(env, value: str) -> None:
    """Criterion 16: literals, prefixes and substrings, casefolded and stripped."""
    with pytest.raises(ValidationError) as excinfo:
        _settings(env, JWT_SECRET=value)

    message = str(excinfo.value)
    assert "JWT_SECRET" in message
    assert "openssl rand -hex 32" in message


@pytest.mark.parametrize("value", PLACEHOLDER_CORPUS)
def test_the_placeholder_corpus_is_rejected_as_an_ai_token(env, value: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _settings(env, AI_ENABLED="true", AI_AGENT_TOKEN=value)

    message = str(excinfo.value)
    assert "AI_AGENT_TOKEN" in message
    assert "openssl rand -hex 32" in message


@pytest.mark.parametrize("value", PLACEHOLDER_CORPUS)
def test_the_predicate_agrees_with_both_call_sites(value: str) -> None:
    assert is_placeholder_secret(value) is True


@pytest.mark.parametrize("value", REAL_SECRET_CORPUS)
def test_a_real_shaped_secret_is_accepted_everywhere(env, value: str) -> None:
    """Risk R4: a false positive here means the app refuses to start.

    The corpus is every secret literal the two suites already use plus the
    shapes the README tells an operator to generate.
    """
    assert is_placeholder_secret(value) is False
    assert _settings(env, JWT_SECRET=value).JWT_SECRET == value
    assert (
        _settings(env, AI_ENABLED="true", AI_AGENT_TOKEN=value).AI_AGENT_TOKEN == value
    )


@pytest.mark.parametrize("value", PLACEHOLDER_CORPUS)
def test_the_rejected_value_is_never_echoed(env, value: str) -> None:
    """Criterion 19, and the reason ``hide_input_in_errors`` is set.

    pydantic otherwise appends ``input_value='…'`` to every error, and a
    startup ``ValidationError`` is printed straight into the container log.
    While the guard only matched already-public values that was harmless; the
    widened table also fires on *near* misses — a real secret that merely
    contains "sample" — so the echo has to go.
    """
    with pytest.raises(ValidationError) as excinfo:
        _settings(env, JWT_SECRET=value)
    assert value.strip() not in str(excinfo.value)

    with pytest.raises(ValidationError) as excinfo:
        _settings(env, AI_ENABLED="true", AI_AGENT_TOKEN=value)
    assert value.strip() not in str(excinfo.value)


def test_a_rejected_short_secret_is_not_echoed_either(env) -> None:
    """The length rule fires on real secrets by definition, so it matters more."""
    too_short = "9f2c41a7be0d38e5c6147a90fbd2e"

    with pytest.raises(ValidationError) as excinfo:
        _settings(env, JWT_SECRET=too_short)

    assert too_short not in str(excinfo.value)
    assert "at least 32 characters" in str(excinfo.value)


@pytest.mark.parametrize("prefix", PLACEHOLDER_PREFIXES)
def test_every_prefix_is_represented_in_the_corpus(prefix: str) -> None:
    """Adding a prefix to the table without a corpus row fails here.

    Which is the point: the corpus is the contract between the two copies of
    the table, so it has to be provably complete rather than merely long.
    """
    assert any(
        value.strip().casefold().startswith(prefix) for value in PLACEHOLDER_CORPUS
    )


@pytest.mark.parametrize("substring", PLACEHOLDER_SUBSTRINGS)
def test_every_substring_is_represented_in_the_corpus(substring: str) -> None:
    assert any(substring in value.strip().casefold() for value in PLACEHOLDER_CORPUS)


def test_every_shipped_literal_is_in_the_corpus() -> None:
    folded = {value.strip().casefold() for value in PLACEHOLDER_CORPUS}
    assert {literal.casefold() for literal in PLACEHOLDER_EXACT} <= folded
    # The per-variable literals this service names on its own are in there too.
    assert {
        PLACEHOLDER_AI_AGENT_TOKEN.casefold(),
        *(value.casefold() for value in PLACEHOLDER_JWT_SECRETS),
    } <= folded


def test_the_corpus_is_long_enough_to_be_meaningful() -> None:
    assert len(PLACEHOLDER_CORPUS) >= 12
    assert len(set(PLACEHOLDER_CORPUS)) == len(PLACEHOLDER_CORPUS)


def test_placeholders_are_rejected_on_their_own_merit_not_on_length() -> None:
    """Every corpus entry clears the length floor, so the guard is what fires."""
    for value in PLACEHOLDER_CORPUS:
        assert len(value.strip()) >= MIN_JWT_SECRET_LENGTH, value


def test_real_secrets_clear_the_length_floor_too() -> None:
    for value in REAL_SECRET_CORPUS:
        assert len(value) >= MIN_JWT_SECRET_LENGTH, value


def test_the_known_argument_widens_without_touching_the_table() -> None:
    """A call site can add its own literal; the shared table is unchanged."""
    own = "a-literal-only-this-call-site-knows"

    assert is_placeholder_secret(own) is False
    assert is_placeholder_secret(own, frozenset({own})) is True
    assert is_placeholder_secret(own.upper(), frozenset({own})) is True
    assert own not in PLACEHOLDER_EXACT


def test_the_guard_carries_no_entropy_rule() -> None:
    """A rejected design (spec §2 A3), pinned so it is not re-introduced.

    An entropy heuristic would reject these perfectly usable keys — and half
    the existing suite's fixtures with them — while adding nothing the length
    and placeholder rules do not already cover. The ``xxx`` prefix was dropped
    for the same reason (security review): it is that heuristic in disguise,
    and it false-positives on a base64 key that happens to start ``Xxx``.
    """
    for boring in ("0" * 64, "a" * 40, "x" * 40, "z" * 64, "f" * 64, "Xxx7vQ2m" * 5):
        assert is_placeholder_secret(boring) is False


def test_the_harness_secrets_survive_the_guard() -> None:
    """If this fails, the whole suite is about to fail for the same reason."""
    from tests.conftest import TEST_AI_AGENT_TOKEN, TEST_JWT_SECRET

    assert is_placeholder_secret(TEST_JWT_SECRET) is False
    assert is_placeholder_secret(TEST_AI_AGENT_TOKEN) is False


def test_jwt_expires_minutes_is_coerced_from_a_string(env) -> None:
    assert _settings(env, JWT_EXPIRES_MINUTES="15").JWT_EXPIRES_MINUTES == 15


def test_jwt_expires_minutes_defaults_to_an_hour(env) -> None:
    assert _settings(env).JWT_EXPIRES_MINUTES == 60


@pytest.mark.parametrize("bad", ["0", "-5", "abc"])
def test_jwt_expires_minutes_rejects_a_non_positive_value(env, bad: str) -> None:
    """A zero or negative lifetime would mint tokens that are already expired."""
    with pytest.raises(Exception):
        _settings(env, JWT_EXPIRES_MINUTES=bad)


def test_auth_enabled_is_no_longer_a_setting(env) -> None:
    """D-S1: the flag is gone, and an env var left over from slice 1 is ignored
    (``extra="ignore"``) rather than resurrecting a bypass."""
    env.setenv("AUTH_ENABLED", "false")
    settings = _settings(env, JWT_SECRET="f" * 64)  # test-only, not a secret
    assert not hasattr(settings, "AUTH_ENABLED")
    assert "AUTH_ENABLED" not in Settings.model_fields


def test_app_env_rejects_an_unknown_value(env) -> None:
    with pytest.raises(Exception):
        _settings(env, APP_ENV="staging")


@pytest.mark.parametrize(
    ("app_env", "docs_enabled"), [("dev", True), ("prod", False)]
)
def test_docs_enabled_follows_app_env(env, app_env: str, docs_enabled: bool) -> None:
    assert _settings(env, APP_ENV=app_env).docs_enabled is docs_enabled


def test_the_full_compose_environment_parses(env) -> None:
    """Every variable the compose backend service sets, in its raw form."""
    settings = _settings(
        env,
        DATABASE_URL="postgresql+asyncpg://todo:secret@db:5432/todo",
        APP_ENV="dev",
        CORS_ORIGINS="http://localhost:5173",
        MAX_BODY_BYTES="65536",
        JWT_SECRET="0123456789abcdef0123456789abcdef",  # test-only, not a secret
        JWT_EXPIRES_MINUTES="60",
        AI_ENABLED="true",
        AI_AGENT_URL="http://ai-agent:8000",
        # What an operator is told to generate, not the .env.example
        # placeholder — that value is rejected on purpose, and the tests for it
        # live under "AI_AGENT_TOKEN strength" below.
        AI_AGENT_TOKEN="1f" * 32,  # test-only, not a secret
        AI_TIMEOUT_SECONDS="50",
    )
    assert settings.DATABASE_URL.endswith("/todo")
    assert settings.CORS_ORIGINS == ["http://localhost:5173"]
    assert settings.MAX_BODY_BYTES == 65536
    assert settings.JWT_SECRET == "0123456789abcdef0123456789abcdef"
    assert settings.JWT_EXPIRES_MINUTES == 60
    assert settings.AI_ENABLED is True
    assert settings.AI_AGENT_URL == "http://ai-agent:8000"
    assert settings.AI_TIMEOUT_SECONDS == 50


# --- AI settings (slice 4) -------------------------------------------------


def test_ai_defaults_match_the_master_spec(env) -> None:
    settings = _settings(env)
    assert settings.AI_ENABLED is True
    assert settings.AI_AGENT_URL == "http://ai-agent:8000"
    assert settings.AI_AGENT_TOKEN is None  # no fallback, ever
    assert settings.AI_TIMEOUT_SECONDS == 50
    assert settings.AI_RATE_LIMIT == 20


@pytest.mark.parametrize("value", ["false", "0", "False"])
def test_ai_can_be_switched_off_the_way_compose_spells_it(env, value: str) -> None:
    assert _settings(env, AI_ENABLED=value).AI_ENABLED is False


def test_an_empty_ai_token_is_treated_as_missing(env) -> None:
    """``AI_AGENT_TOKEN: ${AI_AGENT_TOKEN:-}`` arrives as an empty string.

    Treating that as "set" would authenticate to ai-agent with an empty secret
    and surface the resulting 401 as "AI is down".
    """
    assert _settings(env, AI_AGENT_TOKEN="").AI_AGENT_TOKEN is None


def test_ai_enabled_without_a_token_refuses_to_build_an_app(env) -> None:
    from app.main import create_app

    settings = _settings(
        env,
        JWT_SECRET="0123456789abcdef0123456789abcdef",  # test-only, not a secret
        AI_ENABLED="true",
    )
    with pytest.raises(RuntimeError, match="AI_AGENT_TOKEN"):
        create_app(settings)


def test_ai_disabled_without_a_token_starts_fine(env) -> None:
    """Running with no AI at all is a supported configuration, not a failure."""
    from app.main import create_app

    settings = _settings(
        env,
        JWT_SECRET="0123456789abcdef0123456789abcdef",  # test-only, not a secret
        AI_ENABLED="false",
    )
    application = create_app(settings)
    assert application.state.settings.AI_ENABLED is False


# --- AI_AGENT_TOKEN strength (SEC-4) ---------------------------------------


def test_the_env_example_placeholder_is_rejected(env) -> None:
    """The exact value .env.example ships must not reach a running deployment.

    It is published in this repository, so a deployment still using it has a
    shared secret that everyone knows.
    """
    with pytest.raises(ValidationError, match="example value"):
        _settings(env, AI_ENABLED="true", AI_AGENT_TOKEN=PLACEHOLDER_AI_AGENT_TOKEN)


def test_the_placeholder_is_exactly_at_the_length_floor(env) -> None:
    """Why the placeholder needs naming at all — pinned so it stays true.

    It is exactly ``MIN_AI_AGENT_TOKEN_LENGTH`` characters, so the length rule
    alone would wave it straight through. If someone later shortens the
    placeholder in .env.example and deletes the literal check believing length
    covers it, this fails.
    """
    assert len(PLACEHOLDER_AI_AGENT_TOKEN) == MIN_AI_AGENT_TOKEN_LENGTH


@pytest.mark.parametrize(
    "spelling",
    [
        "CHANGE-ME-INTERNAL-SHARED-SECRET",
        "Change-Me-Internal-Shared-Secret",
    ],
)
def test_the_placeholder_is_rejected_whatever_its_case(env, spelling: str) -> None:
    """Shouting it does not make it a different secret."""
    with pytest.raises(ValidationError, match="example value"):
        _settings(env, AI_ENABLED="true", AI_AGENT_TOKEN=spelling)


@pytest.mark.parametrize("length", [1, 8, MIN_AI_AGENT_TOKEN_LENGTH - 1])
def test_a_short_ai_token_is_rejected(env, length: int) -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        _settings(env, AI_ENABLED="true", AI_AGENT_TOKEN="a" * length)


def test_a_token_at_the_floor_is_accepted(env) -> None:
    token = "b" * MIN_AI_AGENT_TOKEN_LENGTH
    assert _settings(env, AI_ENABLED="true", AI_AGENT_TOKEN=token).AI_AGENT_TOKEN == token


def test_the_generated_token_shape_is_accepted(env) -> None:
    """What `openssl rand -hex 32` actually produces: 64 hex characters."""
    token = "9f" * 32
    assert _settings(env, AI_ENABLED="true", AI_AGENT_TOKEN=token).AI_AGENT_TOKEN == token


@pytest.mark.parametrize(
    "token", [PLACEHOLDER_AI_AGENT_TOKEN, "short", "a" * 31]
)
def test_a_weak_token_is_inert_when_ai_is_disabled(env, token: str) -> None:
    """With AI off the value is never sent anywhere.

    Refusing to start over an inert string would break the "fully usable with
    no AI at all" guarantee: someone who copied .env.example and set
    AI_ENABLED=false has nothing to fix.
    """
    settings = _settings(env, AI_ENABLED="false", AI_AGENT_TOKEN=token)
    assert settings.AI_ENABLED is False
    assert settings.AI_AGENT_TOKEN == token


def test_a_weak_token_stops_the_app_from_starting(env) -> None:
    """The rule holds through create_app, not only through Settings()."""
    from app.main import create_app

    with pytest.raises(ValidationError, match="at least 32 characters"):
        settings = _settings(
            env,
            JWT_SECRET="0123456789abcdef0123456789abcdef",  # test-only
            AI_ENABLED="true",
            AI_AGENT_TOKEN="too-short",
        )
        create_app(settings)


def _shipped(name: str) -> str:
    """The value ``.env.example`` actually ships for ``name``.

    Read from disk rather than restated so that whatever devops puts there is
    what gets tested — the point of the checks below is that *no* shipped
    secret can start a backend, and a copy pasted into this file would only
    ever test the value it was copied from.
    """
    from pathlib import Path

    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    return next(
        line.split("=", 1)[1].strip()
        for line in env_example.read_text().splitlines()
        if line.startswith(f"{name}=")
    )


def _shipped_ai_agent_token() -> str:
    return _shipped("AI_AGENT_TOKEN")


def test_the_shipped_env_example_cannot_start_a_backend_with_ai_enabled(env) -> None:
    """End to end on the real file: ``.env.example`` is not a usable ``.env``.

    Asserted through ``create_app`` rather than ``Settings`` because *which*
    layer refuses depends on what the file ships, and both are correct:

    * an empty ``AI_AGENT_TOKEN=`` (what it ships today) is normalised to
      ``None`` by ``Settings`` — an empty secret is a missing secret — and
      refused by :func:`app.main.require_ai_agent_token`;
    * a placeholder or any too-short value is refused by ``Settings`` itself.

    Accepting either exception is deliberate. Pinning one of them would make
    this test fail the day devops legitimately changes the shipped value from
    empty to a placeholder or back, which is a documentation decision, not a
    security regression. What must never change is that the shipped file cannot
    start a backend with AI enabled — and both messages name ``AI_AGENT_TOKEN``,
    so whichever fires tells an operator the same thing.
    """
    from app.main import create_app

    shipped = _shipped_ai_agent_token()

    with pytest.raises((ValidationError, RuntimeError), match="AI_AGENT_TOKEN"):
        create_app(
            _settings(
                env,
                JWT_SECRET="0123456789abcdef0123456789abcdef",  # test-only
                AI_ENABLED="true",
                AI_AGENT_TOKEN=shipped,
            )
        )


def test_the_shipped_env_example_jwt_secret_cannot_start_a_backend(env) -> None:
    """The same from-disk property for the signing key — and it matters more.

    A published ``AI_AGENT_TOKEN`` lets whoever knows it reach a language model.
    A published ``JWT_SECRET`` lets them mint an access token for **any
    account**. Both must be impossible to inherit by copying the file.

    Either layer may refuse, for the same reason as the AI token: devops ships
    an empty value today (``Settings`` normalises it to ``None`` and
    ``create_app`` refuses), and a placeholder would be refused by ``Settings``.
    """
    from app.main import create_app

    shipped = _shipped("JWT_SECRET")

    with pytest.raises((ValidationError, RuntimeError), match="JWT_SECRET"):
        create_app(_settings(env, JWT_SECRET=shipped, AI_ENABLED="false"))


@pytest.mark.parametrize("name", ["JWT_SECRET", "AI_AGENT_TOKEN"])
def test_the_env_example_ships_no_usable_secret(env, name: str) -> None:
    """Whatever it ships must be inert, not merely rejected somewhere.

    A committed value that *passed* validation would be a real secret sitting
    in the repository. Empty (today) and a documented ``change-me`` placeholder
    are the only shapes that are safe to publish — this fails if someone ever
    commits a generated one.
    """
    shipped = _shipped(name)
    assert shipped == "" or is_placeholder_secret(
        shipped, frozenset({PLACEHOLDER_AI_AGENT_TOKEN, *PLACEHOLDER_JWT_SECRETS})
    ), f".env.example ships what looks like a real {name}: {shipped!r}"


def test_an_empty_token_from_the_env_file_stops_the_app(env) -> None:
    """The shape compose and ``.env.example`` produce today.

    ``AI_AGENT_TOKEN: ${AI_AGENT_TOKEN:-}`` and a bare ``AI_AGENT_TOKEN=`` both
    arrive as an empty string. ``Settings`` treats that as missing, so the
    refusal comes from ``create_app`` — with the message that names the one
    variable to set and the one-line way out.
    """
    from app.main import create_app

    settings = _settings(
        env,
        JWT_SECRET="0123456789abcdef0123456789abcdef",  # test-only
        AI_ENABLED="true",
        AI_AGENT_TOKEN="",
    )
    assert settings.AI_AGENT_TOKEN is None

    with pytest.raises(RuntimeError, match="AI_AGENT_TOKEN"):
        create_app(settings)


def test_an_empty_token_is_harmless_when_ai_is_disabled(env) -> None:
    """`docker compose up -d` with no AI profile must still start."""
    from app.main import create_app

    application = create_app(
        _settings(
            env,
            JWT_SECRET="0123456789abcdef0123456789abcdef",  # test-only
            AI_ENABLED="false",
            AI_AGENT_TOKEN="",
        )
    )
    assert application.state.settings.AI_ENABLED is False


# --- REALTIME_BACKEND (IT3-4, decision D-IT4-4) ----------------------------


def test_realtime_backend_defaults_to_memory(env) -> None:
    assert _settings(env).REALTIME_BACKEND == REALTIME_BACKEND_MEMORY


@pytest.mark.parametrize("value", ["redis", "postgres", "Redis", "kafka", ""])
def test_any_other_realtime_backend_fails_at_startup(env, value: str) -> None:
    """Reserved, not implemented (D-IT4-4). ``redis`` is the one an operator is
    most likely to try, so it must fail loudly rather than silently do nothing."""
    with pytest.raises(ValidationError) as excinfo:
        _settings(env, REALTIME_BACKEND=value)

    message = str(excinfo.value)
    assert "REALTIME_BACKEND" in message
    assert REALTIME_BACKEND_MEMORY in message
    # An operator has to learn *what* they lose, not only that a parser said no.
    assert "one backend replica" in message


@pytest.mark.parametrize("spelling", ["memory", "MEMORY", "  Memory  "])
def test_the_supported_value_is_accepted_however_it_is_spelled(
    env, spelling: str
) -> None:
    assert _settings(env, REALTIME_BACKEND=spelling).REALTIME_BACKEND == "memory"


def test_no_redis_dependency_was_added() -> None:
    """Criterion 23, asserted rather than promised.

    D-IT4-4 reserves the *name* and nothing else: a Redis client appearing in
    the dependency list would mean the decision quietly changed.
    """
    from pathlib import Path

    manifest = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert "redis" not in manifest.casefold()


# --- application logging ---------------------------------------------------
#
# Found by QA in iteration 4: nothing configured Python logging, so every
# ``logger.info`` in ``app.*`` was dropped in the container — including the
# single-replica notice criterion 21 is about. The tests below therefore never
# use ``caplog.at_level``: forcing the level is exactly what hid the bug.


@contextmanager
def isolated_logging():
    """Run with the ``app`` and root loggers empty, and put them back after.

    ``configure_logging`` only attaches a handler when nothing is listening
    yet, and under pytest something always is (the logging plugin installs a
    root handler for every test). Without this the "attaches a handler" branch
    could not be reached from a test at all — and a handler left behind on the
    ``app`` logger would print every later test's log lines twice.
    """
    app_logger = logging.getLogger("app")
    root = logging.getLogger()
    saved = (app_logger.handlers[:], root.handlers[:], app_logger.level)
    app_logger.handlers.clear()
    root.handlers.clear()
    try:
        yield app_logger
    finally:
        app_logger.handlers[:], root.handlers[:], app_logger.level = saved


@contextmanager
def recording(logger_name: str = "app"):
    """Attach a handler that keeps every record it is given, then remove it."""
    records: list[logging.LogRecord] = []

    class Recorder(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger(logger_name)
    handler = Recorder()
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)


def test_log_level_defaults_to_info(env) -> None:
    assert _settings(env).LOG_LEVEL == "INFO"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("debug", "DEBUG"), ("WARNING", "WARNING"), ("  Error  ", "ERROR")],
)
def test_log_level_is_normalized(env, value: str, expected: str) -> None:
    assert _settings(env, LOG_LEVEL=value).LOG_LEVEL == expected


@pytest.mark.parametrize("value", ["INF", "verbose", "12", ""])
def test_an_unknown_log_level_is_refused(env, value: str) -> None:
    """``Logger.setLevel`` would raise from inside startup instead, with a
    message about a logging internal rather than about the variable."""
    with pytest.raises(ValidationError) as excinfo:
        _settings(env, LOG_LEVEL=value)

    message = str(excinfo.value)
    assert "LOG_LEVEL" in message
    assert "INFO" in message  # the valid values are listed


def test_create_app_leaves_the_app_logger_emitting_info(test_settings) -> None:
    """The regression itself: an unconfigured ``app`` logger drops INFO."""
    from app.main import create_app

    with isolated_logging() as app_logger:
        create_app(test_settings)
        assert app_logger.getEffectiveLevel() <= logging.INFO


def test_an_info_record_reaches_a_handler_without_forcing_the_level(
    test_settings,
) -> None:
    from app.main import create_app

    with isolated_logging():
        create_app(test_settings)
        with recording() as records:
            logging.getLogger("app.main").info("marker %s", 1)

    assert [record.getMessage() for record in records] == ["marker 1"]


def test_the_configured_handler_writes_to_stderr(test_settings, capsys) -> None:
    """stderr, not stdout: log output must not be mistaken for program output,
    and Docker collects both anyway."""
    from app.main import configure_logging

    with isolated_logging():
        configure_logging(test_settings)
        logging.getLogger("app.main").info("hello from the app logger")

    captured = capsys.readouterr()
    assert "hello from the app logger" in captured.err
    assert captured.out == ""


def test_configuring_twice_does_not_duplicate_the_handler(test_settings) -> None:
    """``create_app`` runs dozens of times in this suite, and once per import
    in production; a second handler on the same stream prints every line twice."""
    from app.main import configure_logging

    with isolated_logging() as app_logger:
        configure_logging(test_settings)
        configure_logging(test_settings)
        configure_logging(test_settings)

        assert len(app_logger.handlers) == 1
        assert isinstance(app_logger.handlers[0], logging.StreamHandler)


def test_an_existing_handler_is_never_clobbered(test_settings) -> None:
    """uvicorn (or a production log-config file) may already have configured
    logging; adding ours on top would double every line."""
    from app.main import configure_logging

    with isolated_logging() as app_logger:
        theirs = logging.NullHandler()
        logging.getLogger().addHandler(theirs)

        configure_logging(test_settings)

        assert app_logger.handlers == []
        assert logging.getLogger().handlers == [theirs]
        # The level is still ours to set — that is the half that was missing.
        assert app_logger.getEffectiveLevel() == logging.INFO


def test_the_root_logger_level_is_left_alone(test_settings) -> None:
    """Turning the root level down would turn up SQLAlchemy, asyncpg and httpx
    — which at DEBUG print statements and headers, including Authorization."""
    from app.main import configure_logging

    with isolated_logging():
        root = logging.getLogger()
        before = root.level

        configure_logging(_settings_with_log_level(test_settings, "DEBUG"))

        assert root.level == before
        assert logging.getLogger("sqlalchemy.engine").getEffectiveLevel() > logging.DEBUG


def _settings_with_log_level(settings: Settings, level: str) -> Settings:
    return settings.model_copy(update={"LOG_LEVEL": level})
