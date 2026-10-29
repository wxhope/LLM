# -*- coding: utf-8 -*-
"""loadSkill：技能正文的按需加载入口。

skills.py 的注册阶段只登记 name/description；模型从系统提示里看到
"有哪些技能"，需要哪个的详细内容，就用这个工具取——上下文不为
用不上的说明书买单（动态加载原则：不调用就不进上下文）。

参数（对齐 Agent Skills 规范）：
    name      必需。技能名，来自系统提示里的清单。
    filename  可选。技能内的某个文件。只给 name 时读 SKILL.md 全文；
              给了 filename 就只读那个文件，不再重复带 SKILL.md。

filename 的解析顺序（都在"技能目录"这个边界内）：
    1. 相对技能目录的路径     scripts/run.py
    2. 项目相对路径           .agents/skills/xxx/scripts/run.py
    3. 绝对路径               只放行落在技能目录以内的
    4. 裸文件名               在技能目录内递归搜索；唯一命中直接读，
                              多个同名返回候选清单，由模型决定读哪个

安全边界：所有路径先解析再核对，**必须落在该技能的目录以内**。
../ 与绝对路径的越界尝试一律拒绝——技能文件再怎么翻，也翻不出自己的目录。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from ..base import DangerLevel, ToolContext, ToolResult, params, string, tool
from .fs_guard import is_binary_file, resolve_path

# 单次加载的硬上限兜底值；正常由 ctx.max_read_chars 决定
MIN_BODY_CHARS = 1000
# 同名文件候选最多列多少个，防止把上下文刷成文件清单
MAX_CANDIDATES = 10


def _inside(root: str, path: str) -> bool:
    """path 解析后是否落在 root 目录以内（含 root 自身）。

    用 realpath 而不是 abspath：.. 和符号链接都按真实落点算，
    "看着在目录里、实际指到外面"的路径在这里现形。
    """
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    return path_real == root_real or path_real.startswith(root_real + os.sep)


def _rel(path: str, root: str) -> str:
    """给模型看的路径：相对技能目录、统一用 /，跨平台都读得懂。"""
    return os.path.relpath(path, root).replace("\\", "/")


def _read_capped(path: str, limit: int) -> ToolResult:
    """读文本文件并按上限截断；二进制文件拒绝——那不是给模型看的内容。"""
    try:
        if is_binary_file(path):
            return ToolResult(
                "文件 %s 是二进制文件，读成文本只会得到乱码。"
                "如果它是脚本，用 RunCli 执行它再看输出。" % path, ok=False)
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
    except OSError as e:
        return ToolResult("文件读不了：%s（%s）" % (path, e), ok=False)

    limit = max(int(limit or 0), MIN_BODY_CHARS)
    if len(body) > limit:
        body = (body[:limit]
                + "\n…（文件共 %d 字符，已截断到 %d。需要后半段就分段读，"
                  "或让用户在终端里直接看。）" % (len(body), limit))
    return ToolResult(body)


def _resolve_filename(raw: str, skill_dir: str, project_root: str
                      ) -> Tuple[Optional[str], Optional[str]]:
    """把模型给的 filename 解析成技能目录内的绝对路径。

    返回 (路径, None) 表示解析成功；
    返回 (None, 原因) 表示失败，原因是给人和模型一起看的说明；
    裸文件名的搜索结果是"命中了多个"，也走 (None, 候选清单) 这条路。
    """
    text = str(raw).strip().strip('"').strip("'")
    if not text:
        return None, "filename 是空的。要么不传（读 SKILL.md），要么给一个具体文件。"

    # ---- 1) 绝对路径：只放行落在技能目录以内的
    if os.path.isabs(text):
        path = resolve_path(text)
        if not _inside(skill_dir, path):
            return None, ("绝对路径越界：%s 不在技能目录 %s 内。"
                          "技能文件只能从自己的目录里取，其他文件请用 readFile。"
                          % (text, _rel(skill_dir, project_root) or skill_dir))
        if not os.path.isfile(path):
            return None, "绝对路径在技能目录内，但文件不存在：%s" % path
        return path, None

    has_sep = ("/" in text or "\\" in text
               or text.startswith(".") or text.startswith("~"))

    # ---- 2) 带路径成分的相对路径：先按技能目录，再按项目根
    if has_sep:
        for base, tag in ((skill_dir, "技能目录"), (project_root, "项目根目录")):
            path = resolve_path(os.path.join(base, text))
            # 项目相对路径也要落回技能目录内，越界同样拒绝
            if not _inside(skill_dir, path):
                continue
            if os.path.isfile(path):
                return path, None
        return None, ("找不到文件 %r。已按「技能目录内相对路径」和「项目相对路径」"
                      "两种方式解析，都没命中（路径不能越出技能目录）。"
                      "只有文件名时可以直接传文件名，我会在技能目录里递归找。"
                      % text)

    # ---- 3) 裸文件名：技能目录内递归搜索
    hits: List[str] = []
    for dirpath, dirnames, filenames in os.walk(skill_dir):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", ".git", ".venv")]
        for fname in filenames:
            if fname.casefold() == text.casefold():
                hits.append(os.path.join(dirpath, fname))
                if len(hits) > MAX_CANDIDATES:
                    break
        if len(hits) > MAX_CANDIDATES:
            break

    if not hits:
        return None, ("技能目录里没有叫 %r 的文件（已递归搜索）。"
                      "确认文件名，或改传相对路径。" % text)
    if len(hits) == 1:
        return hits[0], None

    listing = "\n".join("  - %s" % _rel(p, skill_dir)
                        for p in hits[:MAX_CANDIDATES])
    return None, ("文件名 %r 在技能目录里命中了 %d 个位置，请选一个再调：\n%s"
                  "\n（传上面对应的相对路径即可）" % (text, len(hits), listing))


def load_skill(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    name = str(args.get("name") or "").strip()
    if not name:
        return ToolResult(
            'loadSkill 需要一个 name 参数（技能名），例如 {"name": "example-hello"}。'
            "可用技能见系统提示里的清单。", ok=False)

    catalog = ctx.skills
    if catalog is None or not catalog.skills:
        return ToolResult(
            "当前没有任何已注册技能（%s）。"
            "没有技能清单可加载，请直接回答用户的问题，不要再调 loadSkill。"
            % (catalog.root if catalog and catalog.root else ".agents/skills 目录"),
            ok=False)

    meta = catalog.get(name)
    if meta is None:
        return ToolResult(
            "没有叫「%s」的技能。已注册的技能：%s"
            % (name, "、".join(catalog.names()) or "（无）"), ok=False)

    skill_dir = os.path.dirname(meta.path)

    # ---- 只给 name：SKILL.md 全文
    filename = str(args.get("filename") or "").strip()
    if not filename:
        return _read_capped(meta.path, ctx.max_read_chars)

    # ---- name + filename：只读那个文件，不重复带 SKILL.md
    project_root = (os.path.dirname(os.path.dirname(catalog.root))
                    if catalog.root else os.getcwd())
    path, reason = _resolve_filename(filename, skill_dir, project_root)
    if path is None:
        return ToolResult(reason, ok=False)
    return _read_capped(path, ctx.max_read_chars)


@tool(
    name="loadSkill",
    description=(
        "加载已注册技能的完整说明或技能内的文件。"
        "系统提示里列出过技能清单；当任务符合某个技能的 description、"
        "或用户点名要用某个技能时，先调用本工具（只传 name）拿到 SKILL.md "
        "的完整指引再动手，不要凭技能名猜用法。"
        "技能正文里提到其他文件时，再传 filename 读取：支持技能目录内的"
        "相对路径、项目相对路径、绝对路径，或裸文件名（目录内递归查找）。"
    ),
    parameters=params(
        {
            "name": string("技能名，来自系统提示里的技能清单，例如 example-hello"),
            "filename": string(
                "可选。技能内的具体文件：相对技能目录的路径、项目相对路径、"
                "绝对路径，或裸文件名（在技能目录内递归搜索）。不传就读 SKILL.md 全文"),
        },
        ["name"],
    ),
    danger=DangerLevel.SAFE,
    tags=["skill"],
)
def load_skill_tool(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    return load_skill(args, ctx)


ALL_TOOLS = [load_skill_tool]

__all__ = [
    "ALL_TOOLS",
    "load_skill",
    "load_skill_tool",
    "_resolve_filename",
    "MIN_BODY_CHARS",
    "MAX_CANDIDATES",
]
