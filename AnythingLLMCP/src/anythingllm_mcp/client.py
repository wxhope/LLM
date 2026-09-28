"""HTTP client for the local AnythingLLM instance.

The only upstream endpoints this service needs:

* ``GET  /api/v1/workspaces``                 - resolve which workspace to use
* ``POST /api/v1/workspace/{slug}/chat``      - ask a question

Every failure is raised as :class:`AnythingLLMError` carrying a message that is
safe to show to the AI client.  Nothing about httpx leaks outwards.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx


class AnythingLLMError(RuntimeError):
    """A failure while talking to AnythingLLM, with a human-readable message."""


class AnythingLLMClient:
    """Async client for the AnythingLLM developer API."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 120.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def ping(self) -> bool:
        """Return ``True`` if the AnythingLLM process answers at all (no auth needed)."""
        try:
            response = await self._client.get(f"{self._base_url}/api/ping", timeout=5.0)
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        """Perform one request and decode JSON, mapping every failure to AnythingLLMError."""
        url = f"{self._base_url}{path}"
        try:
            response = await self._client.request(method, url, json=payload)
        except httpx.TimeoutException as exc:
            raise AnythingLLMError(
                f"AnythingLLM did not answer within {self._timeout:.0f}s "
                f"({method} {path}). The local model may still be loading."
            ) from exc
        except httpx.HTTPError as exc:
            raise AnythingLLMError(
                f"Cannot reach AnythingLLM at {self._base_url}. "
                "Make sure the AnythingLLM app is running."
            ) from exc

        if response.status_code == 403:
            raise AnythingLLMError(
                "AnythingLLM rejected the API key (HTTP 403). "
                "Regenerate it in Settings → Tools → Developer API and update .env."
            )
        if response.status_code == 404:
            raise AnythingLLMError(
                f"AnythingLLM returned HTTP 404 for {method} {path} - "
                "the workspace slug probably does not exist."
            )
        if response.status_code != 200:
            raise AnythingLLMError(
                f"AnythingLLM returned HTTP {response.status_code}: {response.text[:200]}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise AnythingLLMError("AnythingLLM returned a response that is not JSON.") from exc

    async def resolve_workspace(self, configured_slug: str) -> str:
        """Return the workspace slug to use.

        An explicit slug (from ``ANYTHINGLLM_WORKSPACE``) always wins.  Otherwise
        the workspace list is queried and the single workspace is used; zero or
        several workspaces is an error the operator has to resolve.
        """
        if configured_slug:
            return configured_slug

        data = await self._request("GET", "/api/v1/workspaces")
        workspaces = data.get("workspaces") or []
        if not workspaces:
            raise AnythingLLMError("AnythingLLM has no workspace to query.")
        if len(workspaces) > 1:
            options = ", ".join(
                f"{item.get('name')}={item.get('slug')}" for item in workspaces
            )
            raise AnythingLLMError(
                f"AnythingLLM has {len(workspaces)} workspaces, so the target is ambiguous. "
                f"Set ANYTHINGLLM_WORKSPACE to one of: {options}"
            )
        return str(workspaces[0]["slug"])

    async def chat(self, slug: str, message: str, mode: str = "query") -> dict[str, Any]:
        """Ask ``message`` in workspace ``slug`` and return the raw response body.

        A fresh ``sessionId`` is generated per call on purpose: reusing one would
        append every question to the same thread, and the model would start
        reading its own earlier answers as context.
        """
        payload = {
            "message": message,
            "mode": mode,
            "sessionId": str(uuid.uuid4()),
        }
        data = await self._request("POST", f"/api/v1/workspace/{slug}/chat", payload)
        if not isinstance(data, dict):
            raise AnythingLLMError("AnythingLLM returned an unexpected response shape.")
        if data.get("error"):
            raise AnythingLLMError(f"AnythingLLM reported an error: {data['error']}")
        return data
