# UPGRADE_PROMPT — 阶段二新增工具指南

本文档用于指导后续阶段在 **不改动现有架构** 的前提下，为 MCP 服务新增工具。

## 现状

- 当前只实现一个工具 `list_pets`，严格适配 `GET /api/v1/pets`。
- 其余 Go REST 接口（`POST /api/v1/pets`、`GET /api/v1/pets/{id}`、
  `DELETE /api/v1/pets/{id}`、`/records`、`/charges`、`/stats`、`/search` 等）**均未实现**。

## 新增工具的步骤

1. 在 `src/pet_hospital_mcp/tools/` 新增一个模块，例如 `create_pet.py`：

   ```python
   from mcp.server.mcpserver import Context, MCPServer
   from ..errors import ...
   from ..rest_client import RestClient

   TOOL_NAME = "create_pet"

   class CreatePetInput(BaseModel):
       model_config = ConfigDict(extra="forbid", strict=True)
       ...

   def register(server: MCPServer, client: RestClient) -> None:
       async def create_pet(ctx: Context) -> CallToolResult:
           ...
       server.add_tool(create_pet, name=TOOL_NAME, ...)
       server._tool_manager._tools[TOOL_NAME].parameters = CreatePetInput.model_json_schema()
   ```

2. 在 `src/pet_hospital_mcp/tools/__init__.py` 中导入并导出该模块。

3. 在 `src/pet_hospital_mcp/server.py` 的 `create_server()` 中调用
   `xxx.register(server, client)` 完成注册。

## 必须复用/遵循的约定

- **REST 客户端**：复用 `rest_client.RestClient`（自带超时与有限重试）；
  在它上面新增方法（如 `create_pet`、`get_pet`），不要另建 HTTP 客户端。
- **错误**：复用 `errors` 中的 `ErrorCode`、`BackendError`、`build_error`；
  所有失败都返回统一结构 `{"error": {"code", "message", "details"}}`，
  并标记 `is_error=True`。
- **日志**：复用 `logging_config`；每条工具调用记录
  `timestamp / tool_name / params / status / duration_ms`，
  且 `ownerPhone / ownerAddr / chipNo`（含 snake_case）会被自动脱敏。
- **严格输入校验**：新工具的输入模型同样使用
  `ConfigDict(extra="forbid", strict=True)` + `model_validator`，
  并在工具函数体内通过 `ctx._input_params.arguments` 读取**原始**参数后严格校验，
  避免 SDK 的宽松解析；随后用 `server._tool_manager._tools[name].parameters = ...`
  覆盖输入 JSON Schema。
- **Pydantic 输出模型**：定义成功/错误输出模型，成功输出必须对应 Go 响应的 `data`，
  并兼容 `records` / `charges` 为 `null` 或数组。

## 禁止事项

- 不要使用或导入 `mcp.server.fastmcp.FastMCP`。
- 不要实现旧协议 `initialize`、`Mcp-Session-Id`、会话存储/过期、`max_sessions`
  或有状态 SSE 恢复。
- 不要把 httpx / Pydantic / SDK / Python 堆栈暴露给 MCP 客户端。
- 不要新增适配器私有业务参数；工具参数必须严格对应 Go REST API 参数。
- 不要修改 Go 服务本身；MCP 服务只能通过 HTTP 调用它。
