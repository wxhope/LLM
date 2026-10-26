# -*- coding: utf-8 -*-
"""python -m xiaoA.tui 的入口。

放在这里有讲究：这样 `from ..core import ...` 有明确的包上下文，
不会因为"直接跑一个文件"而产生相对导入失败的经典问题。
"""

from __future__ import annotations

import argparse
import sys

from .app import run


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="xiaoA.tui",
        description="xiaoA 终端界面（Textual）",
    )
    parser.add_argument("-c", "--config", help="config.ini 的路径，默认用仓库根目录下那份")
    parser.add_argument("--check", action="store_true",
                        help="只检查配置与工具能否加载，不启动界面")
    args = parser.parse_args(argv)

    if args.check:
        from ..core.config import ConfigError, load_config
        from ..core.tools import build_default_registry
        try:
            cfg = load_config(args.config)
        except ConfigError as e:
            print("[配置错误] %s" % e)
            return 1
        reg = build_default_registry()
        print("配置 OK：模型 %s，接口 %s，Key %s"
              % (cfg.model, cfg.base_url, cfg.masked_key()))
        print("工具 %d 个：%s" % (len(reg), "、".join(reg.names())))
        try:
            import textual
            print("Textual %s" % textual.__version__)
        except ImportError:
            print("[提示] 还没装 textual，请先：pip install -r requirements.txt")
            return 1
        return 0

    run(args.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
