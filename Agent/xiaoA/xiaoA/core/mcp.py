# -*- coding: utf-8 -*-
"""MCP Client：让 xiaoA 挂载外部 MCP Server 提供的工具。

整个链路分四步，对应题目要求的四条：

  1. 读配置      项目根（程序启动时的工作目录，同 skills.STARTUP_CWD）下的
                .mcp.json，拿到每个 Server 的名字和 URL。
  2. 连接+发现   按 MCP Python SDK 的标准接口（streamable_http_client +
                ClientSession）建立持久连接，list_tools() 拿工具清单。
  3. 注册        把远端工具包成本地的 Tool 对象塞进现有注册表——模型看到的
                就是普通 Function Calling 工具，根本不知道背后是 MCP。
                名字冲突时给工具名加「服务器名__」前缀。
  4. 调用转发    模型发起 tool_call 后，按「暴露名 → (服务器, 原名)」映射找到
                对应 Server，转发参数，把返回的文本/结构化数据/错误排成文本。

为什么需要一个后台线程：
  MCP Python SDK 是 asyncio 的，而 xiaoA 的 core 是纯同步的（工具 handler
  是普通函数）。不把两个世界硬扭在一起，而是让 MCP 事件循环在一个专属
  后台线程里常驻，同步侧用 run_coroutine_threadsafe 提交协程并等待结果。
  这样 handler 保持同步签名，注册表一行都不用改。

失败哲学（和 skills.py 一致）：
  连不上的 Server 收进 problems，一行说明哪里坏了；
  其余 Server、其余工具、整个对话流程完全不受影响。
"""

from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from .skills import STARTUP_CWD
from .tools.base import DangerLevel, Tool, ToolContext, ToolResult
from .tools.builtin.cli_ops import truncate_middle

# --------------------------------------------------------------------- 常量

MCP_CONFIG_FILENAME = ".mcp.json"

DEFAULT_CONNECT_TIMEOUT = 10.0    # 建立连接 + initialize 的等待秒数
DEFAULT_CALL_TIMEOUT = 60.0       # 单次 call_tool 的等待秒数
DEFAULT_MAX_CHARS = 8000          # 工具返回文本的上限（决定吃掉多少上下文）

MAX_DESC_CHARS = 500              # 注册时给模型看的描述上限，超了截断

# OpenAI Function Calling 对工具名的硬约束：字母/数字/下划线/连字符。
# 中文服务器名、带点的工具名都过不去，必须先消毒。
_NAME_BAD_CHARS = re.compile(r"[^0-9A-Za-z_-]")


def sanitize_name(text: str, fallback: str) -> str:
    """把任意名字消毒成 Function Calling 能接受的形态。

    中文、点号、空格 → 下划线；开头是数字也补下划线（部分实现要求
    名字以字母/下划线开头）；全坏掉了就用 fallback 兜底。
    """
    cleaned = _NAME_BAD_CHARS.sub("_", (text or "").strip())
    if not cleaned or set(cleaned) == {"_"}:
        cleaned = fallback
    if cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned[:64]


# --------------------------------------------------------------------- 配置


@dataclass(frozen=True)
class McpServerConfig:
    """一个 MCP Server 的连接信息（.mcp.json 里的一项）。"""

    name: str          # 服务器名：来自 mcpServers 的键，也是工具前缀的来源
    url: str           # Streamable HTTP 端点，例如 http://127.0.0.1:8090/mcp
    headers: Dict[str, str] = field(default_factory=dict)


