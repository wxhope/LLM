"""MCP tool implementations.

Each module exposes a ``register(server, client)`` function; adding a new tool
only requires adding a module here and reusing the shared REST client, logger
and error conventions.
"""

from __future__ import annotations

from . import list_pets

__all__ = ["list_pets"]
