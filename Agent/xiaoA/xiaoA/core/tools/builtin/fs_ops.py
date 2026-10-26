# -*- coding: utf-8 -*-
"""文件操作工具（10 个）。

分组：
  查看  getCwd  listDir  getFileType  readFile
  新建  createFile  createDir
  修改  editFile  renamePath
  删除  deleteDir  deleteFile      ← 标记为 DESTRUCTIVE，执行前必须问用户

安全规则统一放在 fs_guard，本文件只关心"这个操作具体怎么做"。
注意：本文件不 import 任何 UI、也不 print——它只返回字符串。
"""

from __future__ import annotations

import mimetypes
import os
import shutil

from ..base import (
    DangerLevel,
    ToolContext,
    ToolResult,
    empty_params,
    enumeration,
    params,
    string,
    tool,
)
from .fs_guard import (
    human_size,
    is_binary_file,
    is_protected,
    is_root_like,
    need_path,
    require_str,
    resolve_path,
    safe_new_name,
    decode_bytes,
)

FS_TAGS = ["fs"]
PATH_PROP = string("路径的绝对路径；支持 ~ 与 %VAR%")

# 先把 os.rename 的原始引用留住。文件底部有个工具函数叫 rename_path，
# 它在模块命名空间里会遮住同名属性，这里留个名字更稳妥。
_ORIG_RENAME = os.rename


# ------------------------------------------------------------------ 查看

@tool("getCwd", "获取当前工作目录的绝对路径。用户问「现在在哪个目录」时调用。",
      empty_params(), tags=FS_TAGS)
def get_cwd(args: dict, ctx: ToolContext) -> str:
    return "当前工作目录：%s" % os.getcwd()


@tool(
    "listDir",
    (
        "列出指定目录下的所有文件和子目录，带文件大小。"
        "用户想看某个目录里有什么、想知道某个文件在不在时调用。"
        "不传 path 就列当前工作目录。"
    ),
    params({"path": string("要列出的目录路径；留空表示当前工作目录")}, []),
    tags=FS_TAGS,
)
def list_dir(args: dict, ctx: ToolContext) -> str:
    raw = str(args.get("path") or "").strip()
    path = resolve_path(raw) if raw else os.getcwd()

    if not os.path.exists(path):
        return "路径不存在：%s" % path
    if not os.path.isdir(path):
        return "这不是目录：%s（要看文件详情请用 getFileType）" % path

    try:
        names = sorted(os.listdir(path))
    except OSError as e:
        return "读不了这个目录：%s" % e

    lines = ["目录：%s" % path]
    if not names:
        lines.append("（空目录）")
        return "\n".join(lines)

    dirs, files = [], []
    for name in names:
        full = os.path.join(path, name)
        if os.path.isdir(full):
            dirs.append(name + "/")
        else:
            try:
                size = human_size(os.path.getsize(full))
            except OSError:
                size = "?"
            files.append("%s  (%s)" % (name, size))

    # 目录排前面，符合人和模型共同的阅读习惯
    lines.append("共 %d 项：%d 个目录，%d 个文件"
                 % (len(names), len(dirs), len(files)))
    if dirs:
        lines.append("[目录]")
        lines.extend("  " + n for n in dirs)
    if files:
        lines.append("[文件]")
        lines.extend("  " + n for n in files)
    return "\n".join(lines)


@tool(
    "getFileType",
    (
        "判断一个路径的类型：目录还是文件、扩展名、MIME、内容是纯文本还是二进制。"
        "在决定能不能读或改写某个文件之前，先调这个确认。"
    ),
    params({"path": PATH_PROP}, ["path"]),
    tags=FS_TAGS,
)
def get_file_type(args: dict, ctx: ToolContext) -> str:
    path = need_path(args)
    if not path:
        return "缺少 path 参数。"
    if not os.path.exists(path):
        return "路径不存在：%s" % path

    if os.path.isdir(path):
        try:
            count = len(os.listdir(path))
        except OSError:
            count = -1
        return "类型：目录\n路径：%s\n直接子项：%d 个" % (path, count)

    ext = os.path.splitext(path)[1].lower() or "（无扩展名）"
    mime, _ = mimetypes.guess_type(path)
    try:
        binary = is_binary_file(path)
        size = human_size(os.path.getsize(path))
    except OSError as e:
        return "读不了这个文件：%s" % e

    return "\n".join([
        "类型：文件",
        "路径：%s" % path,
        "扩展名：%s" % ext,
        "MIME 推测：%s" % (mime or "猜不出来"),
        "大小：%s" % size,
        "内容判定：%s" % ("二进制" if binary else "纯文本"),
        "能否用 readFile / editFile 处理：%s" % ("不能" if binary else "能"),
    ])