def parse_mcp_config(path: str) -> Tuple[List[McpServerConfig], List[str]]:
    """解析 .mcp.json。返回 (服务器列表, 问题列表)。

    纯函数、不碰网络——离线可测。格式错误只进 problems，不抛异常：
    一个坏掉的配置项不该拖死其他配置项，更不该炸掉整个程序。
    """
    servers: List[McpServerConfig] = []
    problems: List[str] = []

    if not os.path.exists(path):
        return servers, problems          # 没有 .mcp.json 是常态，不是错误

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except (OSError, UnicodeDecodeError) as e:
        return servers, ["MCP：读不了 %s（%s）" % (path, e)]
    except json.JSONDecodeError as e:
        return servers, ["MCP：%s 不是合法的 JSON（%s）" % (path, e)]

    if not isinstance(raw, dict):
        return servers, ["MCP：%s 的顶层必须是对象 {\"mcpServers\": {...}}" % path]

    section = raw.get("mcpServers", {})
    if not isinstance(section, dict):
        problems.append("MCP：mcpServers 必须是对象，收到 %s" % type(section).__name__)
        section = {}
    if not section:
        problems.append("MCP：%s 里 mcpServers 是空的，没有要连接的服务器" % path)

    seen: Dict[str, int] = {}
    for name, item in section.items():
        if not isinstance(item, dict):
            problems.append("MCP：服务器「%s」的配置必须是对象，收到 %s"
                            % (name, type(item).__name__))
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            # 教学阶段只接 Streamable HTTP；stdio 型（command 启动子进程）
            # 以后要支持的话在这里加分支即可。
            problems.append(
                "MCP：服务器「%s」没有 url 字段。本阶段只支持 Streamable HTTP，"
                "写法：{\"url\": \"http://127.0.0.1:8090/mcp\"}" % name)
            continue
        if not re.match(r"^https?://", url, re.IGNORECASE):
            problems.append("MCP：服务器「%s」的 url 必须以 http:// 或 https:// 开头，"
                            "收到「%s」" % (name, url))
            continue

        clean = name.strip() or "server"
        seen[clean] = seen.get(clean, 0) + 1
        if seen[clean] > 1:
            problems.append("MCP：服务器名「%s」重复，只保留第一个" % clean)
            continue

        headers_raw = item.get("headers")
        headers: Dict[str, str] = {}
        if isinstance(headers_raw, dict):
            headers = {str(k): str(v) for k, v in headers_raw.items()}
        elif headers_raw is not None:
            problems.append("MCP：服务器「%s」的 headers 必须是对象，已忽略" % name)

        servers.append(McpServerConfig(name=clean, url=url, headers=headers))

    return servers, problems


# --------------------------------------------------------------------- 连接

# SDK 是可选依赖：没装 mcp 包时，其他工具照常工作，这里如实报告即可。
# 另外各版本 SDK 的传输层签名不一样（2.3+ 用 http_client 传自定义客户端、
# 产出 2 元组；旧版用 headers/timeout 形参、产出 3 元组），两种都接住。
try:                                    # pragma: no cover - 视环境而定
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    try:
        from mcp.shared._httpx_utils import create_mcp_http_client
    except Exception:                   # noqa: BLE001 - 旧版 SDK 没有这个工具函数
        create_mcp_http_client = None   # type: ignore[assignment]
    try:
        import httpx2 as _httpx
    except Exception:                   # noqa: BLE001
        _httpx = None                   # type: ignore[assignment]

    SDK_ERROR = ""                      # 空串 = SDK 可用
except Exception as _e:                 # noqa: BLE001 - ImportError 之外的怪环境也兜住
    ClientSession = None                # type: ignore[assignment]
    streamable_http_client = None       # type: ignore[assignment]
    create_mcp_http_client = None       # type: ignore[assignment]
    _httpx = None                       # type: ignore[assignment]
    SDK_ERROR = "%s: %s" % (type(_e).__name__, _e)


class _LoopThread(threading.Thread):
    """专属事件循环线程。

    daemon=True：主进程退出时它跟着死，连接不必优雅关闭——
    TCP 断了服务端自己能感知，比在退出路径上挂一堆清理逻辑更可靠。
    """

    def __init__(self) -> None:
        super().__init__(name="xiaoA-mcp", daemon=True)
        self.loop = asyncio.new_event_loop()

    def run(self) -> None:
        try:
            self.loop.run_forever()
        finally:
            self.loop.close()

    def start_once(self) -> None:
        if not self.is_alive():
            self.start()


