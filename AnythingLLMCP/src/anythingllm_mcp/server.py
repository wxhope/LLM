"""Assembly of the MCP server and the single tool it exposes."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent
from starlette.requests import Request
from starlette.responses import JSONResponse

from .client import AnythingLLMClient, AnythingLLMError
from .config import Settings

SERVICE_NAME = "anythingllm-mcp"
SERVICE_VERSION = "0.1.0"

TOOL_NAME = "ask_workspace"

TOOL_DESCRIPTION = (
    "Answer a question using the documents stored in the local AnythingLLM workspace. "
    "Returns the grounded answer together with the source documents it came from. "
    "Call this whenever the question has to be answered from that knowledge base "
    "instead of your own general knowledge."
)


def _sources(raw: Any) -> list[str]:
    """Flatten AnythingLLM source entries into a list of human-readable labels."""
    if not isinstance(raw, list):
        return []
    labels: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            label = item.get("title") or item.get("id") or item.get("url")
            labels.append(str(label) if label else "untitled source")
        else:
            labels.append(str(item))
    return labels


def _ok(answer: str, slug: str, sources: list[str]) -> CallToolResult:
    """Successful tool result: readable text plus a machine-readable payload."""
    text = answer or "The workspace returned an empty answer."
    if sources:
        text = f"{text}\n\nSources: " + "; ".join(sources)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content={
            "workspace": slug,
            "answer": answer,
            "sources": sources,
        },
        is_error=False,
    )


def _fail(message: str) -> CallToolResult:
    """Failed tool result: the client sees a readable reason, not a stack trace."""
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        structured_content={"error": message},
        is_error=True,
    )


def create_server(settings: Settings, client: AnythingLLMClient | None = None) -> MCPServer:
    """Create the MCP server with the ``ask_workspace`` tool and a ``/health`` route."""
    server = MCPServer(
        name=SERVICE_NAME,
        title="AnythingLLM Workspace MCP Service",
        description="Ask questions against the local AnythingLLM workspace.",
        version=SERVICE_VERSION,
    )

    client = client or AnythingLLMClient(
        settings.anythingllm_base_url,
        settings.anythingllm_api_key,
        timeout=settings.request_timeout,
    )

    # The workspace slug is configuration, not session state, but it is resolved
    # lazily so the service still starts when AnythingLLM is not running yet.
    resolved: dict[str, str] = {}

    async def current_slug() -> str:
        slug = resolved.get("slug")
        if slug is None:
            slug = await client.resolve_workspace(settings.anythingllm_workspace)
            resolved["slug"] = slug
        return slug

    async def ask_workspace(question: str) -> CallToolResult:
        """Ask a question about the contents of the AnythingLLM workspace.

        Args:
            question: The natural-language question to answer from the workspace.
        """
        try:
            slug = await current_slug()
            data = await client.chat(slug, question, settings.anythingllm_mode)
        except AnythingLLMError as exc:
            return _fail(str(exc))
        except Exception as exc:  # defensive: never leak a traceback to the client
            return _fail(f"Unexpected error while querying AnythingLLM: {exc}")

        answer = data.get("textResponse") or ""
        return _ok(str(answer), slug, _sources(data.get("sources")))

    server.add_tool(
        ask_workspace,
        name=TOOL_NAME,
        title="Ask the AnythingLLM workspace",
        description=TOOL_DESCRIPTION,
    )

    @server.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:  # noqa: ARG001 - route signature
        return JSONResponse(
            {
                "status": "ok",
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "mode": settings.anythingllm_mode,
                "workspace": resolved.get("slug") or settings.anythingllm_workspace or "auto",
            }
        )

    return server
