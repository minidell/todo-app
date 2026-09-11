"""Error contract v2 (master spec §5, decisions D-E1/D-E2).

Every error the application raises deliberately is an :class:`AppError`, which
serialises to ``{"detail": "<human string>", "code": "<machine code>"}``.
Pydantic validation errors keep FastAPI's default 422 body and carry **no**
``code`` — the frontend never parses that structure.

D-E2: a resource owned by another user is reported as 404, never 403. There is
no ``forbidden`` code in iteration 2.
"""

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request

#: Body fields whose submitted value must never be echoed back or logged.
SENSITIVE_FIELDS = frozenset({"password"})

#: What replaces a redacted ``input``. A placeholder rather than a removed key,
#: so the 422 entry keeps its shape and the redaction is visible to a developer
#: reading the response.
REDACTED = "[redacted]"


class AppError(Exception):
    """Base class for every deliberate application error."""

    status_code: int = 400
    code: str = "error"
    detail: str = "Error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        if detail is not None:
            self.detail = detail
        self.headers = headers or {}
        super().__init__(self.detail)


# --- 404 -------------------------------------------------------------------
class NotFoundError(AppError):
    status_code = 404
    code = "not_found"
    detail = "Not found"


class TodoNotFound(NotFoundError):
    code = "todo_not_found"
    detail = "Todo not found"


class ListNotFound(NotFoundError):
    code = "list_not_found"
    detail = "List not found"


class TagNotFound(NotFoundError):
    code = "tag_not_found"
    detail = "Tag not found"


# --- 401 / 409 / 400 -------------------------------------------------------
class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"
    detail = "Not authenticated"


class InvalidCredentials(AppError):
    status_code = 401
    code = "invalid_credentials"
    detail = "Invalid email or password"


class EmailTaken(AppError):
    status_code = 409
    code = "email_taken"
    detail = "That email is already registered"


class ListNameTaken(AppError):
    status_code = 409
    code = "list_name_taken"
    detail = "A list with that name already exists"


class TagNameTaken(AppError):
    status_code = 409
    code = "tag_name_taken"
    detail = "A tag with that name already exists"


class CannotDeleteLastList(AppError):
    status_code = 409
    code = "cannot_delete_last_list"
    detail = "You must keep at least one list"


class SubtaskDepthExceeded(AppError):
    status_code = 400
    code = "subtask_depth_exceeded"
    detail = "Subtasks can only be one level deep"


class SubtaskListMismatch(AppError):
    """``parent_id`` and ``list_id`` in one body that disagree.

    Additive to the §5 table, in the shape of its existing
    ``conflicting_due_filters``: two client-supplied fields contradict each
    other, and only the client can say which it meant. Silently letting the
    parent win — the previous behaviour — filed the todo somewhere the caller
    did not ask for and answered 201 as if it had complied.
    """

    status_code = 400
    code = "subtask_list_mismatch"
    detail = "A subtask always belongs to its parent's list"


class EmptyUpdate(AppError):
    status_code = 400
    code = "empty_update"
    detail = "No fields to update"


class ConflictingDueFilters(AppError):
    status_code = 400
    code = "conflicting_due_filters"
    detail = "Use either a due preset or a due date range, not both"


# --- infrastructure --------------------------------------------------------
class RequestTooLarge(AppError):
    status_code = 413
    code = "request_too_large"
    detail = "Request body too large"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"
    detail = "Too many requests. Please wait and try again."


class DatabaseUnavailable(AppError):
    status_code = 503
    code = "database_unavailable"
    detail = "Database unavailable"


class AiDisabled(AppError):
    status_code = 503
    code = "ai_disabled"
    detail = "AI features are disabled"


class AiUnavailable(AppError):
    status_code = 503
    code = "ai_unavailable"
    detail = "AI service is unavailable"


class AiTimeout(AppError):
    status_code = 504
    code = "ai_timeout"
    detail = "AI request timed out"


def error_body(error: AppError) -> dict[str, str]:
    return {"detail": error.detail, "code": error.code}


def is_sensitive_name(part: object) -> bool:
    """Whether a ``loc`` element or object key names a secret.

    Matched case-insensitively: the key comes from the client, and a body
    carrying ``"Password"`` leaks exactly as much as one carrying ``"password"``.
    """
    return isinstance(part, str) and part.lower() in SENSITIVE_FIELDS


def is_sensitive_location(location: object) -> bool:
    """Whether a pydantic error ``loc`` points at a secret field.

    Any element matches, not just the last one, so a password nested in a list
    or a sub-model (``["body", "accounts", 0, "password"]``) is covered too.
    """
    if not isinstance(location, (list, tuple)):
        return False
    return any(is_sensitive_name(part) for part in location)


def scrub_value(value: object) -> object:
    """Replace the value of every password-named key, at any depth.

    ``loc`` is not enough on its own. Pydantic reports a *missing* field with
    the whole surrounding object as ``input``, and a wrong-type body with the
    entire payload at ``loc == ["body"]`` — so ``POST /api/auth/register`` with
    the email left out answers 422 with the submitted password echoed inside a
    ``loc`` that names no secret at all. That is the same plaintext leak the
    field-level redaction exists to prevent, one nesting level deeper, and it
    happens on precisely the requests a user is most likely to retry with a
    password they use elsewhere.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED if is_sensitive_name(key) else scrub_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub_value(item) for item in value]
    return value


def redact_errors(exc: RequestValidationError) -> list[dict]:
    """FastAPI's error array with sensitive ``input`` values replaced."""
    errors = jsonable_encoder(exc.errors())
    for error in errors:
        if not isinstance(error, dict) or "input" not in error:
            continue
        if is_sensitive_location(error.get("loc")):
            # The whole input *is* the secret.
            error["input"] = REDACTED
        else:
            error["input"] = scrub_value(error["input"])
    return errors


class CodedHTTPException(HTTPException):
    """An ``HTTPException`` that also carries a machine-readable ``code``."""

    def __init__(self, status_code: int, detail: str, code: str, **kwargs) -> None:
        super().__init__(status_code=status_code, detail=detail, **kwargs)
        self.code = code


def register_error_handlers(app: FastAPI) -> None:
    """Install the handlers implementing the error contract."""

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc),
            headers=exc.headers or None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        code = getattr(exc, "code", None)
        if code is None:
            # Framework-raised (routing 404, 405, …): FastAPI's default body.
            return await http_exception_handler(request, exc)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail, "code": code},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """FastAPI's own 422 body, with sensitive submitted values removed.

        Pydantic echoes the rejected value back in ``input`` so a developer can
        see what was wrong with it. For a password that means the plaintext
        travels back over the wire and lands in whatever logs or error trackers
        the client keeps — for a *rejected* registration, which is exactly when
        someone is most likely to be re-typing a password they use elsewhere.

        Only ``input`` under a sensitive field is touched: the array shape,
        the ``loc``/``msg``/``type`` keys and the absence of ``code`` are the
        frozen contract (D-E1) and stay byte-identical everywhere else.
        """
        return JSONResponse(status_code=422, content={"detail": redact_errors(exc)})
