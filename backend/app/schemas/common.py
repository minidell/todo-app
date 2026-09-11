"""Shared response schemas (health, error envelope)."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response body for ``GET /api/health``."""

    status: Literal["ok"]


class ReadyResponse(BaseModel):
    """Response body for ``GET /api/health/ready``."""

    status: Literal["ok"]
    database: Literal["ok"]


class ErrorResponse(BaseModel):
    """The error envelope of master §5 — documents non-422 failures."""

    detail: str
    code: str
