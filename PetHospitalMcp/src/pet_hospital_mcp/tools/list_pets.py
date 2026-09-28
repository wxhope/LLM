"""The ``list_pets`` MCP tool.

Strictly adapts ``GET /api/v1/pets`` from the Go pet-hospital REST API.

Arguments are read raw (un-coerced) from the request and validated against a
strict Pydantic model, so unknown fields, NaN/Infinity and type-incorrect
values are rejected with the unified error structure rather than the SDK's
lax permissive parsing.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..errors import BackendError, ErrorCode, build_error
from ..rest_client import RestClient

logger = logging.getLogger("pet_hospital_mcp.tools.list_pets")

TOOL_NAME = "list_pets"

# Real backend allowed values (from GET /api/v1/meta).
SPECIES = ["犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"]
STATUS = ["待就诊", "就诊中", "住院中", "已康复", "慢性病随访"]
SORT_FIELDS = [
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
]
ORDER = ["asc", "desc"]

TOOL_DESCRIPTION = (
    "Query the pet-hospital archive (GET /api/v1/pets). "
    "Returns a paginated list of pet records, each with visit history (records), "
    "charges, and the aggregate fields totalCost and visitCount. "
    "Use it to search, filter, sort, and page through pet archives. "
    "Filters: q (keyword), name, ownerName, ownerPhone, species, doctor, disease, "
    "status, min/max (totalCost range). "
    "Sorting: sortBy (id|name|ownerName|species|doctor|disease|status|totalCost|"
    "visitCount|createdAt|updatedAt) with order (asc|desc). "
    "Pagination: page (>=1, default 1) and pageSize (1..500, default 20). "
    "On success the result is the backend ``data`` object: items, total, page, "
    "pageSize, totalPages, totalCost."
)


class ListPetsInput(BaseModel):
    """Strict input model for the ``list_pets`` tool."""

    model_config = ConfigDict(extra="forbid", strict=True)

    q: str | None = None
    name: str | None = None
    ownerName: str | None = None
    ownerPhone: str | None = None
    species: Literal[tuple(SPECIES)] | None = None
    doctor: str | None = None
    disease: str | None = None
    status: Literal[tuple(STATUS)] | None = None
    min: int | float | None = Field(default=None, ge=0)
    max: int | float | None = Field(default=None, ge=0)
    sortBy: Literal[tuple(SORT_FIELDS)] | None = None
    order: Literal[tuple(ORDER)] | None = None
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=20, ge=1, le=500)

    @model_validator(mode="after")
    def _check_min_max(self) -> "ListPetsInput":
        for field_name in ("min", "max"):
            value = getattr(self, field_name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{field_name} must be a finite number")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must be less than or equal to max")
        return self


# Success output models.  Tolerant by design: the Go API may return
# ``records`` / ``charges`` as either ``null`` or an array, and may add fields;
# unknown fields are kept, never rejected.
class PetRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    visitDate: str | None = None
    doctor: str | None = None
    diagnosis: str | None = None
    symptoms: str | None = None
    treatment: str | None = None
    prescription: list[str] | None = None
    weightKg: float | None = None
    temperature: float | None = None
    followUp: str | None = None
    charge: float | None = None
    createdAt: str | None = None


class PetCharge(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    item: str | None = None
    category: str | None = None
    amount: float | None = None
    doctor: str | None = None
    date: str | None = None


class PetItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str | None = None
    name: str | None = None
    species: str | None = None
    breed: str | None = None
    gender: str | None = None
    ageMonths: int | None = None
    color: str | None = None
    chipNo: str | None = None
    ownerName: str | None = None
    ownerPhone: str | None = None
    ownerAddr: str | None = None
    doctor: str | None = None
    disease: str | None = None
    status: str | None = None
    allergy: str | None = None
    note: str | None = None
    records: list[PetRecord] | None = None
    charges: list[PetCharge] | None = None
    totalCost: float | None = None
    visitCount: int | None = None
    createdAt: str | None = None
    updatedAt: str | None = None


class ListPetsData(BaseModel):
    """Successful ``data`` payload from ``GET /api/v1/pets``."""

    model_config = ConfigDict(extra="allow")

    items: list[PetItem] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    pageSize: int = 20
    totalPages: int = 0
    totalCost: float | None = None


def _format_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    """Flatten Pydantic validation errors into a leak-free details list."""
    errors: list[dict[str, Any]] = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err.get("loc", ()))
        errors.append({"field": field, "message": err.get("msg", "")})
    return errors


def _error_result(code: str, message: str, details: dict[str, Any] | None = None) -> CallToolResult:
    """Build a unified error ``CallToolResult`` marked as failed."""
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        structured_content=build_error(code, message, details),
        is_error=True,
    )


def _tool(client: RestClient) -> Any:
    """Return the ``list_pets`` tool function bound to ``client``."""

    async def list_pets(ctx: Context) -> CallToolResult:
        start = time.perf_counter()

        raw: dict[str, Any] = {}
        if ctx._input_params is not None:
            raw = getattr(ctx._input_params, "arguments", None) or {}

        status = "success"
        try:
            params = ListPetsInput.model_validate(raw)
        except ValidationError as exc:
            status = "error"
            result = _error_result(
                ErrorCode.VALIDATION_ERROR,
                "invalid tool arguments",
                {"errors": _format_validation_errors(exc)},
            )
        else:
            query = params.model_dump(exclude_none=True)
            try:
                payload = await client.list_pets(query)
            except BackendError as exc:
                status = "error"
                result = _error_result(exc.code, exc.message, exc.details)
            except Exception:  # pragma: no cover - defensive last resort
                logger.exception("unexpected error while calling the backend")
                status = "error"
                result = _error_result(ErrorCode.INTERNAL_ERROR, "internal server error")
            else:
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
                    status = "error"
                    result = _error_result(
                        ErrorCode.BACKEND_INVALID_RESPONSE,
                        "backend response is missing the 'data' object",
                    )
                else:
                    try:
                        data = ListPetsData.model_validate(payload["data"])
                    except ValidationError as exc:
                        status = "error"
                        result = _error_result(
                            ErrorCode.BACKEND_INVALID_RESPONSE,
                            "backend response does not match the expected data model",
                            {"errors": _format_validation_errors(exc)},
                        )
                    else:
                        result = CallToolResult(
                            content=[
                                TextContent(
                                    type="text",
                                    text=(
                                        f"Found {data.total} pet record(s) "
                                        f"(page {data.page} of {data.totalPages})"
                                    ),
                                )
                            ],
                            # Return the validated Go ``data`` object verbatim so the
                            # output corresponds exactly to the upstream response
                            # (including ``records``/``charges`` as null or array).
                            structured_content=payload["data"],
                        )

        duration_ms = round((time.perf_counter() - start) * 1000, 3)
        logger.info(
            "tool call completed",
            extra={
                "tool_name": TOOL_NAME,
                "params": raw,
                "status": status,
                "duration_ms": duration_ms,
            },
        )
        return result

    return list_pets


def register(server: MCPServer, client: RestClient) -> None:
    """Register the ``list_pets`` tool (with its strict input schema) on ``server``."""
    server.add_tool(_tool(client), name=TOOL_NAME, title="List pet records", description=TOOL_DESCRIPTION)

    # The function intentionally takes only `ctx` so the SDK never coerces or
    # drops arguments before our strict validation runs.  The auto-generated
    # input schema is therefore empty; replace it with the strict model's schema
    # so discovery advertises the real parameters, enums and constraints.
    server._tool_manager._tools[TOOL_NAME].parameters = ListPetsInput.model_json_schema()
