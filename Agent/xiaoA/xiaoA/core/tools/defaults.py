# -*- coding: utf-8 -*-
"""注册表工厂。

单独放一个文件，是为了让 core.session 只依赖"注册表接口"，
而不依赖"内置工具都有哪些"这个具体清单。
以后做精简版（只带时间工具）或定制版，换个工厂函数就行。
"""

from __future__ import annotations

from typing import Iterable, Optional

from .base import Tool
from .builtin import ALL_TOOLS
from .registry import ToolRegistry


def build_default_registry(extra: Optional[Iterable[Tool]] = None,
                           exclude: Optional[Iterable[str]] = None) -> ToolRegistry:
    """内置工具全量 + 可选的额外工具；exclude 可剔除某些内置工具。"""
    skip = set(exclude or ())
    registry = ToolRegistry([t for t in ALL_TOOLS if t.name not in skip])
    for item in extra or ():
        registry.register(item)
    return registry


__all__ = ["build_default_registry"]
