# -*- coding: utf-8 -*-
"""本地命令执行工具：RunCli。

这一节课要讲清的东西全在这个文件里：

1. **参数列表 + shell=False**
   程序名和参数分开传，不拼成一条命令字符串。一旦拼字符串，shell 就会重新
   解释里面的 `&` `|` `>` `$()`；而参数是模型给的，天然不可信。用列表时
   `"; rm -rf /"` 只是传给程序的一个普通字符串，不会被当成命令分隔符。

2. **stdout / stderr / 退出码，三样一起回**
   新手最常犯的错是只看 stdout。很多命令失败时什么也不打印，
   答案全在 stderr 和退出码里。模型拿到三样才能判断"跑成了没有"。

3. **超时**
   命令行程序可能永远不返回：等输入、死循环、网络卡住。
   没有超时，整个 Agent 就卡死在这里，用户只能杀进程。

4. **截断**
   `npm install` 能刷几千行，全塞回模型就吃掉整个上下文窗口。
   所以砍中间、留两头：开头的用法/报错摘要和结尾的结论都还有用。

关于安全，本阶段的取舍是有意的、写明白的：
  这里**没有**白名单、没有黑名单、没有逐条审批。目的是让学生看清
  「模型 → tool_call → subprocess → 结果回填 → 模型决定下一步」这条完整链路。
  命令以运行 xiaoA 的那个用户的权限执行 —— 它能干的事，和这个人在终端里
  手打一样多，不会更多，也不会更少。
  真要上生产：把下面的 danger 改成 DangerLevel.DESTRUCTIVE（session.py 会自动
  走确认口，不用改这里），再在 build_argv 前面加一层白名单。两处改动而已。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..base import DangerLevel, ToolContext, ToolResult, integer, params, string, tool
from .fs_guard import decode_bytes

DEFAULT_TIMEOUT = 30.0        # 秒。够 git / npm / 编译一个小文件用
DEFAULT_MAX_CHARS = 4000      # stdout 与 stderr 各自的上限
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 600.0
MIN_CHARS = 200

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# cmd.exe 的内建命令：磁盘上没有对应的 exe，shell=False 一定找不到。
# 把这件事说清楚，比让模型看到一句 FileNotFoundError 强得多。
CMD_BUILTINS = {
    "dir", "del", "erase", "copy", "move", "ren", "rename", "type", "echo",
    "cd", "chdir", "cls", "set", "setx", "md", "mkdir", "rd", "rmdir",
    "attrib", "assoc", "ftype", "doskey", "pushd", "popd", "mklink",
    "start", "call", "pause", "title", "ver", "vol", "prompt", "color",
}


class CliError(Exception):
    """命令根本没跑起来（找不到程序、工作目录不对……）。消息可直接给模型看。"""


@dataclass
class CliRun:
    """一次执行的结果。字段就是给模型看的那几个，不多不少。"""

    argv: List[str]
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    out_total: int = 0          # 截断前的原始长度，用来告诉模型"省略了多少"
    err_total: int = 0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0

    @property
    def truncated(self) -> bool:
        return self.out_total > len(self.stdout) or self.err_total > len(self.stderr)


# --------------------------------------------------------------------- 1. 找程序


def check_command(command: str) -> str:
    """把模型给的程序名落成一个真实可执行文件的路径。失败抛 CliError。

    为什么必须先 which 一遍：
      subprocess 自己会去 PATH 里找，但找不到时只给一句
      `[WinError 2] 系统找不到指定的文件` —— 分不清是"程序没装"、
      "你把参数写进 command 了"、还是"这是 cmd 内建命令"。
      自己找一次，就能给出具体是哪种。
    """
    text = (command or "").strip()
    if not text:
        raise CliError('command 是空的。请传程序名，例如 "git"，参数放进 args。')

    # 模型偶尔会把整个可执行文件名用引号包起来
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()

    # 带路径分隔符的当文件处理，不带路径的交给 PATH
    looks_like_path = os.sep in text or "/" in text or (os.altsep and os.altsep in text)
    if looks_like_path:
        path = os.path.abspath(os.path.expanduser(os.path.expandvars(text)))
        if os.path.isfile(path):
            return path
        raise CliError("找不到这个文件：%s" % path)

    found = shutil.which(text)
    if found:
        return found

    raise CliError(_missing_hint(text))


def _missing_hint(text: str) -> str:
    """程序没找到时，按最可能的原因给一条具体建议。"""
    name = text.lower()

    # 最常见的错：把整条命令写进 command
    if " " in text:
        return ("command 里只能放程序名，不能带参数（收到的是「%s」）。"
                "请拆开：command 填「%s」，剩下的放进 args 数组，"
                "例如 {\"command\": \"%s\", \"args\": [\"%s\"]}。"
                % (text, text.split()[0], text.split()[0], text.split(None, 1)[1]))

    if name in CMD_BUILTINS:
        return ("「%s」是 cmd.exe 的内建命令，磁盘上没有它的 exe 文件，"
                "不经 shell 就调不起来。文件操作请改用 listDir / readFile / "
                "createFile / editFile 这些专用工具；确实要走 cmd 的话，"
                "把 command 设为 cmd.exe、args 设为 [\"/c\",\"%s\",...]，"
                "但要知道 cmd 会重新解析参数。" % (text, text))

    if name.endswith(".py"):
        return ("「%s」是 Python 脚本，不是可执行程序。请用 command=\"python\"、"
                "args=[\"%s\", ...] 来跑。" % (text, text))

    if name.endswith(".ps1"):
        return ("「%s」是 PowerShell 脚本。请用 command=\"powershell\"、"
                "args=[\"-NoProfile\",\"-File\",\"%s\", ...] 来跑。" % (text, text))

    return ("系统里找不到程序「%s」，PATH 里没有它。先在终端确认它装好了"
            "（Windows 用 where %s，其他系统用 which %s），或者改用带路径的完整写法。"
            % (text, text, text))


def build_argv(command: str, args: List[str]) -> List[str]:
    """程序名 + 参数，拼成 execve/CreateProcess 直接吃的列表。"""
    return [check_command(command)] + list(args)


def resolve_cwd(raw: str) -> Optional[str]:
    """工作目录。空字符串 = 用 xiaoA 自己所在的目录，交给 subprocess 处理。"""
    text = (raw or "").strip()
    if not text:
        return None
    path = os.path.abspath(os.path.expanduser(os.path.expandvars(text)))
    if not os.path.isdir(path):
        raise CliError("工作目录不存在：%s" % path)
    return path


# --------------------------------------------------------------------- 2. 执行


def run_cli(argv: List[str], timeout: float = DEFAULT_TIMEOUT,
            cwd: Optional[str] = None, max_chars: int = DEFAULT_MAX_CHARS) -> CliRun:
    """跑起来。只有"根本没启动"才抛 CliError，超时和失败都算正常结果。"""
    try:
        proc = subprocess.run(
            argv,
            shell=False,                    # 显式写出来：这是本课的重点，不是默认值
            stdin=subprocess.DEVNULL,       # 没有人在键盘前，等输入的命令会挂到超时
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            cwd=cwd or None,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as e:
        # Python 已经把子进程杀了；它顺带收回了死之前吐出来的那部分输出，
        # 这些残留往往正是排查"卡在哪一步"的线索，别丢。
        run = CliRun(argv=list(argv), timed_out=True,
                     stdout=_clean(decode_bytes(e.stdout or b"")),
                     stderr=_clean(decode_bytes(e.stderr or b"")))
        return _truncate(run, max_chars)
    except FileNotFoundError as e:
        # 走到这里通常是 cwd 不存在，或者文件在 which 之后被删了
        raise CliError("启动失败：可执行文件或工作目录不存在（%s）" % e)
    except OSError as e:
        raise CliError("启动失败：%s" % e)
    except subprocess.SubprocessError as e:
        raise CliError("子进程异常：%s" % e)

    run = CliRun(
        argv=list(argv),
        returncode=proc.returncode,
        stdout=_clean(decode_bytes(proc.stdout or b"")),
        stderr=_clean(decode_bytes(proc.stderr or b"")),
    )
    return _truncate(run, max_chars)


def _clean(text: str) -> str:
    """统一换行。Windows 的 \\r\\n 和进度条的 \\r 在终端里都会显示成怪东西。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def truncate_middle(text: str, limit: int) -> Tuple[str, int]:
    """超长就砍中间、留两头。返回 (结果, 被省略的字符数)。

    为什么不是只留开头：命令的关键信息经常在**结尾**（测试失败的汇总、
    编译的 error 数、`git status` 的统计行）。只砍尾部会把最有用的部分砍掉。
    """
    limit = max(MIN_CHARS, int(limit or DEFAULT_MAX_CHARS))
    if len(text) <= limit:
        return text, 0
    # 给"中间省略"那行留 40 字符，保证最终长度不超过 limit
    keep = max(40, limit - 40)
    head = int(keep * 0.6)
    tail = keep - head
    removed = len(text) - keep
    marker = "\n…（中间省略 %d 字符）…\n" % removed
    return text[:head] + marker + text[-tail:], removed


