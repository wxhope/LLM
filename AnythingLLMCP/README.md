# AnythingLLMCP

一个最小的 Python MCP Server：把**本机 AnythingLLM 的工作区**作为一个工具暴露给 AI 客户端。

- **协议**：MCP `2026-07-28`（无状态核心，无 `initialize` 握手、无 `Mcp-Session-Id`）
- **传输**：Streamable HTTP（**不是** stdio），`stateless_http=True` + `json_response=True`
- **工具**：只有一个 —— `ask_workspace(question)`，返回基于工作区文档的回答与出处

---

## 1. 前置条件

1. AnythingLLM 正在运行，且开启 Developer API（`http://localhost:3001`）。
2. 在 AnythingLLM 里生成 API Key：`Settings` → `Tools` → `Developer API`。
3. 把 Key 填进本目录的 `.env`（可从 `.env.example` 复制）。

## 2. 安装

```bash
cd E:/LLM/AnythingLLMCP
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e .
```

## 3. 运行

```bash
.venv/Scripts/python.exe -m anythingllm_mcp
```

默认监听 `http://127.0.0.1:8765/mcp`，健康检查在 `/health`。

## 4. 配置项（`.env`）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ANYTHINGLLM_BASE_URL` | `http://localhost:3001` | 本机 AnythingLLM 地址 |
| `ANYTHINGLLM_API_KEY` | *(必填)* | Developer API Key |
| `ANYTHINGLLM_WORKSPACE` | *(空)* | 工作区 slug；**留空则自动使用唯一的工作区** |
| `ANYTHINGLLM_MODE` | `query` | `query` 只依据工作区文档回答；`chat` 允许掺入模型通用知识 |
| `MCP_HOST` | `127.0.0.1` | 监听地址 |
| `MCP_PORT` | `8765` | 监听端口 |
| `MCP_PATH` | `/mcp` | Streamable HTTP 端点路径 |
| `REQUEST_TIMEOUT` | `120` | 等待 AnythingLLM 回答的秒数 |

> 工作区**多于一个**时服务会拒绝启动调用并列出所有 slug，此时必须显式设置
> `ANYTHINGLLM_WORKSPACE`。

## 5. 工具契约

```
ask_workspace(question: string) -> {
  workspace: string,
  answer: string,
  sources: string[]
}
```

内部调用 `POST /api/v1/workspace/{slug}/chat`，`mode` 取 `ANYTHINGLLM_MODE`。
**每次调用生成随机 `sessionId`** —— 复用同一个 sessionId 会让所有提问堆进同一线程，
模型会把自己上一轮的回答读成上下文，一次错答会污染之后的所有提问。

## 6. 验收（按 2026-07-28 报文直接调用）

列表工具：

```bash
curl -s http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/list' \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

调用工具：

```bash
curl -s http://127.0.0.1:8765/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/call' \
  -H 'Mcp-Name: ask_workspace' \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ask_workspace","arguments":{"question":"这个工作区里有什么"},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

**通过标准**：没有 `initialize` 握手、没有 `Mcp-Session-Id` 也能正常返回；
响应头**不含** `Mcp-Session-Id`。

## 7. 排错

| 现象 | 原因 |
| --- | --- |
| 工具返回 `AnythingLLM rejected the API key (HTTP 403)` | `.env` 里的 Key 无效，去 Developer API 重新生成 |
| 工具返回 `Cannot reach AnythingLLM ...` | AnythingLLM 没启动 |
| 工具返回 `AnythingLLM has N workspaces ...` | 有多个工作区，需设置 `ANYTHINGLLM_WORKSPACE` |
| 回答是 `There is no relevant information in this workspace...` | 工作区里没有已成功入库的文档，或与问题无关 |

---

*AI 只是镜子，落笔永远是你自己。*
