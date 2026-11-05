# -*- coding: utf-8 -*-
"""会话持久化：把对话历史存成文件、列出来、再读回来。

支撑三个斜杠命令：
    /save    手动保存当前会话
    /new     新建会话前自动保存当前会话
    /resume  列出历史会话，选一个恢复

存哪里：<启动目录>/.agents/sessions/<编号>.json，和技能、上下文归档
同一个项目根。一个会话一个文件，文件名就是会话编号（时间戳），人能看懂，
排序即时间排序，也永远不会互相覆盖。

为什么存 JSON 而不是 JSONL：一个会话是一次性整体读写的，没有"逐行追加"
的需求；JSON 一次 read/write 就是全部，实现最短、最好教。

消息结构原样保存（role / content / tool_calls / tool_call_id），恢复后
直接就是 Session.messages 的形态——工具调用轮次也能完整回放，不会出现
"恢复了历史却接不上 tool 消息"的残缺结构。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List

SESSIONS_DIRNAME = (".agents", "sessions")


class SessionStoreError(Exception):
    """会话存取失败。消息可直接给用户看。"""


@dataclass(frozen=True)
class SessionMeta:
    """一条会话记录的"目录卡片"：列表页只显示这些，不装整份历史。"""

    session_id: str        # 形如 20261105-113000（文件名去掉扩展名）
    title: str             # 首条用户消息截断，人一眼认出这是哪个对话
    saved_at: str          # 保存时间
    message_count: int
    path: str


def sessions_dir(root: str) -> str:
    """<root>/.agents/sessions。root 传启动目录。"""
    return os.path.join(root, *SESSIONS_DIRNAME)


def _title_of(messages: List[Dict[str, Any]]) -> str:
    """拿第一条用户消息当标题，一行以内。"""
    for m in messages:
        if m.get("role") == "user":
            text = " ".join(str(m.get("content") or "").split())
            if text:
                return text[:40] + ("…" if len(text) > 40 else "")
    return "（空会话）"


def new_session_id(now: Optional[datetime] = None,
                   directory: Optional[str] = None) -> str:
    """生成会话编号：YYYYmmdd-HHMMSS。同一秒内重名时追加 -2、-3…"""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    if directory is None:
        return stamp
    candidate, bump = stamp, 1
    while os.path.exists(os.path.join(directory, candidate + ".json")):
        bump += 1
        candidate = "%s-%d" % (stamp, bump)
    return candidate


def save_session(messages: List[Dict[str, Any]], directory: str,
                 session_id: Optional[str] = None,
                 saved_at: Optional[str] = None) -> SessionMeta:
    """保存一个会话。返回它的元信息；失败抛 SessionStoreError。

    保存前先写临时文件再改名（Windows 上 os.replace 是原子的）：
    写一半断电/被杀时，旧的会话文件不会被半份新数据毁掉。
    """
    msgs = [dict(m) for m in (messages or [])]
    directory = directory or sessions_dir(".")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        raise SessionStoreError("建不了会话目录 %s：%s" % (directory, e)) from e

    sid = session_id or new_session_id(directory=directory)
    saved = saved_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    path = os.path.join(directory, sid + ".json")
    payload = {
        "version": 1,
        "session_id": sid,
        "title": _title_of(msgs),
        "saved_at": saved,
        "message_count": len(msgs),
        "messages": msgs,
    }
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise SessionStoreError("写会话文件失败 %s：%s" % (path, e)) from e

    return SessionMeta(session_id=sid, title=payload["title"],
                       saved_at=saved, message_count=len(msgs), path=path)


def list_sessions(directory: str) -> List[SessionMeta]:
    """列出目录下全部会话，按保存时间倒序（最新的在最上面）。

    单个文件坏了只跳过它，不让整个列表页报废——和 skills/MCP 同一个哲学。
    """
    result: List[SessionMeta] = []
    if not os.path.isdir(directory):
        return result
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            result.append(SessionMeta(
                session_id=str(data.get("session_id") or name[:-5]),
                title=str(data.get("title") or "（无标题）"),
                saved_at=str(data.get("saved_at") or ""),
                message_count=int(data.get("message_count") or 0),
                path=path,
            ))
        except (OSError, ValueError, TypeError):
            continue
    result.sort(key=lambda m: m.saved_at, reverse=True)
    return result


def load_session(path: str) -> List[Dict[str, Any]]:
    """读回一个会话的完整消息列表。文件不存在/损坏抛 SessionStoreError。"""
    if not os.path.exists(path):
        raise SessionStoreError("会话文件不存在：%s" % path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, UnicodeDecodeError) as e:
        raise SessionStoreError("读不了会话文件 %s：%s" % (path, e)) from e
    except json.JSONDecodeError as e:
        raise SessionStoreError("会话文件损坏（%s）：%s" % (path, e)) from e

    msgs = data.get("messages")
    if not isinstance(msgs, list):
        raise SessionStoreError("会话文件里没有 messages 列表：%s" % path)
    # 只保留"长得像消息"的条目，脏数据宁可不带回来
    clean = [m for m in msgs if isinstance(m, dict) and m.get("role")]
    if not clean:
        raise SessionStoreError("会话文件里没有可用消息：%s" % path)
    return clean


__all__ = [
    "SessionMeta",
    "SessionStoreError",
    "sessions_dir",
    "save_session",
    "list_sessions",
    "load_session",
    "new_session_id",
]
