# -*- coding: utf-8 -*-
"""时间类工具：getDate。

要求是"真去问系统的 date"，而不是 Python 自己 datetime.now()。
理由：这是让模型接触真实环境的示范——它的知识有截止日期，系统时钟没有。

Windows 有个坑：date 是 cmd 的内建命令，不带参数会提示"输入新日期"并阻塞。
所以这里按三级回退找可执行文件，绝不直接调 cmd 的 date。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, List, Optional

from ..base import DangerLevel, ToolContext, empty_params, tool
from .fs_guard import decode_bytes

# 让 date 输出的格式：年-月-日 时:分:秒 星期
DATE_FORMAT = "+%Y-%m-%d %H:%M:%S %A"

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_date_command() -> Optional[List[str]]:
    """找一条能直接执行、且不会阻塞等待输入的 date 命令。"""
    exe = shutil.which("date")
    if exe:
        return [exe, DATE_FORMAT]

    if os.name == "nt":
        # git.exe 通常在 <Git根>/cmd 下，date.exe 在 <Git根>/usr/bin 下
        git = shutil.which("git")
        if git:
            root = os.path.dirname(os.path.dirname(os.path.abspath(git)))
            for sub in ("usr/bin/date.exe", "mingw64/bin/date.exe", "bin/date.exe"):
                path = os.path.join(root, *sub.split("/"))
                if os.path.exists(path):
                    return [path, DATE_FORMAT]

        # 最后兜底：PowerShell 的 Get-Date，同样是问系统，不是 Python 算的
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            return [ps, "-NoProfile", "-Command",
                    "Get-Date -Format 'yyyy-MM-dd HH:mm:ss dddd'"]

    return None


def run_date() -> str:
    cmd = find_date_command()
    if not cmd:
        return "本机找不到可用的 date 命令，拿不到时间。"

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return "执行 date 命令失败：%s" % e

    out = decode_bytes(proc.stdout).strip()
    if proc.returncode != 0 or not out:
        err = decode_bytes(proc.stderr).strip()
        return "date 命令没有返回结果（退出码 %s）。%s" % (proc.returncode, err)
    return "当前系统日期时间（本机 date 命令的输出）：%s" % out


@tool(
    name="getDate",
    description=(
        "获取当前电脑的日期和时间。"
        "当用户问现在几点、今天几号、今天星期几、今年是哪一年这类问题时调用。"
    ),
    parameters=empty_params(),
    danger=DangerLevel.SAFE,
    tags=["time"],
)
def get_date(args: dict, ctx: ToolContext) -> str:
    return run_date()


ALL_TOOLS = [get_date]

__all__ = ["ALL_TOOLS", "get_date", "run_date", "find_date_command", "DATE_FORMAT"]