class McpConnection:
    """一台 MCP Server 的持久连接。

    用法：
        conn = McpConnection(cfg, call_timeout=60)
        conn.connect()              # 阻塞到握手完成或超时；失败不抛，看 conn.error
        tools = conn.list_tools()   # [(name, description, inputSchema), ...]
        text, ok = conn.call_tool(name, args)
        conn.close()
    """

    def __init__(self, config: McpServerConfig,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 call_timeout: float = DEFAULT_CALL_TIMEOUT) -> None:
        self.config = config
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout
        self.error: str = ""             # 非空 = 连接失败的原因，给用户和模型看
        self._loop_thread: Optional[_LoopThread] = None
        self._session: Any = None
        self._ready = threading.Event()
        self._stop = asyncio.Event()
        self._task: Any = None
        self._closed = False

    # ------------------------------------------------------------ 生命周期

    def connect(self) -> bool:
        """建立连接并完成 initialize 握手。成功 True；失败把原因写进 self.error。"""
        if SDK_ERROR:
            self.error = ("MCP：本机没装 mcp 库（%s）。先在 xiaoA 的虚拟环境里执行："
                          "pip install mcp" % SDK_ERROR)
            return False

        self._stop = asyncio.Event()
        self._loop_thread = _LoopThread()
        self._loop_thread.start_once()

        self._task = asyncio.run_coroutine_threadsafe(
            self._main(), self._loop_thread.loop)
        if not self._ready.wait(self.connect_timeout):
            # 握手没完成。任务还挂在后台，先请求它退出，再给模型一句人话。
            self.close()
            if not self.error:
                self.error = ("MCP：连接 %s 超时（%g 秒）。确认服务已启动、"
                              "地址端口没写错，且没有代理拦截 127.0.0.1。"
                              % (self.config.url, self.connect_timeout))
            return False
        return not self.error

    async def _main(self) -> None:
        """连接的主协程：握手 → 挂起等退出信号 → 退出时关掉两个 async with。"""
        client = self._make_http_client()
        try:
            if client is not None:
                transport_cm = streamable_http_client(
                    self.config.url, http_client=client)
            else:       # 旧版 SDK：没有 create_mcp_http_client，走形参
                transport_cm = streamable_http_client(
                    self.config.url,
                    headers=self.config.headers or None,
                    timeout=timedelta(seconds=self.connect_timeout),
                )
            async with transport_cm as streams:
                streams = tuple(streams)
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except Exception as e:            # noqa: BLE001 - 一切连接层异常都转成文本
            self.error = _humanize_error(e, self.config.url)
        finally:
            self._ready.set()             # 失败也要放行等待方，别让它干等超时

    def _make_http_client(self) -> Any:
        """2.3+ 的 SDK 要求自带 httpx 客户端才能配 headers 和超时。"""
        if create_mcp_http_client is None:
            return None
        timeout = None
        if _httpx is not None:
            timeout = _httpx.Timeout(self.connect_timeout)
        return create_mcp_http_client(
            headers=self.config.headers or None, timeout=timeout)

    def close(self) -> None:
        """请求断开。重复调用安全；连接从未建立时也安全。"""
        if self._closed:
            return
        self._closed = True
        try:
            self._loop_thread.loop.call_soon_threadsafe(self._stop.set)
        except RuntimeError:
            pass                          # 循环已经停了，无需再停

    # ------------------------------------------------------------ 远端调用

    def list_tools(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """拉工具清单。返回 [(原名, 描述, inputSchema), ...]；失败抛 McpError。"""
        async def _list():
            result = await self._session.list_tools()
            pairs = []
            for t in result.tools:
                # SDK 各版本字段命名不一致：2.0 用 inputSchema，2.3 改成 input_schema
                schema = (getattr(t, "inputSchema", None)
                          or getattr(t, "input_schema", None) or {})
                pairs.append((t.name, t.description or "", schema))
            return pairs

        try:
            return self._submit(_list(), self.call_timeout)
        except Exception as e:            # noqa: BLE001
            raise McpError("列取工具失败：%s" % _humanize_error(e, self.config.url)) from e

    def call_tool(self, original_name: str, arguments: Dict[str, Any],
                  timeout: Optional[float] = None,
                  max_chars: int = DEFAULT_MAX_CHARS) -> Tuple[str, bool]:
        """调用远端工具。返回 (给模型看的文本, 是否成功)。绝不抛异常。"""
        async def _call():
            return await self._session.call_tool(original_name, arguments)

        try:
            raw = self._submit(_call(), timeout or self.call_timeout)
        except Exception as e:            # noqa: BLE001 - 网络断、超时、协议错全在这里
            return ("MCP 工具调用失败（服务器 %s）：%s"
                    % (self.config.name, _humanize_error(e, self.config.url))), False
        return format_call_result(raw, max_chars)

    def _submit(self, coro, timeout: float) -> Any:
        if self._session is None or self._loop_thread is None:
            raise McpError("连接未建立或已关闭")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop_thread.loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise McpError("等待 %g 秒没有响应" % timeout) from None


class McpError(Exception):
    """MCP 侧的可预期错误。消息可直接给模型看。"""


def _humanize_error(e: Exception, url: str) -> str:
    """把异常翻成人话。新手看到的应该是「哪里断了」，不是一串类名。"""
    # anyio 的 TaskGroup 会把真实异常包进 ExceptionGroup，拆开看里层
    subs = getattr(e, "exceptions", None)
    if subs:
        return _humanize_error(subs[0], url)
    text = str(e) or repr(e)
    lowered = text.lower()
    if isinstance(e, McpError):
        if url in text:
            return text
        return "%s（服务器 %s）。确认服务在跑、地址端口没写错。" % (text, url)
    # SDK 自己的 McpError（mcp.shared.exceptions）不在我们的类体系里，按名字认
    if type(e).__name__.lower() == "mcperror":
        return ("MCP 握手失败（服务器 %s）：%s。多半是服务没启动、路径不对"
                "（Streamable HTTP 通常是 http://主机:端口/mcp），"
                "或被代理拦截。" % (url, text))
    if "connectionrefused" in lowered or "winerror 10061" in lowered:
        return ("连接被拒绝（%s）。服务器大概率没启动——先把 MCP 服务跑起来，"
                "再重启 xiaoA。" % url)
    if "timeout" in lowered or "timed out" in lowered:
        return "连接/响应超时（%s）。确认服务在跑、端口没错。" % url
    if "404" in text:
        return ("服务器返回 404（%s）。路径多半不对：Streamable HTTP 的端点"
                "通常是 http://主机:端口/mcp。" % url)
    if "401" in text or "403" in text:
        return "服务器返回 %s（%s）。需要认证的话在 .mcp.json 的 headers 里补上。" % (text[:80], url)
    if "proxy" in lowered or "502" in text:
        return "疑似被代理拦截（%s）。本机服务请设置 NO_PROXY=127.0.0.1,localhost。" % url
    return "%s：%s" % (type(e).__name__, text[:200])


# --------------------------------------------------------------------- 结果排版


def format_call_result(raw: Any, max_chars: int = DEFAULT_MAX_CHARS) -> Tuple[str, bool]:
    """把 MCP 的 CallToolResult 排成给模型看的文本。

    三类内容都要接住：
      - content 里的 TextContent（主流，直接拼）
      - structuredContent（结构化数据，转成 JSON 附在后面）
      - isError=True（服务端报的业务错误——它是「正常返回的错误」，
        不是连接层的异常，必须以 ok=False 的文本形式交回模型）
    """
    ok = not bool(getattr(raw, "isError", False) or getattr(raw, "is_error", False))

    texts: List[str] = []
    for item in (getattr(raw, "content", None) or []):
        mime = getattr(item, "mimeType", "") or getattr(item, "mime_type", "") or ""
        text = getattr(item, "text", None)
        if text is not None:
            texts.append(str(text))
        elif mime.startswith("image/"):
            texts.append("[该工具返回了一张图片（%s），文本通道无法展示]" % mime)
        elif text is None and mime:
            texts.append("[该工具返回了 %s 类型的内容]" % mime)

    body = "\n".join(part for part in texts if part).strip()

    structured = getattr(raw, "structuredContent", None)
    if structured is None:
        structured = getattr(raw, "structured_content", None)
    if structured:
        try:
            text = json.dumps(structured, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(structured)
        body = (body + "\n【结构化数据】\n" + text).strip()

    if not body:
        body = "（工具执行完成，但没有返回内容）"

    body, removed = truncate_middle(body, max_chars)
    if removed:
        body += "\n（返回共 %d 字符，已截断省略 %d 字符）" % (removed + len(body), removed)
    return body, ok


# --------------------------------------------------------------------- 注册


@dataclass
class McpToolInfo:
    """一个远端工具注册后的完整映射：暴露名 → 哪台服务器上的哪个原名。"""

    exposed: str
    server: str
    original: str
    description: str
    schema: Dict[str, Any]


def plan_tool_names(
    servers_tools: List[Tuple[str, List[Tuple[str, str, Dict[str, Any]]]]],
    existing_names: List[str],
) -> Tuple[List[McpToolInfo], List[str]]:
    """决定每个远端工具对外暴露什么名字。纯函数，离线可测。

    规则：
      - 消毒后的原名在全集群唯一、也不和本地工具撞名 → 直接用原名；
      - 否则加「服务器名__」前缀（服务器名同样消毒）；
      - 前缀后还撞（极端情况：两个服务器消毒后同名且工具同名）→ 后者进 problems。
    """
    local = set(existing_names)
    # 先数一遍：每个消毒后的工具名出现了几次、分布在几台服务器
    counts: Dict[str, int] = {}
    for _, tools in servers_tools:
        for name, _, _ in tools:
            key = sanitize_name(name, "tool")
            counts[key] = counts.get(key, 0) + 1

    infos: List[McpToolInfo] = []
    problems: List[str] = []
    taken = set(local)

    for server_name, tools in servers_tools:
        srv = sanitize_name(server_name, "server")
        for name, desc, schema in tools:
            base = sanitize_name(name, "tool")
            exposed = base if counts.get(base, 0) == 1 and base not in local else "%s__%s" % (srv, base)
            if exposed in taken:
                problems.append("MCP：工具「%s」（服务器 %s）暴露名 %s 与已有工具冲突，未注册"
                                % (name, server_name, exposed))
                continue
            taken.add(exposed)
            infos.append(McpToolInfo(
                exposed=exposed,
                server=server_name,
                original=name,
                description=(desc or "").strip()[:MAX_DESC_CHARS],
                schema=_normalize_schema(schema),
            ))
    return infos, problems


def _normalize_schema(schema: Any) -> Dict[str, Any]:
    """MCP 的 inputSchema 本身就是 JSON Schema；兜底保证它是 object 形态。"""
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    if isinstance(schema, dict) and "properties" in schema:
        fixed = dict(schema)
        fixed["type"] = "object"
        return fixed
    # 没有 schema 或形态不对：声明成"无参数"，至少模型能不带参调用
    return {"type": "object", "properties": {}, "required": []}


def build_mcp_tools(connections: List["McpConnection"],
                    existing_names: List[str]) -> Tuple[List[Tool], List[McpToolInfo], List[str]]:
    """对每台已连接的 Server 执行 list_tools，包成本地 Tool 对象。"""
    servers_tools: List[Tuple[str, List[Tuple[str, str, Dict[str, Any]]]]] = []
    problems: List[str] = []

    for conn in connections:
        if conn.error:
            problems.append(conn.error)
            continue
        try:
            tools = conn.list_tools()
        except McpError as e:
            problems.append("MCP：服务器「%s」%s" % (conn.config.name, e))
            continue
        servers_tools.append((conn.config.name, tools))

    infos, name_problems = plan_tool_names(servers_tools, existing_names)
    problems.extend(name_problems)

    by_server: Dict[str, McpConnection] = {c.config.name: c for c in connections}
    tools: List[Tool] = []
    for info in infos:
        conn = by_server[info.server]
        tools.append(_wrap_tool(info, conn))
    return tools, infos, problems


def _wrap_tool(info: McpToolInfo, conn: "McpConnection") -> Tool:
    """把远端工具包成本地 Tool。

    闭包捕获 conn 和原名；模型给的参数 dict 原样转发给 MCP Server。
    handler 的同步签名、ToolResult 返回值，与本地工具完全一致——
    注册表和会话层根本不需要知道这个工具来自远端。
    """

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        text, ok = conn.call_tool(
            info.original,
            args,
            timeout=ctx.mcp_call_timeout or None,
            max_chars=ctx.mcp_max_chars or DEFAULT_MAX_CHARS,
        )
        return ToolResult(text, ok=ok)

    handler.__name__ = "mcp_" + info.exposed

    return Tool(
        name=info.exposed,
        description="（MCP·%s）%s" % (info.server, info.description or "远端工具，无描述"),
        parameters=info.schema,
        handler=handler,
        danger=DangerLevel.SAFE,
        tags=["mcp", info.server],
    )


# --------------------------------------------------------------------- 总装


class McpManager:
    """.mcp.json → 连接 → 工具清单 的总管。

    Session 通过它拿 tools 和 problems；程序退出时 atexit 自动收尾。
    """

    def __init__(self, config_path: str,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                 call_timeout: float = DEFAULT_CALL_TIMEOUT) -> None:
        self.config_path = config_path
        self.connect_timeout = connect_timeout
        self.call_timeout = call_timeout
        self.connections: List[McpConnection] = []
        self.tools: List[Tool] = []
        self.infos: List[McpToolInfo] = []
        self.problems: List[str] = []
        self.summary: str = ""

    @classmethod
    def load(cls, config_path: Optional[str] = None,
             connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
             call_timeout: float = DEFAULT_CALL_TIMEOUT) -> "McpManager":
        """读配置、逐台连接、拉工具清单。任何失败都只进 problems。"""
        path = config_path or os.path.join(STARTUP_CWD, MCP_CONFIG_FILENAME)
        mgr = cls(path, connect_timeout, call_timeout)

        servers, problems = parse_mcp_config(path)
        mgr.problems.extend(problems)
        if not servers:
            if problems:
                mgr.summary = "MCP：配置有问题，0 台连接"
            return mgr

        connections: List[McpConnection] = []
        for cfg in servers:
            conn = McpConnection(cfg, connect_timeout=connect_timeout,
                                 call_timeout=call_timeout)
            if not conn.connect():
                mgr.problems.append(conn.error or
                                    "MCP：服务器「%s」连接失败" % cfg.name)
                continue
            connections.append(conn)
        mgr.connections = connections

        if connections:
            tools, infos, more_problems = build_mcp_tools(
                connections, existing_names=[])
            mgr.tools, mgr.infos = tools, infos
            mgr.problems.extend(more_problems)
            names = "、".join(i.exposed for i in infos)
            mgr.summary = "MCP（%d 台）：已挂载工具 %d 个：%s" % (
                len(connections), len(tools), names or "（无）")
        else:
            mgr.summary = "MCP：0/%d 台服务器连接成功" % len(servers)
        return mgr

    def shutdown(self) -> None:
        for conn in self.connections:
            conn.close()


def load_mcp_manager(config_path: Optional[str] = None,
                     connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
                     call_timeout: float = DEFAULT_CALL_TIMEOUT) -> Optional[McpManager]:
    """给 Session 用的入口。没配 .mcp.json 时返回 None——零开销路径。"""
    path = config_path or os.path.join(STARTUP_CWD, MCP_CONFIG_FILENAME)
    if not os.path.exists(path):
        return None
    mgr = McpManager.load(path, connect_timeout, call_timeout)
    atexit.register(mgr.shutdown)
    return mgr


__all__ = [
    "MCP_CONFIG_FILENAME",
    "McpManager",
    "McpConnection",
    "McpServerConfig",
    "McpError",
    "McpToolInfo",
    "parse_mcp_config",
    "plan_tool_names",
    "sanitize_name",
    "format_call_result",
    "load_mcp_manager",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_CALL_TIMEOUT",
    "DEFAULT_MAX_CHARS",
]
