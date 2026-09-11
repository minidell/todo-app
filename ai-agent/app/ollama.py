"""Thin async client for the Ollama HTTP API (slice-4 spec B2).

Only two calls are needed:

* ``chat_json`` — ``POST /api/chat`` with ``stream: false`` and the expected
  JSON Schema in ``format`` (Ollama structured outputs). If the server rejects a
  schema ``format`` the call is retried once with ``"format": "json"``.
* ``list_models`` — ``GET /api/tags``, used by ``/health`` to report whether the
  configured model has actually been pulled.

Every failure mode is normalized into one of the three exceptions below so the
routers never have to know about httpx.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: Indirection so tests can drive a fake clock.
_now = time.monotonic

#: A retry is pointless with less budget than this left; fail fast instead of
#: starting a call that is certain to be cut off mid-flight.
MIN_RETRY_BUDGET_SECONDS = 1.0

#: Connecting to Ollama is local and fast; only *generating* is slow. Keep this
#: ceiling even when a large budget remains (master §8.3).
CONNECT_TIMEOUT_SECONDS = 5.0

#: ```json ... ``` or ``` ... ``` wrappers that small models like to add even
#: when told to reply with JSON only.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


class OllamaError(Exception):
    """Base class for every Ollama failure."""


class OllamaUnavailable(OllamaError):
    """Ollama is unreachable, returned a 5xx, or does not have the model."""


class OllamaTimeout(OllamaError):
    """Ollama did not answer within the configured read timeout."""


class OllamaInvalidResponse(OllamaError):
    """Ollama answered, but the message content was not JSON."""


class Deadline:
    """A wall-clock budget shared by every upstream call of one request.

    ``OLLAMA_TIMEOUT_SECONDS`` is a per-call httpx read timeout, so a request
    that fell back on the schema ``format`` *and* then took a repair retry could
    spend three times that budget — blowing past the backend's 50 s and the
    browser's 60 s ceilings (master §8.3). One deadline per endpoint call keeps
    the total bounded.
    """

    def __init__(self, budget_seconds: float | None) -> None:
        self._budget = budget_seconds
        self._started = _now()

    @property
    def remaining(self) -> float | None:
        """Seconds left, or ``None`` when the deadline is unbounded."""
        if self._budget is None:
            return None
        return self._budget - (_now() - self._started)

    def check(self, *, minimum: float = 0.0) -> None:
        """Raise ``OllamaTimeout`` when too little budget is left to continue."""
        remaining = self.remaining
        if remaining is not None and remaining <= minimum:
            raise OllamaTimeout(
                f"Ollama budget of {self._budget}s exhausted "
                f"({remaining:.1f}s remaining)"
            )


@dataclass(frozen=True)
class ChatResult:
    """A successful ``chat_json`` call: parsed object plus the raw content.

    ``raw`` is kept so a repair retry can show the model its own previous reply.
    """

    data: dict[str, Any]
    raw: str


Message = dict[str, str]


class OllamaClient:
    """Async Ollama client bound to one model and one httpx client."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        model: str,
        *,
        budget_seconds: float | None = None,
    ) -> None:
        self._client = client
        self._model = model
        self._budget_seconds = budget_seconds

    @property
    def model(self) -> str:
        return self._model

    def new_deadline(self) -> Deadline:
        """A fresh budget covering every upstream call of one request."""
        return Deadline(self._budget_seconds)

    async def chat_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = 512,
        follow_up: list[Message] | None = None,
        deadline: Deadline | None = None,
    ) -> ChatResult:
        """Ask the model for one JSON object matching ``schema``.

        ``follow_up`` appends extra messages after the initial user turn; the
        repair retry in ``routers/ai.py`` uses it to feed back validator errors.
        ``deadline`` bounds this call *and* is shared across retries.
        """
        if deadline is not None:
            deadline.check()
        messages: list[Message] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        if follow_up:
            messages.extend(follow_up)

        payload: dict[str, Any] = {
            "model": self._model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0.2, "num_predict": max_tokens},
            "messages": messages,
        }

        response = await self._post("/api/chat", payload, deadline)
        if self._rejects_schema_format(response):
            logger.info(
                "Ollama rejected a JSON-schema 'format'; retrying with format=json"
            )
            if deadline is not None:
                deadline.check(minimum=MIN_RETRY_BUDGET_SECONDS)
            payload["format"] = "json"
            response = await self._post("/api/chat", payload, deadline)

        self._raise_for_status(response)

        try:
            body = response.json()
        except ValueError as exc:  # pragma: no cover - Ollama always sends JSON
            raise OllamaInvalidResponse("Ollama returned a non-JSON envelope") from exc

        content = (body.get("message") or {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise OllamaInvalidResponse("Ollama returned an empty message")

        return ChatResult(data=_parse_content(content), raw=content)

    async def list_models(self, *, timeout: float | None = 3.0) -> list[str]:
        """Names of the models currently present in Ollama (``GET /api/tags``)."""
        try:
            response = await self._client.get("/api/tags", timeout=timeout)
        except httpx.TimeoutException as exc:
            raise OllamaTimeout("Ollama timed out while listing models") from exc
        except httpx.RequestError as exc:
            raise OllamaUnavailable(f"Cannot reach Ollama: {exc.__class__.__name__}") from exc

        self._raise_for_status(response)
        try:
            body = response.json()
        except ValueError as exc:  # pragma: no cover
            raise OllamaUnavailable("Ollama returned a non-JSON model list") from exc

        models = body.get("models") or []
        names: list[str] = []
        for entry in models:
            name = entry.get("name") or entry.get("model") if isinstance(entry, dict) else None
            if isinstance(name, str):
                names.append(name)
        return names

    async def _post(
        self, path: str, payload: dict[str, Any], deadline: Deadline | None = None
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {}
        if deadline is not None and deadline.remaining is not None:
            # Never let one call outlive the request-wide budget. A scalar here
            # would widen the connect timeout to the whole remaining budget, so
            # the 5 s connect ceiling is kept explicitly.
            remaining = deadline.remaining
            kwargs["timeout"] = httpx.Timeout(
                connect=min(CONNECT_TIMEOUT_SECONDS, remaining),
                read=remaining,
                write=remaining,
                pool=remaining,
            )
        try:
            return await self._client.post(path, json=payload, **kwargs)
        except httpx.TimeoutException as exc:
            raise OllamaTimeout("Ollama timed out") from exc
        except httpx.RequestError as exc:
            raise OllamaUnavailable(f"Cannot reach Ollama: {exc.__class__.__name__}") from exc

    @staticmethod
    def _rejects_schema_format(response: httpx.Response) -> bool:
        """True when a 4xx complains about the ``format`` field specifically."""
        if not 400 <= response.status_code < 500:
            return False
        return "format" in response.text.lower()

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = response.text.strip()
        if response.status_code == 404:
            raise OllamaUnavailable(
                f"Ollama does not have the model {self._model!r} "
                f"(pull it with: ollama pull {self._model})"
            )
        raise OllamaUnavailable(
            f"Ollama returned HTTP {response.status_code}: {detail[:200]}"
        )


def _parse_content(content: str) -> dict[str, Any]:
    """Parse the model's message content as a JSON object.

    Falls back to stripping a markdown code fence once before giving up
    (slice-4 spec B2).
    """
    for candidate in _json_candidates(content):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
        raise OllamaInvalidResponse("Model returned JSON that is not an object")
    raise OllamaInvalidResponse("Model did not return JSON")


def _json_candidates(content: str) -> list[str]:
    candidates = [content.strip()]
    fenced = _FENCE_RE.match(content)
    if fenced:
        candidates.append(fenced.group("body").strip())
    return [candidate for candidate in candidates if candidate]