@tool(
    "readFile",
    (
        "读取一个纯文本文件的内容。二进制文件会被拒绝。"
        "文件太长时只返回前面一部分。"
    ),
    params({"path": PATH_PROP}, ["path"]),
    tags=FS_TAGS,
)
def read_file(args: dict, ctx: ToolContext) -> str:
    path = need_path(args)
    if not path:
        return "缺少 path 参数。"
    if not os.path.exists(path):
        return "文件不存在：%s" % path
    if os.path.isdir(path):
        return "这是目录，不是文件：%s（要列目录请用 listDir）" % path

    try:
        if is_binary_file(path):
            return "%s 是二进制文件，本工具只读纯文本。" % path
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        return "读文件失败：%s" % e

    text = decode_bytes(raw)
    limit = max(int(ctx.max_read_chars or 0), 0)
    note = ""
    # 截断是必须的：一个几十万行的日志会把整个上下文冲爆，
    # 而且花了钱还什么都没答上来。
    if limit and len(text) > limit:
        text = text[:limit]
        note = "\n...（文件太长，只返回前 %d 个字符）" % limit

    return "文件：%s\n--- 内容开始 ---\n%s%s\n--- 内容结束 ---" % (path, text, note)


# ------------------------------------------------------------------ 新建

@tool(
    "createFile",
    (
        "在指定目录下新建一个文本文件并写入内容。"
        "同名文件已存在时不会覆盖，会报错让你改用 editFile。"
    ),
    params({
        "dir": string("文件所在目录的绝对路径"),
        "name": string("文件名，只写名字，不要带路径"),
        "content": string("文件内容，纯文本"),
    }, ["dir", "name"]),
    tags=FS_TAGS,
)
def create_file(args: dict, ctx: ToolContext) -> str:
    directory = resolve_path(args.get("dir") or ".")
    name = safe_new_name(args, "name") or str(args.get("name") or "").strip()
    content = args.get("content") or ""

    if not name:
        return "缺少文件名参数 name。"
    if name != os.path.basename(name) or name in (".", ".."):
        return "name 只能是文件名，不要带路径：%s" % name
    if not os.path.isdir(directory):
        return "目录不存在：%s（要新建目录请用 createDir）" % directory

    path = os.path.join(directory, name)
    if os.path.exists(path):
        # 不覆盖是有意的：覆盖等于静默丢数据，交给 editFile 显式做
        return "文件已存在：%s。本工具不覆盖，要改内容请用 editFile。" % path

    try:
        # newline="" 保证内容原样写入，不偷偷把 \n 变成 \r\n
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    except OSError as e:
        return "创建失败：%s" % e
    return "已创建：%s（%d 个字符）" % (path, len(content))


@tool(
    "createDir",
    "创建一个目录，父目录不存在会一起创建（相当于 mkdir -p）。目录已存在时直接返回成功。",
    params({"path": PATH_PROP}, ["path"]),
    tags=FS_TAGS,
)
def create_dir(args: dict, ctx: ToolContext) -> str:
    path = need_path(args)
    if not path:
        return "缺少 path 参数。"
    if os.path.exists(path) and not os.path.isdir(path):
        return "同名文件已存在，无法建目录：%s" % path
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        return "创建目录失败：%s" % e
    return "目录已就绪：%s" % path


# ------------------------------------------------------------------ 修改

@tool(
    "editFile",
    (
        "修改一个已存在的纯文本文件。mode=overwrite 整体覆盖（默认），"
        "mode=append 追加到末尾。只能改文本文件。"
    ),
    params({
        "path": PATH_PROP,
        "content": string("要写入的纯文本内容"),
        "mode": enumeration("overwrite 覆盖全文，append 追加到末尾",
                            ["overwrite", "append"]),
    }, ["path", "content"]),
    tags=FS_TAGS,
)
def edit_file(args: dict, ctx: ToolContext) -> str:
    path = need_path(args)
    if not path:
        return "缺少 path 参数。"
    if "content" not in args or args.get("content") is None:
        return "缺少内容参数 content。"
    content = str(args.get("content"))
    mode = str(args.get("mode") or "overwrite").strip().lower()

    if not os.path.exists(path):
        return "文件不存在：%s（新建请用 createFile）" % path
    if os.path.isdir(path):
        return "这是目录：%s" % path

    try:
        if is_binary_file(path):
            return "%s 是二进制文件，本工具只能改纯文本。" % path
        before = os.path.getsize(path)
    except OSError as e:
        return "读文件失败：%s" % e

    try:
        if mode == "append":
            with open(path, "a", encoding="utf-8", newline="") as f:
                f.write(content)
            action = "追加写入"
        else:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            action = "整体覆盖"
    except OSError as e:
        return "写入失败：%s" % e

    return "已%s：%s（%d → %d 字节）" % (action, path, before, os.path.getsize(path))


