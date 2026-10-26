# -*- coding: utf-8 -*-
"""注册表：工具的收集、查找、导出。

它的存在让"新增一个工具"退化成一步：写好函数 + 装饰器。
注册表自己会把它算进 schema、算进分发，不需要在别处登记。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from .base import Tool, ToolContext, ToolResult


class ToolRegistry:
    def __init__(self, tools: Optional[Iterable[Tool]] = None) -> None:
        self._tools: Dict[str, Tool] = {}
        for item in tools or []:
            self.register(item)

    # ---------------------------------------------------------------- 管理

    def register(self, item: Tool) -> Tool:
        if item.name in self._tools:
            raise ValueError("工具重名：%s" % item.name)
        self._tools[item.name] = item
        return item

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    # ---------------------------------------------------------------- 对外

    def schemas(self) -> List[Dict[str, Any]]:
        """给模型看的工具清单。空注册表返回 []，此时请求里不该带 tools 字段。"""
        return [t.schema() for t in self._tools.values()]

    def invoke(self, name: str, arguments: str, ctx: ToolContext) -> ToolResult:
        """按名字执行。arguments 是模型给的 JSON 字符串。

        参数解析失败也返回 ToolResult 而不是抛异常——模型偶尔会拼出残缺 JSON，
        这时候正确的做法是告诉它"参数不对，重来"，而不是让程序崩掉。
        """
        target = self.get(name)
        if target is None:
            return ToolResult("没有这个工具：%s。可用工具：%s"
                              % (name, "、".join(self.names())), ok=False)

        args = self._parse_arguments(arguments)
        if args is None:
            return ToolResult(
                "参数不是合法的 JSON 对象：%s。请重新生成，例如 {\"path\": \"...\"}"
                % (arguments or "（空）"), ok=False)

        return target.invoke(args, ctx)

    @staticmethod
    def _parse_arguments(arguments: str) -> Optional[Dict[str, Any]]:
        text = (arguments or "").strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(value, dict):
            return value
        return None


__all__ = ["ToolRegistry"]