def _truncate(run: CliRun, max_chars: int) -> CliRun:
    """先记原始长度再截断 —— 顺序反了就没法告诉模型"省掉了多少"。"""
    run.out_total = len(run.stdout)
    run.err_total = len(run.stderr)
    run.stdout, _ = truncate_middle(run.stdout, max_chars)
    run.stderr, _ = truncate_middle(run.stderr, max_chars)
    return run


# --------------------------------------------------------------------- 3. 排版


def format_result(run: CliRun, timeout: float = DEFAULT_TIMEOUT) -> str:
    """拼成给模型读的文本：命令 → 退出码 → stdout → stderr。"""
    lines = ["【命令】%s" % _display(run.argv)]

    if run.timed_out:
        lines.append("【结果】超时：超过 %g 秒还没结束，已被强制终止。" % timeout)
        lines.append("下面是终止前已经产生的输出，可能不完整。")
    else:
        lines.append("【退出码】%d%s" % (run.returncode, _exit_hint(run.returncode)))

    lines.append(_stream("stdout", run.stdout, run.out_total))
    lines.append(_stream("stderr", run.stderr, run.err_total))

    if run.timed_out:
        lines.append("需要更久就把 timeout 调大（上限 %g 秒），或者把任务拆小分几次跑。"
                     % MAX_TIMEOUT)
    elif run.returncode != 0:
        lines.append("退出码不为 0 说明这条命令没有正常完成，原因通常写在 stderr 里；"
                     "照原样重试一次通常会得到同样的结果，先看原因再决定下一步。")

    return "\n".join(lines)


