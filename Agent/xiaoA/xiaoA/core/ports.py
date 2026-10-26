# -*- coding: utf-8 -*-
"""ports：core 对上层提出的"能力要求"。

这是依赖倒置的关键一步。举例：

    Textual 版本需要"逐字打印"，所以要一个能流式推送的回调；
    Web 版本用的是 WebSocket，天然就是流式的；
    桌面版可能是 pywebview，也能推。

如果 core 直接调 print()，那 Web 版就只能伪造一个假终端——那叫耦合。
所以 core 只声明一个协议：

    class EventSink(Protocol):
        def emit(self, event: Event) -> None: ...

任何界面，只要实现 emit()，就能接上 core。core 完全不知道对方是终端还是浏览器。

本文件里的所有 Protocol 都是"结构类型"——不需要继承，
只要方法对得上就算实现（鸭子类型的正式写法）。
"""

from __future__ import annotations

from typing import Callable, List, Protocol, runtime_checkable

from .events import Event


@runtime_checkable
class EventSink(Protocol):
    """事件接收端。每种界面都必须实现这一个。

    约定：emit() 必须立刻返回，不能阻塞等待用户操作。
    要问用户问题，用下面的 ConfirmPort，它允许阻塞。
    """

    def emit(self, event: Event) -> None:
        ...


@runtime_checkable
class ConfirmPort(Protocol):
    """危险操作的确认口。

    core 只负责问"可以吗"，怎么问（终端 input、弹窗、网页对话框）由 UI 决定。
    Core 只认返回值：True 才动手。
    """

    def ask(self, title: str, message: str) -> bool:
        ...


class AutoConfirm:
    """永远答"是"的确认口。仅用于测试和自动化脚本，绝不给真人用。"""

    def ask(self, title: str, message: str) -> bool:
        return True


class AutoDeny:
    """永远答"否"的确认口。测试里用它可以验证"拒绝后什么都没删"。"""

    def ask(self, title: str, message: str) -> bool:
        return False


class ListSink:
    """把事件收集到列表里。测试用，也方便脚本把整轮结果一次性打印出来。"""

    def __init__(self) -> None:
        self.events: List[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def texts(self) -> str:
        from .events import TextDelta
        return "".join(e.text for e in self.events if isinstance(e, TextDelta))

    def clear(self) -> None:
        self.events.clear()


# 有些场景（比如命令行批处理）只想拿回调，不想建类。
# 给个薄适配器，把函数当 sink 用。
class FunctionSink:
    def __init__(self, fn: Callable[[Event], None]) -> None:
        self._fn = fn

    def emit(self, event: Event) -> None:
        self._fn(event)


__all__ = [
    "EventSink",
    "ConfirmPort",
    "AutoConfirm",
    "AutoDeny",
    "ListSink",
    "FunctionSink",
]
