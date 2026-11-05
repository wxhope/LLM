# -*- coding: utf-8 -*-
"""TUI 的模态组件：通知对话框、会话恢复面板、命令补全面板。

都做成独立文件，是因为 app.py 已经装了"主界面"这一件事；
对话框、面板是可复用的浮层，混在一起只会让两边都难改。

这里只管"长什么样、怎么交互"，业务决定（恢复哪个会话、要不要退出）
通过 dismiss(结果) 交回给 app —— ModalScreen 的标准用法。
"""

from __future__ import annotations

from typing import List, Optional

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, ListItem, ListView, Static

from ..core.session_store import SessionMeta


class NoticeDialog(ModalScreen):
    """通知对话框：一段必须被用户看到的文字 + 一个关闭按钮。

    首次运行生成 config.ini 时用它告诉用户"改完配置重启才能用"。
    ESC / Enter / 点按钮都能关闭。
    """

    BINDINGS = [
        ("escape", "close", "关闭"),
        ("enter", "close", "关闭"),
    ]

    def __init__(self, title: str, body: str, button_text: str = "我知道了") -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._button_text = button_text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog-box"):
            yield Label(self._title, id="dialog-title")
            with VerticalScroll(id="dialog-body"):
                yield Static(self._body)
            yield Button(self._button_text, id="dialog-ok", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(True)

    def action_close(self) -> None:
        self.dismiss(True)


class ResumePanel(ModalScreen):
    """历史会话选择面板。

    交互：↑/↓ 移动高亮，Enter 恢复选中的会话，ESC 取消。
    列表来自 session_store.list_sessions()，最新的排在最上面。
    """

    BINDINGS = [
        ("escape", "cancel", "取消"),
    ]

    def __init__(self, metas: List[SessionMeta]) -> None:
        super().__init__()
        self._metas = metas

    def compose(self) -> ComposeResult:
        with Vertical(id="resume-box"):
            yield Label("选择要恢复的会话", id="resume-title")
            yield ListView(
                *[ListItem(Label(" %s   %d 条 ｜ %s" % (m.title, m.message_count, m.saved_at)),
                           id="sess-%s" % m.session_id)
                  for m in self._metas],
                id="resume-list",
            )
            yield Static("↑/↓ 选择 · Enter 恢复 · ESC 取消", id="resume-hint")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index or 0
        if 0 <= index < len(self._metas):
            self.dismiss(self._metas[index])
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
