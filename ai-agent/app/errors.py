"""Error contract for the ai-agent service.

Mirrors the backend's error contract v2 (master §5, decision D-E1): a
human-readable ``detail`` string plus a sibling machine-readable ``code``.
Pydantic request-validation errors keep FastAPI's default 422 body and carry no
``code``.

Codes emitted by this service:

===================== ====== ==============================================
``code``              status meaning
===================== ====== ==============================================
``unauthorized``      401    missing or wrong ``X-Internal-Token``
``ai_unavailable``    503    Ollama unreachable, 5xx, or model missing
``ai_timeout``        504    Ollama did not answer within the read timeout
``ai_invalid_response`` 503  the model produced unusable output twice
===================== ====== ==============================================
"""

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.ollama import OllamaInvalidResponse, OllamaTimeout, OllamaUnavailable

logger = logging.getLogger(__name__)


class ApiError(HTTPException):
    """``HTTPException`` that also carries a machine-readable ``code``."""

    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code


def unauthorized() -> ApiError:
    return ApiError(401, "unauthorized", "Invalid internal token")


def ai_unavailable(detail: str = "AI service is unavailable") -> ApiError:
    return ApiError(503, "ai_unavailable", detail)


def ai_timeout(detail: str = "AI request timed out") -> ApiError:
    return ApiError(504, "ai_timeout", detail)


def ai_invalid_response(
    detail: str = "AI produced an invalid response",
) -> ApiError:
    return ApiError(503, "ai_invalid_response", detail)


def _body(detail: Any, code: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"detail": detail}
    if code is not None:
        payload["code"] = code
    return payload


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that render the error contract above."""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.detail, exc.code),
            headers=exc.headers,
        )

    @app.exception_handler(HTTPException)
    async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        # Plain HTTPExceptions (404 on an unknown path, 405, ...) get no code.
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.detail, getattr(exc, "code", None)),
            headers=exc.headers,
        )

    # Upstream failure details (which may quote an Ollama response body) are
    # logged for the operator and never returned to the caller: the detail
    # strings below are static.

    @app.exception_handler(OllamaTimeout)
    async def _timeout(_: Request, exc: OllamaTimeout) -> JSONResponse:
        logger.warning("Ollama timed out: %s", exc)
        error = ai_timeout()
        return JSONResponse(
            status_code=error.status_code, content=_body(error.detail, error.code)
        )

    @app.exception_handler(OllamaUnavailable)
    async def _unavailable(_: Request, exc: OllamaUnavailable) -> JSONResponse:
        logger.warning("Ollama unavailable: %s", exc)
        error = ai_unavailable()
        return JSONResponse(
            status_code=error.status_code, content=_body(error.detail, error.code)
        )

    @app.exception_handler(OllamaInvalidResponse)
    async def _invalid(_: Request, exc: OllamaInvalidResponse) -> JSONResponse:
        logger.warning("Ollama returned an unusable reply: %s", exc)
        error = ai_invalid_response()
        return JSONResponse(
            status_code=error.status_code, content=_body(error.detail, error.code)
        )
