"""The placeholder secret guard (iteration 4, IT3-3).

KEEP IN SYNC WITH ``backend/tests/test_config.py``. ``PLACEHOLDER_CORPUS`` and
``REAL_SECRET_CORPUS`` below are duplicated **verbatim** in that file, as are
the three tables in ``app/config.py``. The services are separate ``uv``
projects with no shared package (decision D-IT4-3), so this pair of identical
corpora is what keeps the duplication honest: if somebody widens one service's
table and not the other's, ``test_every_prefix_is_represented_in_the_corpus``
or one of the corpus rows fails here rather than a deployment failing later.

The corpora are shared, but the *variable* is not: this file asserts
``AI_AGENT_TOKEN``, the backend copy asserts ``JWT_SECRET`` and its own
``AI_AGENT_TOKEN``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import (
    MIN_TOKEN_LENGTH,
    PLACEHOLDER_EXACT,
    PLACEHOLDER_PREFIXES,
    PLACEHOLDER_SUBSTRINGS,
    Settings,
    is_placeholder_secret,
)
from tests.conftest import TEST_TOKEN

# --------------------------------------------------------------------------- #
# The shared corpora (identical in backend/tests/test_config.py)
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# The predicate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", PLACEHOLDER_CORPUS)
def test_the_predicate_rejects_every_placeholder(value: str) -> None:
    assert is_placeholder_secret(value) is True


@pytest.mark.parametrize("value", REAL_SECRET_CORPUS)
def test_the_predicate_accepts_every_real_shaped_secret(value: str) -> None:
    assert is_placeholder_secret(value) is False


def test_the_known_argument_widens_without_touching_the_table() -> None:
    """A call site can add its own literal; the shared table is unchanged."""
    own = "a-literal-only-this-call-site-knows"

    assert is_placeholder_secret(own) is False
    assert is_placeholder_secret(own, frozenset({own})) is True
    assert is_placeholder_secret(own.upper(), frozenset({own})) is True
    assert own not in PLACEHOLDER_EXACT


# --------------------------------------------------------------------------- #
# Corpus self-checks — these are what make the duplication safe
# --------------------------------------------------------------------------- #


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


def test_the_corpus_is_long_enough_to_be_meaningful() -> None:
    assert len(PLACEHOLDER_CORPUS) >= 12
    assert len(set(PLACEHOLDER_CORPUS)) == len(PLACEHOLDER_CORPUS)


def test_placeholders_are_rejected_on_their_own_merit_not_on_length() -> None:
    """Every corpus entry clears the length floor, so the guard is what fires."""
    for value in PLACEHOLDER_CORPUS:
        assert len(value.strip()) >= MIN_TOKEN_LENGTH, value


def test_real_secrets_clear_the_length_floor_too() -> None:
    for value in REAL_SECRET_CORPUS:
        assert len(value) >= MIN_TOKEN_LENGTH, value


# --------------------------------------------------------------------------- #
# End to end through Settings
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", PLACEHOLDER_CORPUS)
def test_settings_reject_every_placeholder_token(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AI_AGENT_TOKEN", value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]

    message = str(excinfo.value)
    assert "AI_AGENT_TOKEN" in message
    assert "openssl rand -hex 32" in message


@pytest.mark.parametrize("value", REAL_SECRET_CORPUS)
def test_settings_accept_every_real_shaped_secret(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("AI_AGENT_TOKEN", value)

    assert Settings(_env_file=None).AI_AGENT_TOKEN == value  # type: ignore[call-arg]


@pytest.mark.parametrize("value", PLACEHOLDER_CORPUS)
def test_the_rejected_value_is_never_echoed(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A near-miss rejection would otherwise print a real secret into logs.

    pydantic does not echo the input for a ``ValueError`` raised in a
    validator, but that is a property worth pinning: the message is written for
    an operator reading a container log, and container logs get shared.
    """
    monkeypatch.setenv("AI_AGENT_TOKEN", value)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)  # type: ignore[call-arg]

    assert value.strip() not in str(excinfo.value)


def test_the_harness_token_survives_the_guard() -> None:
    """If this fails, the whole suite is about to fail for the same reason."""
    assert is_placeholder_secret(TEST_TOKEN) is False
