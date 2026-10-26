# -*- coding: utf-8 -*-
"""工具的契约层。

一个工具由三部分组成：
  - 元信息：名字、描述、参数 Schema —— 这三样原样发给模型
  - 执行体：handler(args, ctx) -> ToolResult
  - 危险等级：SAFE / DESTRUCTIVE，决定要不要先问用户

为什么用装饰器：
  旧版是"一个巨大的 TOOLS 列表 + 一个 if/elif 大分支 + 一个 FILE_TOOLS 字典"，
  三处都要同步改，漏一处就静默失效。现在声明和执行贴在一起，
  注册表自动收集，加工具不会漏。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class DangerLevel(str, enum.Enum):
    SAFE = "safe"                 # 直接执行
    DESTRUCTIVE = "destructive"   # 不可逆，执行前必须问


@dataclass
class ToolContext:
    """执行工具时能给的外部依赖。

    工具本身不持有配置（否则没法复用、也没法测试）。
    真正需要时（比如读文件上限、搜索 key）由调用方塞进来。
    """

    max_read_chars: int = 20000
    confirm: Optional[Callable[[str, str], bool]] = None

    # ---- 联网搜索用的凭据。空字符串 = 没配，工具会如实告诉模型"没配"，
    # 而不是抛异常或去环境变量里乱翻。
    search_api_key: str = ""
    search_model: str = ""
    search_timeout: float = 30.0
    config_path: str = ""        # 只用于错误提示里指出该改哪个文件

    # ---- 抓网页：超时和单页字符上限（上限决定一次抓取吃掉多少上下文）
    fetch_timeout: float = 30.0
    fetch_max_chars: int = 12000

    # ---- 跑本地命令：超时和 stdout/stderr 各自的上限
    cli_timeout: float = 30.0
    cli_max_chars: int = 4000

    def ask(self, title: str, message: str) -> bool:
        """问用户。没有确认口就返回 False —— 宁可不动手，也不擅自删。"""
        if self.confirm is None:
            return False
        return bool(self.confirm(title, message))


@dataclass
class ToolResult:
    """工具返回值。

    content 会被原样塞进 role=tool 的 content 发给模型，
    所以它既是"给模型的观察"，也是"给真人看的说明"，要写得人机都能读。
    ok=False 用于 UI 标红；模型那边不区分成功失败，照样看文本。
    """

    content: str
    ok: bool = True

    def __str__(self) -> str:      # 方便日志
        return self.content


@dataclass
class Tool:
    """一个工具的完整描述。"""

    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any], ToolContext], ToolResult]
    danger: DangerLevel = DangerLevel.SAFE
    tags: List[str] = field(default_factory=list)

    def schema(self) -> Dict[str, Any]:
        """转成 OpenAI tools 数组里的一项。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def invoke(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        """执行。任何异常都兜成 ToolResult，绝不让工具炸掉整轮对话。"""
        try:
            result = self.handler(args, ctx)
        except Exception as e:  # noqa: BLE001 —— 工具出错应当降级为文本反馈
            return ToolResult("工具 %s 执行出错：%s" % (self.name, e), ok=False)
        if isinstance(result, ToolResult):
            return result
        # 允许 handler 直接返回字符串，省去模板代码
        return ToolResult(str(result))


def tool(name: str, description: str, parameters: Optional[Dict[str, Any]] = None,
         danger: DangerLevel = DangerLevel.SAFE, tags: Optional[List[str]] = None
         ) -> Callable[[Callable], Tool]:
    """把普通函数变成 Tool。

    用法：
        @tool("getCwd", "获取当前工作目录", empty_params(), tags=["fs"])
        def get_cwd(args, ctx) -> str:
            return os.getcwd()

    不指定 parameters 时用"无参"Schema，免得每个工具都手写一遍空对象。
    """
    def decorate(fn: Callable[[Dict[str, Any], ToolContext], Any]) -> Tool:
        return Tool(
            name=name,
            description=description,
            parameters=parameters if parameters is not None else empty_params(),
            handler=fn,
            danger=danger,
            tags=list(tags or []),
        )
    return decorate


def empty_params() -> Dict[str, Any]:
    return {"type": "object", "properties": {}, "required": []}


def params(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    """构造参数 Schema 的小工具，减少手写重复。"""
    return {
        "type": "object",
        "properties": properties,
        "required": list(required or []),
    }


def string(desc: str) -> Dict[str, str]:
    return {"type": "string", "description": desc}


def integer(desc: str) -> Dict[str, str]:
    return {"type": "integer", "description": desc}


def enumeration(desc: str, values: List[str]) -> Dict[str, Any]:
    return {"type": "string", "enum": list(values), "description": desc}


__all__ = [
    "DangerLevel",
    "ToolContext",
    "ToolResult",
    "Tool",
    "tool",
    "empty_params",
    "params",
    "string",
    "integer",
    "enumeration",
]
