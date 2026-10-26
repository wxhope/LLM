# -*- coding: utf-8 -*-
"""LLM 接入子层。

里面三块：
  types.py    数据对象（ToolCall / AssistantMessage / StreamDelta）
  stream.py   SSE 解析，纯函数，能脱离网络单测
  client.py   HTTP 连接（唯一知道 http.client 的地方）

换模型厂商时只改 client 和 stream——工具层、会话层、界面层都不受影响。
"""

from .client import LLMClient, LLMError
from .types import AssistantMessage, StreamDelta, ToolCall

__all__ = ["LLMClient", "LLMError", "AssistantMessage", "StreamDelta", "ToolCall"]
