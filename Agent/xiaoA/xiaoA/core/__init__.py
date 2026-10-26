# -*- coding: utf-8 -*-
"""core：业务核心层。

这一层不知道"界面长什么样"，也不 import 任何 UI 库。
它对外只暴露三样东西：
  1. Session    —— 编排整个对话
  2. events     —— 往下发的消息
  3. ports      —— 对上的能力要求（UI 来实现）

依赖方向永远是从外往里：tui/web/desktop → core，core 绝不反向依赖。
"""

from .config import AppConfig, ConfigError, load_config
from .session import Session
from .tools import ToolRegistry, build_default_registry

__all__ = [
    "AppConfig",
    "ConfigError",
    "Session",
    "ToolRegistry",
    "build_default_registry",
    "load_config",
]