def _exit_hint(code: int) -> str:
    if code == 0:
        return "（成功）"
    if code < 0:
        # POSIX 下被信号杀掉是负数；Windows 也可能出现很大的无符号异常码
        return "（被系统信号终止）"
    return "（失败）"


def _stream(name: str, text: str, total: int) -> str:
    if not text:
        return "【%s】（空）" % name
    if total > len(text):
        head = "【%s】共 %d 字符，已截断到 %d 字符" % (name, total, len(text))
    else:
        head = "【%s】共 %d 字符" % (name, total)
    return head + "\n" + text.rstrip("\n")


def _display(argv: List[str]) -> str:
    """展示用的命令行。list2cmdline 会把带空格的参数加引号，和真正执行的一致。"""
    try:
        text = subprocess.list2cmdline(argv)
    except Exception:      # noqa: BLE001 —— 纯展示，出错也不该影响结果
        text = " ".join(argv)
    return text if len(text) <= 300 else text[:300] + " …"


# --------------------------------------------------------------------- 工具声明


def _parse_args_field(raw: Any) -> Tuple[List[str], str]:
    """校验 args。返回 (参数列表, 错误说明)，错误说明非空表示不合法。"""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return [], ""

    if isinstance(raw, str):
        return [], (
            "args 必须是字符串数组，不要写成一条命令字符串。"
            "例如 {\"command\": \"git\", \"args\": [\"status\", \"--short\"]}；"
            "收到的是「%s」。" % raw)

    if not isinstance(raw, (list, tuple)):
        return [], ("args 必须是字符串数组，收到的是 %s。"
                    % type(raw).__name__)

    result: List[str] = []
    for index, item in enumerate(raw, 1):
        if isinstance(item, bool):
            return [], ("args 第 %d 项是布尔值，命令行参数只能是字符串。"
                        "要开关就写 \"--flag\"，不要写 true/false。" % index)
        if isinstance(item, (int, float)):
            # 数字很常见（-n 5），转成字符串就好，没必要为难模型
            result.append(str(item))
            continue
        if isinstance(item, str):
            result.append(item)
            continue
        return [], ("args 第 %d 项不是字符串（是 %s）。参数必须逐个写成字符串。"
                    % (index, type(item).__name__))
    return result, ""


