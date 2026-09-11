"""Settings for the ai-agent service (master spec §9.1, slice-4 spec B1).

Every value comes from the environment. ``AI_AGENT_TOKEN`` deliberately has **no
default**: the service must fail to start rather than run with a guessable shared
secret (slice-4 B5, acceptance criterion 11).

It must also be at least :data:`MIN_TOKEN_LENGTH` characters *and* not be the
placeholder shipped in ``.env.example`` — note that the placeholder is exactly
32 characters long, so a length floor alone would happily accept it. Both checks
run at startup, so a misconfigured deployment fails loudly instead of running on
a secret that is published in the repository.
"""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: ``openssl rand -hex 32`` yields 64 characters; 32 is the floor we enforce.
MIN_TOKEN_LENGTH = 32

# --------------------------------------------------------------------------- #
# Placeholder secret table (iteration 4, IT3-3)
#
# KEEP IN SYNC WITH ``backend/app/config.py`` — the three tables below and
# ``is_placeholder_secret`` are duplicated **verbatim** in that file. The two
# services are separate ``uv`` projects with no shared package (decision
# D-IT4-3), so the duplication is deliberate; it is kept honest by an identical
# test corpus in both suites (``ai-agent/tests/test_config.py`` and
# ``backend/tests/test_config.py``), so drift fails a test rather than a
# deployment. Edit both files or neither.
#
# All three are matched on ``value.strip().casefold()``: retyping a placeholder
# in capitals, or leaving a stray space when copying ``.env.example``, does not
# make it a secret.
# --------------------------------------------------------------------------- #

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


#: Backwards-compatible alias: the shipped literals this service used to name on
#: its own. They are a subset of :data:`PLACEHOLDER_EXACT` now.
PLACEHOLDER_TOKENS = PLACEHOLDER_EXACT


class Settings(BaseSettings):
    """Environment-driven configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        #: Every field here is either a secret or sits next to one, and a
        #: startup ValidationError is printed straight into the container log.
        #: By default pydantic appends ``input_value='…'`` to each error, so a
        #: rejected AI_AGENT_TOKEN would be logged in full. That was harmless
        #: while the placeholder guard only matched values that were already
        #: public; since iteration 4 (IT3-3) it also fires on *near* misses —
        #: an operator's real secret that merely contains "sample" — so the
        #: echo has to go (acceptance criterion 19). Messages are unaffected.
        hide_input_in_errors=True,
    )

    OLLAMA_BASE_URL: str = "http://ollama:11434"
    OLLAMA_MODEL: str = "qwen2.5:3b"
    OLLAMA_TIMEOUT_SECONDS: float = 45.0
    AI_AGENT_TOKEN: str = Field(min_length=MIN_TOKEN_LENGTH)
    LOG_LEVEL: str = "info"
    #: Fails closed: an unset APP_ENV must not expose the API surface. Local
    #: development and compose set APP_ENV=dev explicitly to get /docs.
    APP_ENV: str = "prod"

    @field_validator("AI_AGENT_TOKEN")
    @classmethod
    def _reject_placeholder_token(cls, value: str) -> str:
        """Fail fast on documentation copy used as a shared secret.

        Whitespace-trimmed and case-insensitive, like the backend's equivalent
        check: ``CHANGE-ME-INTERNAL-SHARED-SECRET`` is just as public as the
        lowercase original. Since iteration 4 the predicate also catches
        placeholders the repository never shipped — ``your-secret-here``,
        ``replace_me_now``, ``dummy-token-…`` — because the failure mode is an
        operator who edited the example file without generating anything.

        The message never echoes the rejected value: it would land in logs and
        in CI output, and on a *near*-miss that value is a real secret.
        """
        if is_placeholder_secret(value):
            raise ValueError(
                "AI_AGENT_TOKEN looks like a placeholder, not a real secret, so "
                "it is public knowledge. Generate a real one with: "
                "openssl rand -hex 32"
            )
        return value

    @property
    def base_url(self) -> str:
        """Ollama base URL without a trailing slash."""
        return self.OLLAMA_BASE_URL.rstrip("/")

    @property
    def docs_enabled(self) -> bool:
        """OpenAPI docs are served only when APP_ENV is exactly ``dev``.

        Anything else — including an unset, empty or misspelled value — hides
        ``/docs``, ``/redoc`` and ``/openapi.json`` (master §9.1 / C5).
        """
        return self.APP_ENV == "dev"


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance; also usable as a FastAPI dependency."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
