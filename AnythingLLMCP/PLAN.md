# AnythingLLMCP — MVP 开发计划

> 目标：一个 Python 写的 MCP Server，通过 **Streamable HTTP** 暴露 **1 个工具**，
> 让 AI 客户端能够对**本机 AnythingLLM 的某一个工作区**提问并拿到带出处的回答。
> 协议版本：**MCP 2026-07-28**（无状态核心）。**不使用 stdio。**

---

## 0. 前置阻塞项（未解决则不开发）

**你给的 API Key 无效。** 本机实测：

| 请求 | 结果 |
| --- | --- |
| `GET http://localhost:3001/api/ping` | `200 {"online":true}` ← 服务是活的 |
| `GET /api/v1/auth` + `Bearer 5P5F2PS-...` | `403 {"error":"No valid api key found."}` |
| `GET /api/v1/workspaces` + 同上 | `403 {"error":"No valid api key found."}` |

**处理方式：** AnythingLLM 桌面版 → 左下角 `Settings` → `Tools` → `Developer API`
→ 重新生成 / 复制 API Key，再贴给我。

其余环境已确认：桌面版安装（非 Docker）、端口 3001、`E:\LLM\AnythingLLMCP` 已存在且为空、
本机无 `uv`（改用 `venv` + `pip`）、Python 3.13.12。

---

## 1. MVP 边界

**只做一件事：** 一个工具 `ask_workspace` —— 给一个问题，返回该工作区的带出处回答。

**明确不做**（避免过度设计）：
多工作区路由、文档上传、会话历史管理、MCP 服务自身鉴权、CORS、Docker 打包、
测试套件、自定义错误码体系、独立日志模块、`tools/` 子包。

---

## 2. 技术选型（均已实测确认，非推测）

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 语言 | Python 3.13 | 本机可用 |
| MCP SDK | `mcp==2.0.0` | 实现 2026-07-28 无状态协议；与 `E:\LLM\PetHospitalMcp` 同版本，已跑通 |
| 传输 | `streamable-http` + `stateless_http=True` + `json_response=True` | 满足「不用 stdio」「HTTP 传输」 |
| 上游调用 | `httpx` | 异步，够用 |
| 依赖管理 | `venv` + `pip -e .` | 本机无 uv |

SDK 调用形态（直接照搬 `PetHospitalMcp` 已验证的写法）：
`from mcp.server.mcpserver import MCPServer` → `server.streamable_http_app(...)` / `server.run(...)`。

---

## 3. 目录结构（6 个文件，够了）

```
E:\LLM\AnythingLLMCP\
├── pyproject.toml
├── .env.example
├── README.md
└── src\anythingllm_mcp\
    ├── __init__.py
    ├── config.py      # Settings.from_env()，读环境变量
    ├── client.py      # AnythingLLMClient：resolve_workspace() + chat()
    ├── server.py      # create_server()：注册 ask_workspace + /health
    └── __main__.py    # 入口：装配并 run
```

对比参考项目 `PetHospitalMcp`，**砍掉** `errors.py`、`logging_config.py`、`tools/`、`tests/`
—— MVP 只有 1 个工具，不值得为它建包和错误码体系。

---

## 4. 接口契约

### 4.1 MCP 工具

```
ask_workspace(question: str) -> { answer: str, sources: [str], workspace: str }
```

- **只有一个入参** `question`。mode / 工作区都从配置来，不暴露给客户端。

### 4.2 上游 AnythingLLM 调用

```
POST {BASE}/api/v1/workspace/{slug}/chat
Authorization: Bearer {API_KEY}
Content-Type: application/json

{ "message": "<question>", "mode": "query", "sessionId": "<uuid4>" }
→ { "textResponse": "...", "sources": [ ... ], "id": "...", "error": null }
```

三个关键决策：

1. **`mode = "query"`**（默认，可用环境变量覆盖）—— 让回答只基于工作区已入库的文档，
   而不是模型的通用知识。这正是「从工作区提取信息」的语义，也避免模型自由发挥。
2. **`sessionId` 每次随机（uuid4）** —— 若不给，所有调用会落进工作区默认线程，
   模型会把自己上一轮的回答当成上下文，一次错答会污染后续所有提问。随机 ID = 每问独立。
3. **工作区自动解析** —— 启动时 `GET /api/v1/workspaces`：
   - 配了 `ANYTHINGLLM_WORKSPACE` → 用它
   - 没配且**恰好只有 1 个**工作区 → 用它（契合「唯一一个工作区」）
   - 没配且有多个 / 一个都没有 → 明确报错，列出可选 slug

---

## 5. 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ANYTHINGLLM_BASE_URL` | `http://localhost:3001` | 本机 AnythingLLM |
| `ANYTHINGLLM_API_KEY` | *(必填)* | Developer API Key |
| `ANYTHINGLLM_WORKSPACE` | *(空)* | 工作区 slug；空则自动取唯一工作区 |
| `ANYTHINGLLM_MODE` | `query` | `query` 或 `chat` |
| `MCP_HOST` | `127.0.0.1` | 仅本机监听 |
| `MCP_PORT` | `8765` | 避开 3001（AnythingLLM）与 8000（PetHospitalMcp） |
| `MCP_PATH` | `/mcp` | Streamable HTTP 端点 |

---

## 6. 实施步骤

| 步骤 | 内容 | 产出 |
| --- | --- | --- |
| S1 | 脚手架：`pyproject.toml`、建 venv、装 `mcp==2.0.0` + `httpx` | 可导入 mcp |
| S2 | `config.py` + `client.py`（含 403/超时/工作区解析的错误处理） | 命令行能拉到工作区问答 |
| S3 | `server.py` + `__main__.py`：`MCPServer` + `ask_workspace` + `/health` | 服务能起在 8765 |
| S4 | 验收：按 2026-07-28 报文直接 curl 调用 | 见下方验收标准 |

### S4 验收命令形态（无 initialize 握手）

```
POST http://127.0.0.1:8765/mcp
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: ask_workspace
→ 200，返回 result.content[]
```

---

## 7. 验收标准

1. 不发 `initialize`、不带 `Mcp-Session-Id`，直接 `tools/call` → **200**；
   响应头**不含** `Mcp-Session-Id`（证明是真无状态，而非退化成有状态）。
2. `tools/list` 能列出 `ask_workspace` 且带 JSON Schema。
3. 提问能返回 `textResponse` 内容 + 非空 `sources`。
4. API Key 错 / 工作区不存在 / AnythingLLM 未启动 → 返回**可读错误**，不是 500 裸栈。
5. `/health` 返回服务自述信息。

---

## 8. 风险与对策

| 风险 | 对策 |
| --- | --- |
| `mcp==2.0.0` 是否真按 2026-07-28 校验 `Mcp-Method` 等头 | 与 `PetHospitalMcp` 同版本且已验证；S4 用裸 curl 复查 |
| 工作区里没有已 embed 的文档 | `query` 模式会召回为空；S2 阶段先确认工作区里有文档 |
| AnythingLLM 回答慢（本地模型） | httpx 超时设 60s，不用 10s |
| 桌面版 AnythingLLM 未运行 | 启动前先探测 `/api/ping`，给出明确提示 |

---

## 9. 需要你确认的

1. **新的 API Key**（唯一的硬阻塞）。
2. 工作区 slug —— 不给我就自动取唯一那个。
