# -*- coding: utf-8 -*-
"""tui 层：终端界面（Textual）。

铁律：这一层可以 import core，core 永远不能 import 这一层。
界面里只做三件事——渲染事件、收集输入、把用户意图转成 core 的调用。
"""

from .app import XiaoAApp, run
from .bridge import QueueSink, TextualConfirm

__all__ = ["XiaoAApp", "run", "QueueSink", "TextualConfirm"]
