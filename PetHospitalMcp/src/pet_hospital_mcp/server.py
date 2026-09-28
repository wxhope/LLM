"""Assembly of the MCP server and its HTTP application."""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from .config import Settings
from .rest_client import RestClient
from .tools import list_pets as list_pets_tool

SERVICE_NAME = "pet-hospital-mcp"
SERVICE_VERSION = "0.1.0"


def create_server(settings: Settings, client: RestClient | None = None) -> MCPServer:
    """Create a configured MCPServer with the ``list_pets`` tool and ``/health``."""
    server = MCPServer(
        name=SERVICE_NAME,
        title="Pet Hospital MCP Service",
        description="Stateless MCP service exposing the pet-hospital REST API.",
        version=SERVICE_VERSION,
    )

    client = client or RestClient(
        settings.pet_hospital_base_url,
        timeout=settings.backend_timeout_seconds,
        max_retries=settings.backend_max_retries,
    )

    list_pets_tool.register(server, client)

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:  # noqa: ARG001 - interface requirement
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
            }
        )

    return server


def create_app(settings: Settings, client: RestClient | None = None):
    """Create a stateless Streamable HTTP Starlette app for the service."""
    server = create_server(settings, client)
    return server.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        json_response=True,
        stateless_http=True,
        host=settings.mcp_host,
    )
