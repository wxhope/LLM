# -*- coding: utf-8 -*-
"""xiaoA：带工具调用能力的本机助手。

目录分层（业务模块化 + 层级化）：

    xiaoA/
      core/                 业务核心，不认识任何界面
        config.py           配置读取
        events.py           向上的事件契约
        ports.py            向上的能力契约（UI 必须实现的协议）
        llm/                LLM 接入层
          client.py         流式 HTTP 客户端（OpenAI 兼容协议）
          stream.py         SSE 解析，可脱离网络单测
          types.py          数据对象
        tools/              工具层
          base.py           工具元信息与装饰器
          registry.py       注册表
          builtin/          具体工具实现
        session.py          会话编排：tool-calling 主循环
      tui/                  终端界面（Textual）
      web/  desktop/        未来版本，只需实现 core.ports 即可接入
      tests/                不依赖界面的单元测试
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
