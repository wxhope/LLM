# -*- coding: utf-8 -*-
"""xiaoA 启动脚本：python main.py [--tui|--check]

未来加了 Web / Desktop 版本，这里就是统一的入口分发点：

    python main.py --tui       终端界面
    python main.py --web       浏览器界面（待实现）
    python main.py --desktop   桌面窗口（待实现）

好处：用户不用记 `python -m xiaoA.web` 还是 `python -m xiaoA.desktop`，
      而且能在启动前做统一的依赖检查（比如 Textual 没装就给出安装命令）。
"""

from __future__ import annotations

import argparse
import os
import sys

# 保证从任意目录运行都能 import 到 xiaoA 包
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def check_core(config_path=None) -> int:
    """--check：不启动界面，只验证 core 是否能装配起来。"""
    from xiaoA.core.config import ConfigError, load_config
    from xiaoA.core.skills import discover_skills
    from xiaoA.core.tools import build_default_registry

    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        print("[配置错误] %s" % e)
        return 1
    reg = build_default_registry()
    catalog = discover_skills()      # 以启动时的工作目录为项目根
    print("配置 OK：模型 %s ｜ 接口 %s ｜ Key %s"
          % (cfg.model, cfg.base_url, cfg.masked_key()))
    if cfg.search_ready:
        print("联网搜索：已配置 ｜ 摘要模型 %s ｜ Key %s"
              % (cfg.search_model, cfg.masked_search_key()))
    else:
        print("联网搜索：未配置（searchWe 会提示补 [search] baidu_qianfan_api_key）")

    from xiaoA.core.tools.builtin.fetch_web import find_curl
    curl = find_curl()
    if curl:
        print("网页抓取：curl %s ｜ 单页上限 %d 字符" % (curl, cfg.fetch_max_chars))
    else:
        print("网页抓取：找不到 curl.exe（FetchWeb 不可用）")
    print("本地命令：超时 %g 秒 ｜ stdout/stderr 各上限 %d 字符 ｜ 不设命令白名单（教学阶段）"
          % (cfg.cli_timeout, cfg.cli_max_chars))
    print("工具 %d 个：%s" % (len(reg), "、".join(reg.names())))
    print(catalog.summary())
    for problem in catalog.problems:
        print("技能提醒：%s" % problem)
    return 0


def launch_tui(config_path=None) -> int:
    try:
        import textual  # noqa: F401
    except ImportError:
        print("缺少 Textual，先运行：")
        print("    pip install -r requirements.txt")
        return 1

    from xiaoA.tui.app import run
    run(config_path)
    return 0


def launch_web(config_path=None) -> int:
    print("Web 界面还没做。")
    print("计划：xiaoA/web/server.py 用 http.server 提供页面，")
    print("      用 WebSocket（或 SSE）把 core.events 推给浏览器。")
    return 2


def launch_desktop(config_path=None) -> int:
    print("桌面界面还没做。")
    print("计划：xiaoA/desktop/app.py 用 pywebview 套住 Web 版，")
    print("      或直接写成原生窗口，同样接 core.events。")
    return 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="xiaoA",
        description="xiaoA —— 本机助手（core + 多界面）",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--tui", action="store_true", help="启动终端界面（默认）")
    group.add_argument("--web", action="store_true", help="启动 Web 界面（未实现）")
    group.add_argument("--desktop", action="store_true", help="启动桌面界面（未实现）")
    group.add_argument("--check", action="store_true", help="只检查配置与工具，不启动界面")
    parser.add_argument("-c", "--config", help="config.ini 的路径")
    args = parser.parse_args(argv)

    if args.check:
        return check_core(args.config)
    if args.web:
        return launch_web(args.config)
    if args.desktop:
        return launch_desktop(args.config)
    return launch_tui(args.config)


if __name__ == "__main__":
    sys.exit(main())
