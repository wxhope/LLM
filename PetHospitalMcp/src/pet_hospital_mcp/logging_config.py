"""JSON logging configuration with recursive sensitive-field redaction.

Tool-call logs are emitted as one JSON object per line containing at least
``timestamp``, ``tool_name``, ``params``, ``status`` and ``duration_ms``.

Sensitive fields ``ownerPhone`` / ``ownerAddr`` / ``chipNo`` (and their
snake_case variants) are recursively replaced with ``***`` before anything is
written, so full personal data never reaches the log.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

SENSITIVE_FIELDS = frozenset({"ownerphone", "owneraddr", "chipno"})
REDACTED = "***"


def _normalize_key(key: str) -> str:
    """Lower-case and strip separators so camelCase and snake_case match."""
    return key.lower().replace("_", "").replace("-", "")


def redact(value: Any) -> Any:
    """Recursively redact sensitive values in an arbitrary structure."""
    if isinstance(value, dict):
        return {
            key: (REDACTED if _normalize_key(str(key)) in SENSITIVE_FIELDS else redact(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in ("tool_name", "params", "status", "duration_ms"):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = redact(value)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the ``pet_hospital_mcp`` logger hierarchy with JSON output."""
    logger = logging.getLogger("pet_hospital_mcp")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``pet_hospital_mcp`` namespace."""
    return logging.getLogger(name)
