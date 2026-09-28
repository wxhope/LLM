"""MCP registration, schema, and SDK 2.x stateless-connection tests."""

from __future__ import annotations

import json

import httpx

from conftest import (
    call_tool,
    discover,
    list_tools,
    make_settings,
    mcp_client,
    pets_envelope,
    sample_item,
)
from pet_hospital_mcp.server import create_server

SPECIES = ["犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"]


async def test_tool_registered_with_name_and_schema():
    server = create_server(make_settings())
    tools = await server.list_tools()

    assert [tool.name for tool in tools] == ["list_pets"]

    schema = tools[0].input_schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False

    props = schema["properties"]
    assert "species" in props
    assert "status" in props
    assert "sortBy" in props
    assert "order" in props
    assert "page" in props
    assert "pageSize" in props

    # Real backend allowed values are advertised as enums.
    species_schema = json.dumps(props["species"], ensure_ascii=False)
    for value in SPECIES:
        assert value in species_schema
    assert "待就诊" in json.dumps(props["status"], ensure_ascii=False)
    assert "慢性病随访" in json.dumps(props["status"], ensure_ascii=False)
    assert "totalCost" in json.dumps(props["sortBy"], ensure_ascii=False)
    assert "desc" in json.dumps(props["order"], ensure_ascii=False)

    # Numeric constraints are reflected in the schema.
    assert props["page"]["minimum"] == 1
    assert props["pageSize"]["minimum"] == 1
    assert props["pageSize"]["maximum"] == 500
    for field in ("min", "max"):
        dumped = json.dumps(props[field], ensure_ascii=False)
        assert '"ge": 0' in dumped or '"minimum": 0' in dumped


async def test_stateless_discovery_list_and_call(settings):
    def backend(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pets_envelope([sample_item()]))

    async with mcp_client(settings, backend) as http:
        # 1. Modern discovery (server/discover), no legacy `initialize` is sent.
        resp = await discover(http)
        assert resp.status_code == 200
        assert "mcp-session-id" not in resp.headers
        discover_result = resp.json()["result"]
        assert "2026-07-28" in discover_result["supportedVersions"]
        assert "tools" in discover_result["capabilities"]

        # 2. The tool is discoverable via tools/list.
        resp = await list_tools(http)
        assert resp.status_code == 200
        assert "mcp-session-id" not in resp.headers
        assert [t["name"] for t in resp.json()["result"]["tools"]] == ["list_pets"]

        # 3. The tool is callable over the HTTP MCP endpoint.
        resp = await call_tool(http, {"species": "犬", "pageSize": 10})
        assert resp.status_code == 200
        assert "mcp-session-id" not in resp.headers
        result = resp.json()["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["total"] == 1
        assert result["structuredContent"]["items"][0]["name"] == "旺财"

        # 4. Health check endpoint.
        health = await http.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
