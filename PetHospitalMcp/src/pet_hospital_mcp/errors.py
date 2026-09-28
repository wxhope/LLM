"""Unified structured error types for the MCP service.

Every failure is surfaced to the MCP client with the same envelope::

    {
      "error": {
        "code": "ERROR_CODE",
        "message": "human readable message",
        "details": {}
      }
    }

Only the canonical codes below are ever emitted.  Internal exception details
(httpx, Pydantic, SDK, Python tracebacks) are never leaked to the client.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode:
    """Canonical error codes returned to MCP clients."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    BACKEND_TIMEOUT = "BACKEND_TIMEOUT"
    BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
    BACKEND_API_ERROR = "BACKEND_API_ERROR"
    BACKEND_INVALID_RESPONSE = "BACKEND_INVALID_RESPONSE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorDetail(BaseModel):
    """A single structured error entry."""

    model_config = ConfigDict(extra="allow")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """The unified error envelope returned on every failure."""

    model_config = ConfigDict(extra="allow")

    error: ErrorDetail


def build_error(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a serializable unified-error payload."""
    return ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details or {})
    ).model_dump(mode="json")


class BackendError(Exception):
    """An upstream failure, carrying a canonical error code.

    Raised by the REST client and translated by the tool layer into a unified
    error result.  The ``message`` is safe to surface to the client.
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
