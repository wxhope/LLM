# -*- coding: utf-8 -*-
"""事件：core 往 UI 发的"通知书"。

为什么要有事件层：
  core 不能直接 print，因为 Web 版没有终端、桌面版没有 stdout。
  所以 core 只负责"宣告发生了什么"，谁来渲染、渲染成什么样，UI 自己决定。

这套事件是 core 的唯一出口。任何 UI 只要会处理这几种事件，
就等于接上了整个助手——新增 Web / Desktop 界面时，core 一行都不用改。

事件分三类（继承自 Event 基类，便于 UI 做统一日志）：
  流式类  —— 逐字推送，UI 可以做成打字机效果，也可以攒完再一次性渲染
  结果类  —— 一轮结束后的收尾
  告知类  —— 工具调用、配额提示等中间状态
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Event:
    """所有事件的基类。"""


# ------------------------------------------------------------------ 流式

@dataclass(frozen=True)
class TextDelta(Event):
    """模型吐出的一小段正文。

    注意：可能是半句话，甚至是半个 emoji，UI 只管拼起来显示。
    """
    text: str


@dataclass(frozen=True)
class RoundStarted(Event):
    """新一轮请求开始了。UI 可以据此清空"正在输入"状态。"""
    round_index: int


# ------------------------------------------------------------------ 工具

@dataclass(frozen=True)
class ToolCallStarted(Event):
    """模型决定调用某个工具，即将开始执行。"""
    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolCallFinished(Event):
    """工具执行完了，带上返回值（已经是给模型看的字符串）。"""
    call_id: str
    name: str
    result: str
    ok: bool = True


# ------------------------------------------------------------------ 结果

@dataclass(frozen=True)
class ReplyCompleted(Event):
    """一轮回复彻底结束。

    message 是完整的 assistant 消息（含 tool_calls 时说明还在工具循环中，
    但通常这个事件只在一轮最终回答后发出），UI 用它做"落盘"或统计。
    """
    text: str
    rounds: int = 1
    tool_calls: int = 0


@dataclass(frozen=True)
class Notice(Event):
    """给用户看的提示，不属于模型输出。例如"历史太长已裁剪"。"""
    text: str
    level: str = "info"          # info | warning | error


@dataclass(frozen=True)
class ErrorOccurred(Event):
    """这一轮失败了。UI 应当显示错误，并允许用户继续下一句。"""
    message: str
    round_index: int = 0


@dataclass(frozen=True)
class SessionClosed(Event):
    """会话结束（用户退出、或程序收尾）。"""
    reason: str = "bye"


@dataclass(frozen=True)
class TokenUsage(Event):
    """可选的用量信息。服务端没给就永远不发。"""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


# 便捷联合类型，方便 UI 写 isinstance 分支
ALL_EVENTS: List[Any] = [
    TextDelta, RoundStarted, ToolCallStarted, ToolCallFinished,
    ReplyCompleted, Notice, ErrorOccurred, SessionClosed, TokenUsage,
]

__all__ = [
    "Event",
    "TextDelta",
    "RoundStarted",
    "ToolCallStarted",
    "ToolCallFinished",
    "ReplyCompleted",
    "Notice",
    "ErrorOccurred",
    "SessionClosed",
    "TokenUsage",
    "ALL_EVENTS",
]
