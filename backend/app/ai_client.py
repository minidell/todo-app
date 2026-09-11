"""HTTP client for the private ``ai-agent`` service (master §8.1, D-AI2).

The frontend never talks to ai-agent: the backend proxies, because it is the
only component allowed to read a user's todos to build a prompt, and because
one auth surface (the JWT) is easier to reason about than two. This module is
that proxy's transport half — it knows how to *call* ai-agent and how to turn
every way that call can fail into the error contract of master §5. It knows
nothing about todos; the router assembles the context.

Nothing an ai-agent response says ever reaches the user verbatim. A caller sees
``ai_unavailable``, ``ai_timeout`` or a validated payload, and the shared secret
appears in no response, no exception message and no log line.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

import httpx

from app.config import Settings
from app.errors import AiTimeout, AiUnavailable

logger = logging.getLogger(__name__)

#: ``GET /health`` gets its own short budget: ``/api/ai/status`` is called on
#: every page load to decide whether to render the AI section, so a hung
#: ai-agent must degrade the UI in three seconds, not in fifty.
HEALTH_TIMEOUT_SECONDS = 3.0

#: How long a health answer is reused. ``/api/ai/status`` is authenticated but
#: deliberately not rate-limited (a 429 on "is AI up?" would black out the AI
#: section for five minutes), so without this one account could aim a request
#: per page load straight at ai-agent — and, when ai-agent is *down*, spend
#: three seconds of connect timeout on each. Ten seconds is short enough that
#: a recovering model shows up on the next poll and long enough that a burst
#: of tabs costs one upstream call.
HEALTH_CACHE_SECONDS = 10.0

#: The header carrying the shared secret. Named once so no call site can spell
#: it differently, and so tests can assert on the constant.
INTERNAL_TOKEN_HEADER = "X-Internal-Token"

#: The authenticated no-op ai-agent serves so we can tell "not reachable" apart
#: from "reachable but we disagree about the shared secret" (master §8.2). It
#: makes no Ollama call, so probing it costs a round trip and nothing else.
PING_PATH = "/ai/ping"

#: What a single probe of :data:`PING_PATH` concluded.
PingResult = Literal["ok", "unauthorized", "error"]


@dataclass(frozen=True, slots=True)
class AiHealth:
    """What ``GET /api/ai/status`` needs, and nothing else."""

    available: bool
    model: str | None = None
    #: ``None`` when :attr:`available`; otherwise one of the closed enum of
    #: master §5.1 — ``unreachable``, ``auth_failed`` or ``model_unavailable``.
    #: (``disabled`` never comes from here: with AI off nothing is probed.)
    reason: str | None = None


class AiAgentClient:
    """A pooled ``httpx.AsyncClient`` aimed at ai-agent, with error mapping.

    One instance per application: an HTTP client is a connection pool, and
    building one per request would open a fresh TCP connection for every AI
    call and never reuse the keep-alive.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        #: Monotonic by default: a wall clock stepped backwards by NTP would
        #: freeze the cached answer instead of expiring it.
        self._clock = clock
        self._cached_health: AiHealth | None = None
        self._health_cached_at = 0.0
        headers = {"Accept": "application/json"}
        if settings.AI_AGENT_TOKEN:
            headers[INTERNAL_TOKEN_HEADER] = settings.AI_AGENT_TOKEN
        self._client = httpx.AsyncClient(
            base_url=str(settings.AI_AGENT_URL).rstrip("/"),
            headers=headers,
            # The read budget is the whole point of the ladder in master §8.3:
            # ai-agent waits 45 s on Ollama, we wait 50 s on ai-agent, and the
            # browser aborts at 60 s — so each layer times out before the one
            # above it gives up, and every failure has a named owner.
            timeout=httpx.Timeout(
                connect=5.0,
                read=settings.AI_TIMEOUT_SECONDS,
                write=10.0,
                pool=5.0,
            ),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> AiHealth:
        """Ask ai-agent whether the model is usable. **Never raises.**

        ``/api/ai/status`` is the one AI endpoint that must always answer 200
        (master §6.6), so every failure here — connection refused, timeout,
        garbage body, a 500 — is simply "not available". The UI hides or
        disables its AI controls and the rest of the app is untouched.

        The answer is reused for :data:`HEALTH_CACHE_SECONDS`. Failures are
        cached exactly like successes: an ai-agent that is down is the case
        where an uncached probe costs the most (a connect timeout per caller),
        and it is the case a caller is most likely to retry in a loop.

        Two probes are needed, not one (IT3-2): ``/health`` is unauthenticated
        by design, so it answers happily while ai-agent is rejecting our shared
        secret — which used to report ``available: true`` and hand the user AI
        buttons that were certain to fail. They run **concurrently**, so the
        worst case stays ≈:data:`HEALTH_TIMEOUT_SECONDS` rather than twice it,
        and they share this one cache entry: a burst of tabs still costs one
        pair of upstream calls.
        """
        cached = self._cached_health
        if cached is not None and (
            self._clock() - self._health_cached_at < HEALTH_CACHE_SECONDS
        ):
            return cached

        # Neither probe raises, so ``return_exceptions`` would only hide a bug.
        health, ping = await asyncio.gather(self._probe_health(), self._probe_ping())
        health = self._combine(health, ping)
        self._cached_health = health
        self._health_cached_at = self._clock()
        return health

    @staticmethod
    def _combine(health: AiHealth, ping: PingResult) -> AiHealth:
        """Fold the two probes into one answer (master §5.1, precedence order).

        Reachability is decided first: if ``/health`` did not answer, *why* the
        authenticated endpoint failed is noise. ``auth_failed`` then outranks
        ``model_unavailable`` — a token mismatch makes every AI call fail
        whatever Ollama is doing, and it is the one an operator can fix.
        """
        if health.reason == "unreachable":
            return health
        if ping == "unauthorized":
            return AiHealth(available=False, model=health.model, reason="auth_failed")
        if ping == "error":
            return AiHealth(available=False, model=health.model, reason="unreachable")
        if not health.available:
            return AiHealth(
                available=False, model=health.model, reason="model_unavailable"
            )
        return AiHealth(available=True, model=health.model, reason=None)

    async def _probe_health(self) -> AiHealth:
        try:
            response = await self._client.get(
                "/health", timeout=HEALTH_TIMEOUT_SECONDS
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001 - "anything at all" is the point
            logger.info("ai-agent health check failed: %s", type(exc).__name__)
            return AiHealth(available=False, reason="unreachable")

        if not isinstance(body, dict):
            logger.warning("ai-agent /health returned a non-object body")
            return AiHealth(available=False, reason="unreachable")

        model = body.get("model")
        # "Reachable" is not "usable": ai-agent answers /health happily while
        # Ollama is down or the model was never pulled, and offering the user
        # AI buttons that are certain to fail is worse than hiding them.
        available = body.get("ollama") == "ok" and bool(body.get("model_present"))
        return AiHealth(
            available=available,
            model=model if isinstance(model, str) else None,
            reason=None if available else "model_unavailable",
        )

    async def _probe_ping(self) -> PingResult:
        """Ask the *authenticated* no-op endpoint whether we are who we claim.

        **Never raises**: it feeds ``/api/ai/status``, which must always answer
        200. A 401 is the one interesting outcome — it means the two services
        disagree about ``AI_AGENT_TOKEN``, which no unauthenticated probe can
        ever reveal. Everything else, including every exception, is "error";
        the caller turns that into ``unreachable``.
        """
        try:
            response = await self._client.get(
                PING_PATH, timeout=HEALTH_TIMEOUT_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - "anything at all" is the point
            logger.info("ai-agent ping failed: %s", type(exc).__name__)
            return "error"

        if response.status_code == 401:
            # Same wording as _log_error_status: one cause, one fix, and the
            # token itself is never logged.
            logger.warning(
                "ai-agent rejected our internal token on %s: the backend and "
                "ai-agent disagree about AI_AGENT_TOKEN. Set the same value on "
                "both services.",
                PING_PATH,
            )
            return "unauthorized"
        if response.is_success:
            return "ok"
        logger.warning(
            "ai-agent %s failed with status %s", PING_PATH, response.status_code
        )
        return "error"

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Call an ai-agent endpoint, or raise an :class:`app.errors.AppError`.

        The mapping is master §8.3's degradation ladder, and it is deliberately
        lossy: the caller learns *that* AI is unavailable, never why. An
        ai-agent error body can contain a prompt fragment or a model name, and
        the user has no use for either.
        """
        try:
            response = await self._client.post(path, json=payload)
        except httpx.TimeoutException as exc:
            # A distinct status (504) because it means something different to
            # the user: "too slow, try again", not "AI is down".
            logger.warning("ai-agent %s timed out after %ss", path, self._read_timeout)
            raise AiTimeout() from exc
        except httpx.RequestError as exc:
            # ConnectError, read errors, DNS, a broken pool — all "it is down".
            logger.warning(
                "ai-agent %s is unreachable: %s", path, type(exc).__name__
            )
            raise AiUnavailable() from exc

        if response.status_code >= 400:
            self._log_error_status(path, response.status_code)
            raise AiUnavailable()

        try:
            body = response.json()
        except ValueError as exc:
            logger.warning("ai-agent %s returned a body that is not JSON", path)
            raise AiUnavailable() from exc

        if not isinstance(body, dict):
            logger.warning("ai-agent %s returned %s, expected an object", path, type(body).__name__)
            raise AiUnavailable()
        return body

    @property
    def _read_timeout(self) -> float:
        return float(self._settings.AI_TIMEOUT_SECONDS)

    def _log_error_status(self, path: str, status_code: int) -> None:
        """Say enough for an operator to fix it, without quoting the response.

        401 gets its own line because it has exactly one cause and one fix: the
        two halves of the deployment disagree about ``AI_AGENT_TOKEN``. The
        token itself is never logged — a secret in a log file is still a leaked
        secret.
        """
        if status_code == 401:
            logger.warning(
                "ai-agent rejected our internal token on %s: the backend and "
                "ai-agent disagree about AI_AGENT_TOKEN. Set the same value on "
                "both services.",
                path,
            )
        elif status_code == 422:
            # We built the request, so this is our bug, not the user's, and it
            # must be loud enough to notice in a log.
            logger.error(
                "ai-agent rejected the request body on %s (422): the backend "
                "sent something outside the contract in master §8.2.",
                path,
            )
        else:
            logger.warning("ai-agent %s failed with status %s", path, status_code)