def _arg_float(raw: Any, default: float) -> float:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


@tool(
    name="RunCli",
    description=(
        "执行本机已安装的命令行程序（git、npm、python、node、ffmpeg、ipconfig 等），"
        "返回 stdout、stderr 和退出码。\n"
        "命令按参数列表执行，不经过 shell：程序名放 command，其余逐个放进 args 数组。\n"
        "需要知道某个程序装没装、版本是多少，或者跑脚本、跑构建、看系统信息时用它。\n"
        "读文件、列目录、写文件请用专门的文件工具，不要用 dir / type / copy —— "
        "那些是 cmd 的内建命令，不是独立程序，调不起来。"
    ),
    parameters=params(
        {
            "command": string(
                "程序名，只写程序本身，例如 git、python、node、npm、curl。"
                "不要带参数，不要写整条命令。"),
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "参数列表，逐个字符串，例如 [\"status\", \"--short\"]。"
                    "每个参数单独一项，不要把多个参数塞进一个字符串。"
                    "路径含空格也没关系，不用自己加引号。"),
            },
            "cwd": string(
                "在哪个目录下执行，绝对路径。省略则在 xiaoA 启动时的目录执行。"
                "跑 git、npm 这类依赖当前目录的命令时建议显式指定。"),
            "timeout": integer("本次执行的超时秒数，默认 30，最大 600。"),
        },
        required=["command"],
    ),
    danger=DangerLevel.SAFE,       # 本阶段不设审批，见文件头的说明
    tags=["cli", "shell", "system"],
)
def run_cli_tool(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    raw_command = args.get("command")
    command = str(raw_command).strip() if raw_command is not None else ""
    if not command:
        return ToolResult(
            'RunCli 需要一个 command 参数，例如 {"command": "git", "args": ["status"]}。'
            "请重新调用。", ok=False)

    arg_list, problem = _parse_args_field(args.get("args"))
    if problem:
        return ToolResult(problem, ok=False)

    timeout = _arg_float(args.get("timeout"), ctx.cli_timeout or DEFAULT_TIMEOUT)
    timeout = max(MIN_TIMEOUT, min(timeout, MAX_TIMEOUT))
    max_chars = int(ctx.cli_max_chars or DEFAULT_MAX_CHARS)

    try:
        argv = build_argv(command, arg_list)
        cwd = resolve_cwd(str(args.get("cwd") or ""))
        run = run_cli(argv, timeout=timeout, cwd=cwd, max_chars=max_chars)
    except CliError as e:
        return ToolResult("RunCli 无法执行：%s" % e, ok=False)
    except Exception as e:      # noqa: BLE001 —— 兜底，别让整轮对话炸掉
        return ToolResult("RunCli 出现未预期的错误（%s）：%s"
                          % (type(e).__name__, e), ok=False)

    return ToolResult(format_result(run, timeout), ok=run.ok)


ALL_TOOLS = [run_cli_tool]

__all__ = [
    "ALL_TOOLS",
    "run_cli_tool",
    "run_cli",
    "build_argv",
    "check_command",
    "resolve_cwd",
    "format_result",
    "truncate_middle",
    "CliError",
    "CliRun",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_TIMEOUT",
    "MAX_TIMEOUT",
]
