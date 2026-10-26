# -*- coding: utf-8 -*-
"""文件工具共用的"防线"小零件。

这些函数不注册成工具，只被工具调用。放在一起的理由：
  - 安全规则只有一处定义，改一次全部生效
  - 每个文件工具都能从同一个地方拿到"已经是绝对路径"的参数
"""

from __future__ import annotations

import os
from typing import Any, Optional


def resolve_path(raw: str) -> str:
    """把模型给的路径变成绝对路径。

    支持 ~、%VAR%（Windows）、$VAR（POSIX），相对路径按当前工作目录解析。
    """
    text = str(raw).strip()
    text = os.path.expanduser(os.path.expandvars(text))
    return os.path.abspath(text)


def need_path(args: dict, key: str = "path") -> Optional[str]:
    """取必填路径参数，空值返回 None。

    为什么必须挡一下：os.path.abspath("") 返回的是当前工作目录。
    如果 deleteDir 直接吃空参数，删的就是整个工作目录。
    模型偶尔会漏参数，这一层是最后的保险。
    """
    raw = str(args.get(key) or "").strip()
    return resolve_path(raw) if raw else None


def is_root_like(path: str) -> bool:
    """是不是盘根（C:\\ 或 /）。这种路径一律不许动。"""
    path = os.path.abspath(path)
    return os.path.dirname(path) in (path, "")


def is_protected(path: str) -> bool:
    """用户主目录。删它等于删掉一半的电脑，禁止。"""
    home = os.path.abspath(os.path.expanduser("~"))
    return os.path.abspath(path).rstrip("\\/").lower() == home.rstrip("\\/").lower()


def is_binary_file(path: str, probe: int = 8192) -> bool:
    """前 8KB 里有 NUL 字节就当二进制。

    纯文本文件（含 UTF-8、GBK 中文）不会出现 0x00，这个判据够用且极快，
    比猜测编码靠谱。
    """
    with open(path, "rb") as f:
        return b"\x00" in f.read(probe)


def decode_bytes(raw: bytes) -> str:
    """命令输出/文件内容可能是 UTF-8 也可能是 GBK，挨个试。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def human_size(size: int) -> str:
    if size < 1024:
        return "%d B" % size
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024:
            return "%.1f %s" % (value, unit)
    return "%.1f TB" % (value / 1024.0)


def safe_new_name(args: dict, key: str = "new_name") -> Optional[str]:
    """取"纯名字"参数。带路径分隔符的返回 None —— 那是偷偷搬家，不是改名。"""
    name = str(args.get(key) or "").strip()
    if not name or name in (".", "..") or name != os.path.basename(name):
        return None
    return name


def require_str(args: dict, key: str) -> Optional[str]:
    value = args.get(key)
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def get_int(args: dict, key: str) -> Optional[int]:
    raw = args.get(key)
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


__all__ = [
    "resolve_path",
    "need_path",
    "is_root_like",
    "is_protected",
    "is_binary_file",
    "decode_bytes",
    "human_size",
    "safe_new_name",
    "require_str",
    "get_int",
]
