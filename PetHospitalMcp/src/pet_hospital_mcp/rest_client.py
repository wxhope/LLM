"""HTTP client for the Go pet-hospital REST backend.

Only ever talks HTTP to the upstream; never imports or relies on anything else
from the Go service.  Adds a timeout and limited retry on transient failures,
and maps every failure mode onto a canonical :class:`~pet_hospital_mcp.errors.BackendError`
code without leaking httpx or Python internals to the caller.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .errors import BackendError, ErrorCode


class RestClient:
    """Async client for the pet-hospital backend with timeout and limited retry."""

    def __init__(
        self,
        base_url: str,
        timeout: float = 10.0,
        max_retries: int = 2,
        backoff: float = 0.1,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff = backoff
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def list_pets(self, params: dict[str, Any]) -> dict[str, Any]:
        """``GET /api/v1/pets`` and return the decoded JSON body.

        Raises :class:`BackendError` with a canonical code on any failure.
        Transient errors (timeout / connection) are retried up to
        ``max_retries`` times; HTTP error statuses and malformed responses are
        returned immediately.
        """
        url = f"{self._base_url}/api/v1/pets"
        attempts = max(1, self._max_retries + 1)

        for attempt in range(1, attempts + 1):
            try:
                response = await self._client.get(url, params=params)
            except httpx.TimeoutException as exc:
                if attempt >= attempts:
                    raise BackendError(
                        ErrorCode.BACKEND_TIMEOUT,
                        f"backend request timed out after {self._timeout}s",
                    ) from exc
            except httpx.HTTPError as exc:
                if attempt >= attempts:
                    raise BackendError(
                        ErrorCode.BACKEND_UNAVAILABLE,
                        f"backend is unreachable at {self._base_url}",
                    ) from exc
            else:
                if response.status_code != 200:
                    raise BackendError(
                        ErrorCode.BACKEND_API_ERROR,
                        f"backend returned HTTP {response.status_code}",
                        {"status_code": response.status_code},
                    )
                try:
                    return response.json()
                except ValueError as exc:
                    raise BackendError(
                        ErrorCode.BACKEND_INVALID_RESPONSE,
                        "backend returned a non-JSON response",
                    ) from exc

            # Transient failure: back off, then retry.
            await asyncio.sleep(self._backoff * attempt)

        # Unreachable in practice (the loop always returns or raises), kept for safety.
        raise BackendError(ErrorCode.INTERNAL_ERROR, "unexpected backend client state")
