"""Behavioural tests for the ``list_pets`` tool (adaptation of GET /api/v1/pets)."""

from __future__ import annotations

import httpx

from conftest import (
    call_tool,
    call_tool_raw,
    mcp_client,
    pets_envelope,
    result_of,
    sample_item,
)


async def test_forwards_all_filters_sort_and_pagination(settings):
    captured: dict = {}

    def backend(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json=pets_envelope([sample_item()]))

    async with mcp_client(settings, backend) as http:
        result = result_of(await call_tool(http, {
            "q": "肠",
            "name": "旺财",
            "ownerName": "张三",
            "ownerPhone": "13800001111",
            "species": "犬",
            "doctor": "李医生",
            "disease": "肠胃炎",
            "status": "待就诊",
            "min": 100,
            "max": 5000,
            "sortBy": "totalCost",
            "order": "desc",
            "page": 2,
            "pageSize": 50,
        }))

    assert captured["path"] == "/api/v1/pets"
    assert captured["params"] == {
        "q": "肠",
        "name": "旺财",
        "ownerName": "张三",
        "ownerPhone": "13800001111",
        "species": "犬",
        "doctor": "李医生",
        "disease": "肠胃炎",
        "status": "待就诊",
        "min": "100",
        "max": "5000",
        "sortBy": "totalCost",
        "order": "desc",
        "page": "2",
        "pageSize": "50",
    }

    assert result["isError"] is False
    data = result["structuredContent"]
    assert data["total"] == 1
    assert data["items"][0]["name"] == "旺财"


async def test_validation_error_returns_unified_error(settings):
    called = {"hit": False}

    def backend(request: httpx.Request) -> httpx.Response:
        called["hit"] = True
        return httpx.Response(200, json=pets_envelope())

    async with mcp_client(settings, backend) as http:
        result = result_of(await call_tool(http, {"species": "恐龙", "page": 0}))

    # Validation fails before any backend call happens.
    assert called["hit"] is False
    assert result["isError"] is True
    error = result["structuredContent"]["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["message"]
    assert error["details"]["errors"]


async def test_unknown_field_rejected(settings):
    async with mcp_client(settings, lambda req: httpx.Response(200, json=pets_envelope())) as http:
        result = result_of(await call_tool(http, {"unknownPrivateField": 1}))

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "VALIDATION_ERROR"


async def test_nan_and_infinity_rejected(settings):
    async with mcp_client(settings, lambda req: httpx.Response(200, json=pets_envelope())) as http:
        for bad_arguments in ('{"min": Infinity}', '{"max": NaN}', '{"min": -Infinity}'):
            result = result_of(await call_tool_raw(http, bad_arguments))
            assert result["isError"] is True
            assert result["structuredContent"]["error"]["code"] == "VALIDATION_ERROR"


async def test_backend_http_error(settings):
    def backend(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": 500, "message": "boom", "data": None})

    async with mcp_client(settings, backend) as http:
        result = result_of(await call_tool(http, {"page": 1}))

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "BACKEND_API_ERROR"


async def test_backend_timeout(settings):
    def backend(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    async with mcp_client(settings, backend) as http:
        result = result_of(await call_tool(http, {}))

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "BACKEND_TIMEOUT"


async def test_backend_connection_error(settings):
    def backend(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with mcp_client(settings, backend) as http:
        result = result_of(await call_tool(http, {}))

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "BACKEND_UNAVAILABLE"


async def test_backend_non_json_response(settings):
    def backend(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    async with mcp_client(settings, backend) as http:
        result = result_of(await call_tool(http, {}))

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "BACKEND_INVALID_RESPONSE"


async def test_backend_missing_data_object(settings):
    def backend(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 200, "message": "ok"})

    async with mcp_client(settings, backend) as http:
        result = result_of(await call_tool(http, {}))

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "BACKEND_INVALID_RESPONSE"


async def test_backend_data_wrong_shape(settings):
    def backend(request: httpx.Request) -> httpx.Response:
        # `items` is a dict, not a list -> model validation fails.
        return httpx.Response(200, json=pets_envelope(items={}))

    async with mcp_client(settings, backend) as http:
        result = result_of(await call_tool(http, {}))

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "BACKEND_INVALID_RESPONSE"


async def test_records_and_charges_null_or_array_tolerated(settings):
    items = [
        sample_item(records=None, charges=None),
        sample_item(
            id="PET-000002",
            records=[{"id": "MR-1", "diagnosis": "x"}],
            charges=[],
        ),
    ]

    def backend(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pets_envelope(items=items, total=2))

    async with mcp_client(settings, backend) as http:
        result = result_of(await call_tool(http, {}))

    assert result["isError"] is False
    data = result["structuredContent"]
    assert data["items"][0]["records"] is None
    assert data["items"][0]["charges"] is None
    assert data["items"][1]["records"] == [{"id": "MR-1", "diagnosis": "x"}]
    assert data["items"][1]["charges"] == []
