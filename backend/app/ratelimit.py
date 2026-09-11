"""In-process sliding-window rate limiting (slice-2 spec B4).

Scope and honesty about it: the counters live in this process only. With the
single backend replica iteration 2 ships (master §7.3, R5) that is exact; behind
several replicas it would be per-replica. It is a *speed bump* against online
password guessing, not an authorization control — the security properties that
matter (argon2 cost, identical failure responses) do not depend on it.

The client key comes from ``app.deps.get_client_ip``: the socket address by
default, or the last ``X-Forwarded-For`` hop when ``TRUST_PROXY_HEADERS`` is on
and a proxy we control is the only way in. Trusting that header on a directly
reachable app would let an attacker mint a fresh counter per request.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable

#: Beyond this many tracked keys the oldest are evicted, so a flood of distinct
#: emails/IPs cannot grow the process memory without bound.
DEFAULT_MAX_KEYS = 10_000


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    #: Seconds to wait before the next attempt can succeed (0 when allowed).
    retry_after: int


class RateLimiter:
    """A fixed-limit sliding window over the last ``window_seconds``.

    ``clock`` is injectable so tests can advance time without sleeping; it
    defaults to a monotonic clock, which cannot be moved backwards by an NTP
    step (a wall clock could, which would silently widen the window).
    """

    def __init__(
        self,
        limit: int,
        window_seconds: int,
        *,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window_seconds < 1:
            raise ValueError("window_seconds must be >= 1")
        self._limit = limit
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def window_seconds(self) -> int:
        return self._window

    def _prune(self, stamps: deque[float], now: float) -> None:
        cutoff = now - self._window
        while stamps and stamps[0] <= cutoff:
            stamps.popleft()

    def hit(self, key: str) -> RateLimitResult:
        """Record an attempt for ``key`` and report whether it is allowed."""
        now = self._clock()
        stamps = self._hits.get(key)
        if stamps is None:
            stamps = deque()
            self._hits[key] = stamps
        self._hits.move_to_end(key)
        self._prune(stamps, now)

        if len(stamps) >= self._limit:
            # A rejected attempt is not recorded: otherwise a client that keeps
            # hammering would extend its own lockout indefinitely, which is a
            # denial of service against the legitimate owner of the account.
            retry_after = max(1, math.ceil(stamps[0] + self._window - now))
            return RateLimitResult(allowed=False, retry_after=retry_after)

        stamps.append(now)
        self._evict_if_needed()
        return RateLimitResult(allowed=True, retry_after=0)

    def _evict_if_needed(self) -> None:
        while len(self._hits) > self._max_keys:
            self._hits.popitem(last=False)

    def reset(self, key: str | None = None) -> None:
        """Forget one key (or everything). Used by tests and by nothing else."""
        if key is None:
            self._hits.clear()
        else:
            self._hits.pop(key, None)
