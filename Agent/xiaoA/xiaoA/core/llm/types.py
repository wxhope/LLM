# -*- coding: utf-8 -*-
"""LLM 层的数据对象。

刻意用普通 dict 的"薄包装"，而不是重写一套消息类：
  - 发出去的东西必须是 OpenAI 协议的 JSON，多套类只会造成来回转换
  - 但接收接口只要求鸭子类型，不必非是这些类
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    """模型发起的一次工具调用。

    arguments 始终是"原始字符串"——不要提前 json.loads。
    发给服务端时它必须原样回传，提前转 dict 再转回字符串，
    可能因为转义/空格差异被判成非法。
    """

    id: str
    name: str
    arguments: str = ""

    def to_api(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments or "{}"},
        }


@dataclass
class AssistantMessage:
    """一次 assistant 回复的完整形态，带 tool_calls 时说明它还要调工具。"""

    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)

    def to_api(self) -> Dict[str, Any]:
        msg: Dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [c.to_api() for c in self.tool_calls]
        return msg


@dataclass
class StreamDelta:
    """解析 SSE 后得到的一小步增量。"""

    content: Optional[str] = None
    tool_call_index: Optional[int] = None
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_arguments: Optional[str] = None
    finish_reason: Optional[str] = None
    done: bool = False
    raw: Optional[Dict[str, Any]] = None


__all__ = ["ToolCall", "AssistantMessage", "StreamDelta"]
