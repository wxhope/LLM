# LLM 工作区

本仓库统一管理 `E:\LLM` 下的三个项目。它们都围绕**本机 AnythingLLM** 展开。

---

## 项目一览

| 目录 | 作用 | 技术 | 端口 |
| --- | --- | --- | --- |
| `PetHospitalMcp` | 把 Go 宠物医院 REST API 包装成 MCP 服务（参考/母版项目） | Python + `mcp==2.0.0` | 8000 |
| `AnythingLLMCP` | 把本机 AnythingLLM 工作区暴露为 MCP 工具 | Python + `mcp==2.0.0` | 8765 |
| `AnythingLLMServer` | 网页工具：上传文件到 AnythingLLM 并做向量化 | 单文件 HTML + `serve.bat` | 8080 |

---

## PetHospitalMcp

- **上游**：Go 写的宠物医院 REST API（`http://127.0.0.1:8080`），必须先启动
- **工具**：`list_pets`（唯一）
- **协议**：MCP `2026-07-28`，无状态 Streamable HTTP，**不使用 stdio**
- **SDK 用法**：`mcp==2.0.0` 的 `MCPServer`，**不是** `FastMCP`
- **结构最完整**：含 `errors.py`、`logging_config.py`、`tools/` 子包、`tests/`（13 个测试）
- 是另外两个项目照抄的**母版**

启动：

```bash
cd PetHospitalMcp
.venv\Scripts\activate
python -m pet_hospital_mcp
```

---

## AnythingLLMCP

- **上游**：本机 AnythingLLM 桌面版（`http://localhost:3001`）
- **工具**：`ask_workspace(question)` → `{ answer, sources, workspace }`
- **协议**：同 `2026-07-28` 无状态 Streamable HTTP

三个关键设计决策：

1. **`mode = "query"`** —— 只依据工作区已入库的文档回答，不让模型掺通用知识
2. **每次调用生成随机 `sessionId`（uuid4）** —— 否则所有提问落进同一线程，
   模型会把自己上一轮的回答当上下文，一次错答污染后续全部提问
3. **工作区自动解析** —— 配了 `ANYTHINGLLM_WORKSPACE` 就用它；
   没配且恰好只有 1 个工作区就自动用；有多个或零个则明确报错

> **当前阻塞**：`.env` 里的 API Key 无效（实测返回 `403 No valid api key found`）。
> 需在 AnythingLLM 桌面版 → `Settings` → `Tools` → `Developer API` 重新生成。

启动：

```bash
cd AnythingLLMCP
.venv\Scripts\activate
python -m anythingllm_mcp
```

---

## AnythingLLMServer

一个单文件网页，用来往 AnythingLLM 上传文档并完成向量化。

**用法**：双击 `serve.bat`（自动起服务 + 开浏览器）。

调用三步接口：

1. `GET  /api/v1/workspaces` → 取第一个工作区的 slug
2. `POST /api/v1/document/upload`（multipart，字段名 `file`）
3. `POST /api/v1/workspace/{slug}/update-embeddings`

**第 2、3 步必须成对** —— 只上传不调 `update-embeddings`，文档不会进工作区。

**两个已踩过的坑**：

- API 地址**必须写 `127.0.0.1` 而不是 `localhost`**。
  预览面板注入的 CSP 白名单里只有 `127.0.0.1`，用 `localhost` 会被直接拦掉。
- 不能直接双击 `index.html`（`file://` 下跨域被拦），必须走本地 HTTP 服务。

---

## 密钥管理

`AnythingLLMCP/.env` 存有 AnythingLLM 的 Developer API Key，**已被 `.gitignore` 拦截，不会进仓库**。

新建项目时，从 `.env.example` 复制一份改名 `.env` 再填自己的值：

```bash
cp AnythingLLMCP/.env.example AnythingLLMCP/.env
```

**提交前请确认 `git status` 输出里没有 `.env`。**
