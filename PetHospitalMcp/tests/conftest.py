"""Shared fixtures and helpers for the MCP service tests.

All tests run fully in-process: the upstream Go service is mocked with
``httpx.MockTransport`` (never contacted) and the MCP service is exercised over
its real Streamable HTTP endpoint via ``httpx.ASGITransport``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.rest_client import RestClient
from pet_hospital_mcp.server import create_app

PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"


def make_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = dict(
        pet_hospital_base_url="http://127.0.0.1:8080",
        mcp_host="127.0.0.1",
        mcp_port=8000,
        backend_timeout_seconds=5.0,
        backend_max_retries=0,
        mcp_path="/mcp",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def _meta() -> dict[str, Any]:
    return {
        PROTOCOL_VERSION_META_KEY: PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META_KEY: {},
    }


def _headers(method: str, name: str | None = None) -> dict[str, str]:
    headers = {
        "mcp-protocol-version": PROTOCOL_VERSION,
        "mcp-method": method,
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    if name is not None:
        headers["mcp-name"] = name
    return headers


@asynccontextmanager
async def mcp_client(
    settings: Settings,
    backend_handler: Callable[[httpx.Request], httpx.Response],
):
    """Run the MCP app over an in-process HTTP client backed by ``backend_handler``.

    Yields an ``httpx.AsyncClient`` whose base URL is the MCP endpoint.  The
    app's lifespan (session manager) is entered for the duration of the context.
    """
    backend = httpx.MockTransport(backend_handler)
    rest_client = RestClient(
        settings.pet_hospital_base_url,
        timeout=settings.backend_timeout_seconds,
        max_retries=settings.backend_max_retries,
        backoff=0.0,
        transport=backend,
    )
    app = create_app(settings, rest_client)
    asgi = httpx.ASGITransport(app=app)
    base_url = f"http://{settings.mcp_host}:{settings.mcp_port}"
    try:
        async with httpx.AsyncClient(transport=asgi, base_url=base_url) as http:
            async with app.router.lifespan_context(app):
                yield http
    finally:
        await rest_client.close()


# --- MCP JSON-RPC helpers -------------------------------------------------


async def call_tool(http: httpx.AsyncClient, arguments: dict[str, Any]) -> httpx.Response:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "list_pets", "arguments": arguments, "_meta": _meta()},
    }
    return await http.post("/mcp", json=body, headers=_headers("tools/call", "list_pets"))


async def call_tool_raw(http: httpx.AsyncClient, arguments_json: str) -> httpx.Response:
    """Call the tool with a raw JSON ``arguments`` literal.

    ``httpx`` refuses to encode ``Infinity``/``NaN`` via ``json=``, so this
    helper sends a hand-built body to exercise those non-finite inputs.
    """
    body = (
        '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{'
        '"name":"list_pets","arguments":' + arguments_json + ','
        '"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28",'
        '"io.modelcontextprotocol/clientCapabilities":{}}}}'
    )
    return await http.post("/mcp", content=body, headers=_headers("tools/call", "list_pets"))


async def discover(http: httpx.AsyncClient) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": _meta()}}
    return await http.post("/mcp", json=body, headers=_headers("server/discover"))


async def list_tools(http: httpx.AsyncClient) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": _meta()}}
    return await http.post("/mcp", json=body, headers=_headers("tools/list"))


def result_of(resp: httpx.Response) -> dict[str, Any]:
    """Return the JSON-RPC ``result`` object of a successful response."""
    return resp.json()["result"]


# --- Sample backend payloads ----------------------------------------------


def pets_envelope(items: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    items = items if items is not None else []
    data: dict[str, Any] = {
        "items": items,
        "total": len(items),
        "page": 1,
        "pageSize": 20,
        "totalPages": 1 if items else 0,
        "totalCost": sum(i.get("totalCost", 0.0) for i in items),
    }
    data.update(overrides)
    return {"code": 200, "message": "ok", "data": data, "time": "2026-09-17T10:00:00+08:00"}


def sample_item(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": "PET-000001",
        "name": "旺财",
        "species": "犬",
        "breed": "金毛",
        "gender": "公",
        "ageMonths": 36,
        "color": "金色",
        "chipNo": "CHIP-000001",
        "ownerName": "张三",
        "ownerPhone": "13800001111",
        "ownerAddr": "成都市锦江区",
        "doctor": "李医生",
        "disease": "急性肠胃炎",
        "status": "待就诊",
        "allergy": "无",
        # records may be null, charges may be an array.
        "records": None,
        "charges": [
            {
                "id": "CH-000001",
                "item": "血常规检查",
                "category": "检查",
                "amount": 180.0,
                "doctor": "李医生",
                "date": "2026-01-01",
            }
        ],
        "totalCost": 180.0,
        "visitCount": 0,
        "createdAt": "2026-01-01T00:00:00+08:00",
        "updatedAt": "2026-01-01T00:00:00+08:00",
    }
    item.update(overrides)
    return item
