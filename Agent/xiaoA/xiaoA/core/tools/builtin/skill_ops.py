# -*- coding: utf-8 -*-
"""技能相关的两个工具：loadSkill（读）与 createSkill（写）。

loadSkill —— 技能正文的按需加载入口。

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

createSkill —— 按模型的自然语言要求落盘一个新技能。
只做"建目录 + 写 SKILL.md"这一件事：references/、scripts/ 这类附属
内容交给 createDir / createFile，本工具不替它们做主（职责单一，出错好查）。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ...skills import (
    SKILL_FILENAME,
    SKILLS_DIRNAME,
    SKILLS_SUBDIR,
    STARTUP_CWD,
    parse_frontmatter,
)
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


# ---------------------------------------------------------------- createSkill

# 规范：技能名只用小写字母、数字和连字符
_NAME_BAD_CHARS = re.compile(r"[^a-z0-9]+")
_NAME_MAX = 64
_DESC_MAX = 300
# 出现在值的最前面时，YAML 会把裸写的值读成别的东西（注释、列表、锚点…）
_YAML_UNSAFE_HEAD = tuple("-?:,[]{}#&*!|>'\"%@`")


def normalize_skill_name(raw: str) -> str:
    """把名字压成规范格式：小写字母、数字、连字符。

    中文、空格、下划线一律换成连字符；纯中文名压完是空串，
    这种情况留给调用方报错——音译不该由工具瞎猜，猜出来多半不是用户要的。
    """
    text = _NAME_BAD_CHARS.sub("-", str(raw or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text[:_NAME_MAX].strip("-")


def derive_description(body: str, fallback_name: str) -> str:
    """从正文里抠一句话当 description。

    只是兜底：正路是让模型把 description 写进 skillcontext 的 frontmatter。
    抠不出东西就用技能名凑一句——描述平淡总比因为缺字段注册不上强。
    """
    in_code = False
    for line in body.splitlines():
        text = line.strip()
        if text.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not text or text.startswith(("#", "|", "---")):
            continue
        text = text.lstrip("-*+ ").strip()
        if not text:
            continue
        for sep in ("。", "！", "？", ". ", "!", "?"):
            cut = text.find(sep)
            if cut > 0:
                keep = cut + 1 if sep in ("。", "！", "？") else cut
                text = text[:keep]
                break
        return text[:_DESC_MAX]
    return "技能 %s" % fallback_name


def split_skill_context(text: str) -> Tuple[Optional[Dict[str, str]], str]:
    """把 skillcontext 拆成 (frontmatter, 正文)。

    没有 frontmatter 时返回 (None, 原文)。有但写坏了抛 ValueError，
    由调用方翻译成给模型看的人话——让它知道该修哪一行。
    """
    clean = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if not clean.lstrip().startswith("---"):
        return None, clean

    fields = parse_frontmatter(clean)

    # 正文从第二行 --- 之后开始；找不到闭合就是坏文件，parse 已经拦过了
    seen_open = False
    close_at: Optional[int] = None
    for i, line in enumerate(clean.split("\n")):
        if line.strip() != "---":
            continue
        if not seen_open:
            seen_open = True
            continue
        close_at = i
        break
    body = "\n".join(clean.split("\n")[close_at + 1:]) if close_at is not None else ""
    return fields, body


def _yaml_scalar(text: str) -> str:
    """按需加引号：带 ": " / " #" 或从特殊字符开头的值，裸写会被 YAML 读错。"""
    value = " ".join(str(text).split())      # 压平换行和多余空白
    if not value:
        return '""'
    if value[0] in _YAML_UNSAFE_HEAD or ": " in value or " #" in value:
        # 优先单引号：skills.py 解析时只会剥外层引号、不做反转义，
        # 用单引号包裹能让值原样还原（描述里出现英文双引号时尤其重要）
        if "'" not in value:
            return "'%s'" % value
        return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')
    return value


def build_skill_document(name: str, description: str, body: str) -> str:
    """拼一份完整的 SKILL.md：frontmatter 在前，空行，正文在后。"""
    return "---\nname: %s\ndescription: %s\n---\n\n%s\n" % (
        _yaml_scalar(name), _yaml_scalar(description), body.strip("\n"))


def _skills_root(ctx: ToolContext) -> str:
    """技能根目录：优先用已经扫到的那个，没有就按启动目录推算。

    必须和 skills.discover_skills 用同一套规则，否则会出现
    "写到一个目录、扫描扫的是另一个目录"这种静默不一致。
    """
    catalog = ctx.skills
    if catalog is not None and catalog.root:
        return catalog.root
    return os.path.join(STARTUP_CWD, SKILLS_DIRNAME, SKILLS_SUBDIR)


def _created_message(name: str, raw_name: str, md_path: str, description: str,
                     body: str, ctx: ToolContext) -> ToolResult:
    """创建成功后的回执：告诉模型东西在哪、现在能不能用、下一步怎么补。"""
    lines = ["已创建技能 %r" % name]
    if raw_name != name:
        # 规范化是静默的，不说清楚的话模型下次会拿原名去 loadSkill
        lines.append("  name 已按规范转换：%r → %r（目录名只允许小写字母、数字、连字符）"
                     % (raw_name, name))
    lines += [
        "  SKILL.md：%s" % md_path,
        "  description：%s" % description,
    ]
    if not body.strip():
        lines.append("  注意：正文是空的，只有 frontmatter。要补内容用 editFile。")

    # 重扫技能目录——不刷新的话，模型当场 loadSkill 只会得到"没有这个技能"
    if ctx.refresh_skills is None:
        lines.append("技能已写入。当前会话没有挂刷新回调，重启后才会出现在技能清单里。")
    else:
        try:
            catalog = ctx.refresh_skills()
        except Exception as e:      # 刷新失败不能把"已经建好"说成失败
            lines.append("技能已写入，但刷新技能清单时出错（%s），重启后生效。" % e)
        else:
            if catalog is not None and catalog.get(name) is not None:
                lines.append("技能清单已刷新，现在可以直接 loadSkill（name=%r）读它。" % name)
            else:
                lines.append("技能已写入，但重扫后没登记成功。"
                             "用 readFile 看一眼 SKILL.md 的 frontmatter 是否完整。")

    lines.append("要加 references/、scripts/ 等附属文件：用 createDir 建目录、"
                 "createFile 写文件，路径都在 %s 下面。" % os.path.dirname(md_path))
    return ToolResult("\n".join(lines))


def create_skill(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    raw_name = str(args.get("name") or "").strip()
    context = str(args.get("skillcontext") or "")

    if not raw_name:
        return ToolResult(
            'createSkill 需要一个 name 参数（技能名），例如 {"name": "pdf-report"}。',
            ok=False)
    if not context.strip():
        return ToolResult(
            "skillcontext 是空的。把要写进 SKILL.md 的内容整段传进来，"
            "推荐直接带上 frontmatter（第一行 ---，name 和 description 各一行）。",
            ok=False)

    name = normalize_skill_name(raw_name)
    if not name:
        return ToolResult(
            "name 压成规范格式后是空的：%r。技能目录名只允许小写字母、数字和连字符，"
            "请给一个英文名（比如 weather-report）；中文名工具不会替你音译。"
            % raw_name, ok=False)

    skill_dir = os.path.join(_skills_root(ctx), name)
    md_path = os.path.join(skill_dir, SKILL_FILENAME)

    # ---- 已存在就不动：覆盖等于静默丢内容，改内容请走 editFile
    if os.path.exists(md_path):
        return ToolResult(
            "技能 %r 已经存在，本次没有写入，原文件保持原样：\n%s\n"
            "要改内容：先 readFile 看现状，再用 editFile 改写。"
            "想建另一个技能，换一个 name。" % (name, md_path), ok=False)
    if os.path.exists(skill_dir) and not os.path.isdir(skill_dir):
        return ToolResult("同名文件已存在，无法建技能目录：%s" % skill_dir, ok=False)
    if os.path.isdir(skill_dir) and os.listdir(skill_dir):
        return ToolResult(
            "目录 %s 已存在但没有 SKILL.md，且不是空目录。\n"
            "为免覆盖里面原有的内容，这次没有写入。确认它就是技能目录的话，"
            "用 createFile 把 SKILL.md 写进去；否则换一个 name。" % skill_dir,
            ok=False)

    # ---- 拆 frontmatter；坏掉就让模型重来，不替它猜
    try:
        fields, body = split_skill_context(context)
    except ValueError as e:
        return ToolResult(
            "skillcontext 以 --- 开头，但 frontmatter 不合法：%s\n"
            "两种改法：① 修好它（第一行 ---，name 和 description 各一行，再用 --- 收尾）；"
            "② 整段不写 frontmatter，由我按 name 生成。" % e, ok=False)

    description = (fields or {}).get("description", "").strip()
    if not description:
        description = derive_description(body, name)

    document = build_skill_document(name, description, body)

    try:
        os.makedirs(skill_dir, exist_ok=True)
        with open(md_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(document)
    except OSError as e:
        return ToolResult("创建失败：%s（%s）" % (md_path, e), ok=False)

    return _created_message(name, raw_name, md_path, description, body, ctx)


@tool(
    name="createSkill",
    description=(
        "按用户要求新建一个 Skill（技能）。在项目的 .agents/skills/ 下"
        "建一个与 name 对应的目录，并写入 SKILL.md。"
        "skillcontext 传要写进 SKILL.md 的完整内容：推荐以 --- 开头写好 name 和 "
        "description 的 frontmatter，再接正文；不写 frontmatter 也行，"
        "工具会按 name 生成并从正文首句提取 description。"
        "name 必须是英文，只允许小写字母、数字和连字符（会自动规范化）。"
        "同名技能已存在时不会覆盖，会返回提示；改内容请用 readFile + editFile。"
        "如果这个技能还需要 references/、scripts/ 等目录或其他辅助文件，"
        "建完后再调用 createDir / createFile 补齐。"
    ),
    parameters=params(
        {
            "name": string("技能名，英文，只用小写字母、数字和连字符，例如 pdf-report"),
            "skillcontext": string(
                "要写入 SKILL.md 的完整内容。推荐自带 YAML frontmatter："
                "--- 换行 name: xxx 换行 description: xxx 换行 --- 换行 正文"),
        },
        ["name", "skillcontext"],
    ),
    danger=DangerLevel.SAFE,
    tags=["skill"],
)
def create_skill_tool(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    return create_skill(args, ctx)


ALL_TOOLS = [load_skill_tool, create_skill_tool]


__all__ = [
    "ALL_TOOLS",
    "load_skill",
    "load_skill_tool",
    "create_skill",
    "create_skill_tool",
    "normalize_skill_name",
    "derive_description",
    "split_skill_context",
    "build_skill_document",
    "_resolve_filename",
    "MIN_BODY_CHARS",
    "MAX_CANDIDATES",
]
