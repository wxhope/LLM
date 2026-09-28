"""Runtime configuration, loaded from environment variables.

Environment variables:

* ``PET_HOSPITAL_BASE_URL`` - upstream Go REST API base URL (default ``http://127.0.0.1:8080``)
* ``MCP_HOST``             - address the MCP service binds to (default ``127.0.0.1``)
* ``MCP_PORT``             - port the MCP service binds to (default ``8000``)
* ``MCP_PATH``             - Streamable HTTP endpoint path (default ``/mcp``)
* ``PET_HOSPITAL_TIMEOUT`` - upstream request timeout in seconds (default ``10``)
* ``PET_HOSPITAL_MAX_RETRIES`` - upstream retries on transient errors (default ``2``)
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_MCP_PATH = "/mcp"
DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 2


@dataclass(frozen=True)
class Settings:
    """Immutable service settings."""

    pet_hospital_base_url: str
    mcp_host: str
    mcp_port: int
    backend_timeout_seconds: float
    backend_max_retries: int
    mcp_path: str = DEFAULT_MCP_PATH

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from environment variables (or an explicit mapping)."""
        env: Mapping[str, str] = os.environ if environ is None else environ
        return cls(
            pet_hospital_base_url=env.get("PET_HOSPITAL_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            mcp_host=env.get("MCP_HOST", DEFAULT_HOST),
            mcp_port=int(env.get("MCP_PORT", str(DEFAULT_PORT))),
            backend_timeout_seconds=float(env.get("PET_HOSPITAL_TIMEOUT", str(DEFAULT_TIMEOUT))),
            backend_max_retries=int(env.get("PET_HOSPITAL_MAX_RETRIES", str(DEFAULT_MAX_RETRIES))),
            mcp_path=env.get("MCP_PATH", DEFAULT_MCP_PATH),
        )
