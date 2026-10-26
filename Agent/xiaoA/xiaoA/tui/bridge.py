# -*- coding: utf-8 -*-
"""TUI 与 core 之间的适配器。

这里解决一个 Textual 特有的矛盾：

    界面必须跑在事件循环里（不阻塞），
    而 core 的 Session.ask() 是同步阻塞的（要等网络、要等工具）。

解法是"生产者-消费者"：
    - Session 跑在后台线程，事件往 Queue 里丢
    - UI 线程定期从 Queue 取事件并渲染，绝不在 UI 线程里等网络

这是所有 GUI 框架的通用套路，Web 版对应的是把事件推给 WebSocket，
桌面版对应的是推给窗口消息队列。core 一行不用改。
"""

from __future__ import annotations

import queue
from typing import Optional

from ..core.events import Event
from ..core.ports import ConfirmPort, EventSink


class QueueSink(EventSink):
    """把事件放进队列，由 UI 线程取出。

    不直接操作 UI 控件是刻意的：Textual 的控件只能在主线程改，
    后台线程乱碰会随机崩（而且是那种最难查的崩）。
    """

    def __init__(self) -> None:
        self.queue: "queue.Queue[Event]" = queue.Queue()

    def emit(self, event: Event) -> None:
        self.queue.put(event)

    def drain(self, limit: int = 200):
        """一次取一批，避免每帧只处理一个事件导致界面卡顿。"""
        items = []
        for _ in range(limit):
            try:
                items.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return items


class TextualConfirm(ConfirmPort):
    """把 core 的"确认请求"转交给界面。

    注意：core 的 ConfirmPort.ask 是同步的（True/False），
    而 Textual 的模态框是异步的。这里用两个队列做一次握手：

        core 线程  ──put(request)──►  UI 线程弹出对话框
        core 线程  ◄──get(answer)──   UI 线程把结果放回来

    core 线程会在这里阻塞等用户按 Y/N，这是对的——删除操作本来就该停住。
    """

    def __init__(self) -> None:
        self.request: "queue.Queue[tuple]" = queue.Queue()
        self.answer: "queue.Queue[bool]" = queue.Queue()

    def ask(self, title: str, message: str) -> bool:
        self.request.put((title, message))
        # 没有超时：用户没回答之前，删除就不该发生
        return bool(self.answer.get())

    def poll(self) -> Optional[tuple]:
        try:
            return self.request.get_nowait()
        except queue.Empty:
            return None

    def reply(self, ok: bool) -> None:
        self.answer.put(bool(ok))
