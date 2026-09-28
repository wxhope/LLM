# 🐾 Pet Hospital MCP 服务

一个独立的 **MCP（Model Context Protocol）服务**，把现有 Go 宠物医院 REST API
（`GET /api/v1/pets`）的能力暴露给 AI Agent。

- 语言 / 运行时：Python 3.11+
- 官方 SDK：`mcp==2.0.0`（使用 SDK 2.x 的 **`MCPServer`**，**未**使用 `FastMCP`）
- MCP 协议版本：**`2026-07-28`**（无状态 Streamable HTTP 单次交换模型）
- 无状态：不实现旧协议的 `initialize`、`Mcp-Session-Id`、会话存储、会话过期、
  `max_sessions` 或有状态 SSE 恢复机制
- 当前阶段只实现 **一个** 工具：`list_pets`

---

## 目录结构

```text
pet_hospital_mcp/
├── pyproject.toml
├── README.md
├── UPGRADE_PROMPT.md
├── src/
│   └── pet_hospital_mcp/
│       ├── __init__.py
│       ├── __main__.py        # 启动入口
│       ├── config.py          # 环境变量配置
│       ├── server.py          # MCPServer 装配 + /health
│       ├── rest_client.py     # 带超时与重试的 httpx 后端客户端
│       ├── errors.py          # 统一结构化错误
│       ├── logging_config.py  # JSON 日志 + 敏感字段脱敏
│       └── tools/
│           ├── __init__.py
│           └── list_pets.py   # 唯一的工具
└── tests/                     # pytest 测试（不访问真实 Go 服务）
```

---

## 前置条件

**必须先启动 Go 宠物医院 REST API**，因为 MCP 服务只是它的 HTTP 适配层：

```bash
cd pet-hospital-windows-amd64
pethospital.exe          # Windows（默认监听 http://127.0.0.1:8080）
```

验证后端可用：

```bash
curl http://127.0.0.1:8080/health
```

---

## 安装与启动

```bash
cd pet_hospital_mcp

# 1) 创建并激活虚拟环境
python -m venv .venv
.venv\Scripts\activate        # Windows（macOS/Linux: source .venv/bin/activate）

# 2) 安装本项目及其依赖（mcp==2.0.0、httpx、pydantic）
pip install -e .

# 3) 启动 MCP 服务
python -m pet_hospital_mcp
```

启动后 MCP 服务默认监听：

- MCP 端点：<http://127.0.0.1:8000/mcp>
- 健康检查：<http://127.0.0.1:8000/health>

> 若不想安装，也可直接运行：`PYTHONPATH=src python -m pet_hospital_mcp`
> （依赖已在当前虚拟环境中安装）。

---

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PET_HOSPITAL_BASE_URL` | `http://127.0.0.1:8080` | 上游 Go REST API 基地址 |
| `MCP_HOST` | `127.0.0.1` | MCP 服务监听地址（默认只监听本机） |
| `MCP_PORT` | `8000` | MCP 服务监听端口 |
| `MCP_PATH` | `/mcp` | Streamable HTTP 端点路径 |
| `PET_HOSPITAL_TIMEOUT` | `10` | 上游请求超时（秒） |
| `PET_HOSPITAL_MAX_RETRIES` | `2` | 上游瞬时错误（超时/连接失败）重试次数 |

示例（换端口 + 换后端地址）：

```bash
MCP_PORT=9000 PET_HOSPITAL_BASE_URL=http://127.0.0.1:8080 python -m pet_hospital_mcp
```

---

## `list_pets` 工具

严格适配 `GET /api/v1/pets`。支持且仅支持以下查询参数：

```text
q  name  ownerName  ownerPhone  species  doctor  disease  status
min  max  sortBy  order  page  pageSize
```

输入校验（严格）：

- `species` / `status` / `sortBy` / `order` 使用真实后端允许值
- `page >= 1`
- `1 <= pageSize <= 500`
- `min` / `max` 非负，且 `min <= max`
- 拒绝未知字段、`NaN`、`Infinity` 以及类型不正确的输入

成功输出对应 Go 响应中的 `data`：

```text
items  total  page  pageSize  totalPages  totalCost
```

并兼容 Go 返回中 `records` / `charges` 为 `null` 或数组的两种真实 JSON 表现。

---

## 调用示例

### `server/discover`（现代发现，替代旧 `initialize`）

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H 'mcp-protocol-version: 2026-07-28' \
  -H 'mcp-method: server/discover' \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"server/discover","params":{"_meta":{
        "io.modelcontextprotocol/protocolVersion":"2026-07-28",
        "io.modelcontextprotocol/clientCapabilities":{}}}}'
```

### `tools/call` 调用 `list_pets`

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H 'mcp-protocol-version: 2026-07-28' \
  -H 'mcp-method: tools/call' \
  -H 'mcp-name: list_pets' \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{
        "name":"list_pets",
        "arguments":{"species":"犬","min":1000,"sortBy":"totalCost","order":"desc","page":1,"pageSize":10},
        "_meta":{
          "io.modelcontextprotocol/protocolVersion":"2026-07-28",
          "io.modelcontextprotocol/clientCapabilities":{}}}}'
```

### `/health`

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","service":"pet-hospital-mcp","version":"0.1.0"}
```

---

## 错误处理

所有失败都返回统一结构化错误，并正确标记工具失败（`isError: true`）：

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "可读错误信息",
    "details": {}
  }
}
```

错误码：`VALIDATION_ERROR`、`BACKEND_TIMEOUT`、`BACKEND_UNAVAILABLE`、
`BACKEND_API_ERROR`、`BACKEND_INVALID_RESPONSE`、`INTERNAL_ERROR`。

不会把 httpx / Pydantic / SDK / Python 堆栈原样暴露给 MCP 客户端。

---

## 使用 MCP Inspector 或 SDK 2.x 客户端验证

### 方式一：MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

在 Inspector 的 Streamable HTTP 连接中填入：

```text
http://127.0.0.1:8000/mcp
```

（协议版本选择 `2026-07-28`。）连接后即可看到并调用 `list_pets`。

### 方式二：Python SDK 2.x 客户端

```python
import anyio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

async def main():
    async with streamable_http_client("http://127.0.0.1:8000/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            tools = await session.list_tools()
            print([t.name for t in tools])  # ['list_pets']
            result = await session.call_tool("list_pets", {"species": "犬", "page": 1, "pageSize": 5})
            print(result.structured_content)

anyio.run(main)
```

---

## 单元测试

测试使用 `pytest` + `pytest-asyncio` + `httpx.MockTransport`，**不会访问真实 Go 服务**：

```bash
cd pet_hospital_mcp
pytest -q
```

预期结果：全部通过（`13 passed`）。

覆盖场景：

1. 正常调用：请求路径与全部过滤/排序/分页参数正确转发
2. 输入参数校验失败（未知字段、非法枚举、`min > max`、`NaN`/`Infinity` 等）
3. Go REST API 返回 4xx / 5xx
4. 超时与连接异常
5. 后端返回非法 JSON 或不符合数据模型
6. MCP 工具注册、工具名与 JSON Schema
7. SDK 2.x 无状态连接流程（`server/discover` + `tools/call`，无 `initialize`、无 `Mcp-Session-Id`）
