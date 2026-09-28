"""Entry point: run the MCP service over stateless Streamable HTTP."""

from __future__ import annotations

from pathlib import Path

from .config import Settings, load_dotenv
from .server import create_server

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    settings = Settings.from_env()

    server = create_server(settings)
    server.run(
        transport="streamable-http",
        host=settings.mcp_host,
        port=settings.mcp_port,
        streamable_http_path=settings.mcp_path,
        json_response=True,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