def _do_rename(args: dict, ctx: ToolContext) -> ToolResult:
    path = need_path(args)
    if not path:
        return ToolResult("缺少 path 参数。", ok=False)
    new_name = safe_new_name(args)
    if not new_name:
        return ToolResult("new_name 只能是名字，不要带路径：%s" % (args.get("new_name") or "（空）"), ok=False)

    if not os.path.exists(path):
        return ToolResult("路径不存在：%s" % path, ok=False)
    if is_root_like(path) or is_protected(path):
        return ToolResult("拒绝：不能重命名 %s" % path, ok=False)

    target = os.path.join(os.path.dirname(path), new_name)
    if os.path.abspath(target) == os.path.abspath(path):
        return ToolResult("新旧名字相同，什么都没做：%s" % path, ok=False)
    # POSIX 的 os.rename 遇到已存在目标是直接覆盖的，Windows 才报错。
    # 这里自己先查一遍，把两端行为统一成"绝不覆盖"。
    if os.path.exists(target):
        return ToolResult("目标已存在，不覆盖：%s" % target, ok=False)

    kind = "目录" if os.path.isdir(path) else "文件"
    try:
        _ORIG_RENAME(path, target)
    except OSError as e:
        return ToolResult("重命名失败：%s" % e, ok=False)
    return ToolResult("已重命名%s：%s → %s" % (kind, path, target))


# 上面那个函数一度直接叫 rename_path、里面调 os.rename，
# 会被装饰器结果覆盖掉。现在显式分离：工具名 renamePath / 实现 _do_rename。
rename_path = tool(
    "renamePath",
    (
        "重命名一个文件或目录（在它原来所在的目录里改名），只改名字不搬家。"
        "新名字已存在时不会覆盖，会报错。"
    ),
    params({
        "path": PATH_PROP,
        "new_name": string("新名字，只写名字，不要带路径，例如 notes.bak.txt"),
    }, ["path", "new_name"]),
    tags=FS_TAGS,
)(_do_rename)


# ------------------------------------------------------------------ 删除

@tool(
    "deleteFile",
    (
        "删除一个文件。执行前会请用户确认，用户不同意就不会删。"
        "用户明确要求删除某个文件时才调用。"
    ),
    params({"path": PATH_PROP}, ["path"]),
    danger=DangerLevel.DESTRUCTIVE,
    tags=FS_TAGS,
)
def delete_file(args: dict, ctx: ToolContext) -> ToolResult:
    path = need_path(args)
    if not path:
        return ToolResult("缺少 path 参数。", ok=False)
    if not os.path.exists(path):
        return ToolResult("文件不存在：%s" % path, ok=False)
    if os.path.isdir(path):
        return ToolResult("这是目录，删目录请用 deleteDir：%s" % path, ok=False)

    # 确认这一步交给 ctx.ask —— 终端是 y/N，Web 版可以是对话框
    if not ctx.ask("删除文件", "即将删除文件：\n%s" % path):
        return ToolResult("用户取消了删除：%s" % path, ok=False)

    try:
        os.remove(path)
    except OSError as e:
        return ToolResult("删除失败：%s" % e, ok=False)
    return ToolResult("已删除文件：%s" % path)


@tool(
    "deleteDir",
    (
        "删除一个目录，连同里面所有内容（包括非空目录）。"
        "执行前会请用户确认，用户不同意就不会删。"
    ),
    params({"path": PATH_PROP}, ["path"]),
    danger=DangerLevel.DESTRUCTIVE,
    tags=FS_TAGS,
)
def delete_dir(args: dict, ctx: ToolContext) -> ToolResult:
    path = need_path(args)
    if not path:
        return ToolResult("缺少 path 参数。", ok=False)
    if not os.path.exists(path):
        return ToolResult("路径不存在：%s" % path, ok=False)
    if not os.path.isdir(path):
        return ToolResult("这不是目录，删文件请用 deleteFile：%s" % path, ok=False)
    if is_root_like(path):
        return ToolResult("拒绝：不能删除盘根目录 %s" % path, ok=False)
    if is_protected(path):
        return ToolResult("拒绝：不能删除用户主目录 %s" % path, ok=False)

    files = sum(len(names) for _, _, names in os.walk(path))

    if not ctx.ask("删除目录", "即将删除目录（里面共 %d 个文件）：\n%s" % (files, path)):
        return ToolResult("用户取消了删除：%s" % path, ok=False)

    try:
        shutil.rmtree(path)
    except OSError as e:
        return ToolResult("删除失败：%s" % e, ok=False)
    return ToolResult("已删除目录：%s（连同其中 %d 个文件）" % (path, files))


ALL_TOOLS = [
    get_cwd, list_dir, get_file_type, read_file,
    create_file, create_dir, edit_file, rename_path,
    delete_dir, delete_file,
]

__all__ = ["ALL_TOOLS"]