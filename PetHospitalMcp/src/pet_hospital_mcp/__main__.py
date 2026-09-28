"""Entry point: run the MCP service as a stateless Streamable HTTP server."""

from __future__ import annotations

from .config import Settings
from .logging_config import setup_logging
from .server import create_server


def main() -> None:
    settings = Settings.from_env()
    setup_logging()

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
