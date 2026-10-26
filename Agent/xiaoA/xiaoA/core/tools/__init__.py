# -*- coding: utf-8 -*-
"""工具子层：契约、注册表、内置工具。

设计要点：
  1. 工具不认识会话，也不认识界面——它只是"输入 dict，输出字符串"
     "输出字符串"这个约定很重要：模型只能读文本，而且文本里可以写
     "已创建：..." 这种人和模型都看得懂的话。
  2. 工具通过注册表暴露，模型看到的是自动生成的 JSON Schema。
     以后加工具只需写一个函数 + 一个 @tool 装饰器，别处一行都不用改（开闭原则）。
"""

from .base import DangerLevel, Tool, ToolContext, ToolResult, tool
from .defaults import build_default_registry
from .registry import ToolRegistry

__all__ = [
    "DangerLevel",
    "Tool",
    "ToolContext",
    "ToolResult",
    "tool",
    "ToolRegistry",
    "build_default_registry",
]
