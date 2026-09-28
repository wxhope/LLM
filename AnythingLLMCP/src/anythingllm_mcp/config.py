"""Runtime configuration, read from environment variables with optional .env support."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULTS: dict[str, str] = {
    "ANYTHINGLLM_BASE_URL": "http://localhost:3001",
    "ANYTHINGLLM_API_KEY": "",
    "ANYTHINGLLM_WORKSPACE": "",
    "ANYTHINGLLM_MODE": "query",
    "MCP_HOST": "127.0.0.1",
    "MCP_PORT": "8765",
    "MCP_PATH": "/mcp",
    "REQUEST_TIMEOUT": "120",
}


def load_dotenv(path: Path) -> None:
    """Minimal .env loader.

    ``KEY=VALUE`` per line, ``#`` starts a comment, surrounding quotes are
    stripped.  Real environment variables always win over the file, so a shell
    export can override the checked-in values.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


@dataclass(frozen=True)
class Settings:
    """Immutable service settings."""

    anythingllm_base_url: str
    anythingllm_api_key: str
    anythingllm_workspace: str
    anythingllm_mode: str
    mcp_host: str
    mcp_port: int
    mcp_path: str
    request_timeout: float

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from the process environment, validating what matters."""
        env = {key: os.environ.get(key, default) for key, default in DEFAULTS.items()}

        api_key = env["ANYTHINGLLM_API_KEY"].strip()
        if not api_key:
            raise SystemExit(
                "ANYTHINGLLM_API_KEY is not set.\n"
                "Put it in the .env file next to pyproject.toml "
                "(AnythingLLM → Settings → Tools → Developer API)."
            )

        mode = env["ANYTHINGLLM_MODE"].strip().lower()
        if mode not in ("query", "chat"):
            raise SystemExit("ANYTHINGLLM_MODE must be either 'query' or 'chat'.")

        return cls(
            anythingllm_base_url=env["ANYTHINGLLM_BASE_URL"].strip().rstrip("/"),
            anythingllm_api_key=api_key,
            anythingllm_workspace=env["ANYTHINGLLM_WORKSPACE"].strip(),
            anythingllm_mode=mode,
            mcp_host=env["MCP_HOST"].strip(),
            mcp_port=int(env["MCP_PORT"]),
            mcp_path=env["MCP_PATH"].strip(),
            request_timeout=float(env["REQUEST_TIMEOUT"]),
        )
