# -*- coding: utf-8 -*-
"""命令行聊天程序 + 本地文件操作工具组，调用 DeepSeek 的 OpenAI 兼容接口。

课题三：在 chat-stream-tool-isLeapYear.py 的基础上，加一组文件操作工具。
全部用 Python 标准库实现（os / shutil / mimetypes），不依赖 shell 命令，
这样在 Windows 和 Linux 上行为一致。

十二个工具：
  时间类   getDate         调本机 date 命令取当前日期时间
  计算类   isLeapYear      Python 代码判断闰年
  文件类   getCwd          取当前工作目录
           listDir         列出目录下的文件和子目录
           getFileType     判断文件类型（扩展名 / MIME / 是否纯文本）
           readFile        读纯文本文件内容
           createFile      在指定目录下新建文件
           editFile        改纯文本文件内容（整体覆盖或追加）
           createDir       创建目录（可多级）
           renamePath      重命名文件或目录
           deleteDir       删除目录（含非空）
           deleteFile      删除文件

关于删除：模型自己决定不了"要不要删"，所以 deleteDir / deleteFile
执行前会停下来问一句 [y/N]，默认不动手。用户按 Ctrl+C 依然能干净退出。
重命名不做确认——改名是可逆的，改回去就行；而且目标已存在时一律拒绝，不覆盖。
"""

import configparser
import http.client
import json
import mimetypes
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.parse

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

# 每个字之间的停顿，单位秒。想快就调小（比如 0.01），想更慢就调大。
TYPE_DELAY = 0.03

# 发送历史的上限（粗略按字符数估算，不引第三方 tokenizer）。
MAX_HISTORY_CHARS = 40000

# 一次提问最多允许模型连续调几轮工具，防止它死循环。
# 现在工具多了，模型可能"看一眼目录 -> 读文件 -> 再改"，给到 8 轮。
MAX_TOOL_ROUNDS = 8

# 单次 readFile 最多返回多少字符，防止一个几十万行的日志冲爆上下文。
MAX_READ_CHARS = 20000

# 让 date 命令输出的格式：年-月-日 时:分:秒 星期
DATE_FORMAT = "+%Y-%m-%d %H:%M:%S %A"

# 系统提示：工具多了以后，得告诉模型"别猜，去查"。
SYSTEM_PROMPT = (
    "你是一个运行在用户本机的命令行助手，可以调用工具查看和修改本地文件。"
    "凡是涉及具体文件、目录的问题，都要先用工具查证（getCwd、listDir、readFile），"
    "不要凭猜测编造路径或文件内容。调用文件工具时传绝对路径。"
    "删除操作会请用户现场确认，用户取消就如实说明，不要换个路径再试。"
)

# 告诉模型我们有哪些工具。description 写清楚"什么时候该用我"，
# 参数 schema 写清楚"参数长什么样"，模型才知道怎么填。
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "getDate",
            "description": (
                "获取当前电脑的日期和时间。"
                "当用户问现在几点、今天几号、今天星期几、今年是哪一年这类问题时调用。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "isLeapYear",
            "description": (
                "判断指定年份是不是闰年。"
                "当用户问某一年是不是闰年、某年二月有几天、某年有没有 2 月 29 日时调用。"
                "用户说「今年」时，先用 getDate 拿到当前年份，再调本工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "要判断的年份，四位数字，例如 2024",
                    }
                },
                "required": ["year"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "getCwd",
            "description": "获取当前工作目录的绝对路径。用户问「现在在哪个目录」时调用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listDir",
            "description": (
                "列出指定目录下的所有文件和子目录，会带上文件大小。"
                "用户想看某个目录里有什么、想知道某个文件在不在时调用。"
                "不传 path 就列当前工作目录。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要列出的目录绝对路径；留空表示当前工作目录",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "getFileType",
            "description": (
                "判断一个路径是什么类型：是目录还是文件、扩展名是什么、"
                "MIME 类型是什么、内容是纯文本还是二进制。"
                "在决定能不能读或改写一个文件之前，先调这个确认。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或目录的绝对路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "readFile",
            "description": (
                "读取一个纯文本文件的内容。只能读文本文件，二进制文件会被拒绝。"
                "文件太长时只返回前面一部分。用户让你看看某个文件写了什么时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要读的文件的绝对路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "createFile",
            "description": (
                "在指定目录下新建一个文本文件并写入内容。"
                "同名文件已存在时不会覆盖，会报错让你改用 editFile。"
                "用户让你新建文件时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dir": {"type": "string", "description": "文件所在目录的绝对路径"},
                    "name": {"type": "string", "description": "文件名，只写名字，不要带路径"},
                    "content": {"type": "string", "description": "文件内容，纯文本"},
                },
                "required": ["dir", "name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "editFile",
            "description": (
                "修改一个已存在的纯文本文件的内容。mode=overwrite 整体覆盖（默认），"
                "mode=append 追加到末尾。只能改文本文件。"
                "用户让你改文件内容、或在文件末尾加东西时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要修改的文件的绝对路径"},
                    "content": {"type": "string", "description": "要写入的纯文本内容"},
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "overwrite 覆盖全文，append 追加到末尾",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "createDir",
            "description": (
                "创建一个目录，父目录不存在会一起创建（相当于 mkdir -p）。"
                "目录已存在时直接返回成功。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要创建的目录绝对路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "renamePath",
            "description": (
                "重命名一个文件或目录（在它原来所在的目录里改名）。"
                "只改名字，不跨目录搬家。新名字已存在时不会覆盖，会报错。"
                "用户说「把 xxx 改名为 yyy」时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要重命名的文件或目录的绝对路径",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "新名字，只写名字，不要带路径，例如 notes.bak.txt",
                    },
                },
                "required": ["path", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deleteDir",
            "description": (
                "删除一个目录，连同里面所有内容（包括非空目录）。"
                "执行前会请用户确认，用户不同意就不会删。"
                "用户明确要求删除某个目录时才调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的目录绝对路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deleteFile",
            "description": (
                "删除一个文件。执行前会请用户确认，用户不同意就不会删。"
                "用户明确要求删除某个文件时才调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "要删除的文件绝对路径"}
                },
                "required": ["path"],
            },
        },
    },
]


# ---------------------------------------------------------------- 基础工具函数

def trim_history(messages, max_chars=MAX_HISTORY_CHARS):
    """历史太长时，从最早的一轮开始丢，保证不超上限。

    返回 (裁剪后的历史, 丢掉的轮数)。
    因为现在多了 role=tool 的消息，不能简单两条两条地丢——那样可能把
    工具结果留下、却把发起调用的 assistant 消息丢掉，接口会直接报错。
    所以这里统一做法：切到下一条 user 消息的位置，整组一起丢。
    """
    def total(msgs):
        return sum(len(m.get("content") or "") for m in msgs)

    if total(messages) <= max_chars:
        return messages, 0

    kept = list(messages)
    dropped = 0
    while len(kept) > 1 and total(kept) > max_chars:
        cut = None
        for i in range(1, len(kept)):
            if kept[i].get("role") == "user":
                cut = i
                break
        if cut is None:
            # 只剩当前这一轮了，再切就没东西可发
            break
        kept = kept[cut:]
        dropped += 1

    return kept, dropped


def load_config(path=CONFIG_PATH):
    """读配置文件，返回 (api_key, base_url, model)。"""
    if not os.path.exists(path):
        print("找不到 config.ini。")
        print("请把 ..\\Practice01\\config.ini 复制一份到本目录，或者照着下面新建：")
        print("    [deepseek]")
        print("    api_key = 你的key")
        print("    base_url = https://api.deepseek.com")
        print("    model = deepseek-chat")
        sys.exit(1)

    cfg = configparser.ConfigParser()
    # 保留 key 里可能出现的大小写（默认会把选项名转成小写，这里关掉更保险）
    cfg.optionxform = str
    cfg.read(path, encoding="utf-8")

    if not cfg.has_section("deepseek"):
        print("config.ini 里缺少 [deepseek] 段，请参考 config.example.ini。")
        sys.exit(1)

    api_key = cfg.get("deepseek", "api_key", fallback="").strip()
    base_url = cfg.get("deepseek", "base_url", fallback="https://api.deepseek.com").strip()
    model = cfg.get("deepseek", "model", fallback="deepseek-chat").strip()

    if not api_key or api_key == "你的key":
        print("config.ini 里的 api_key 还没填，请填入你自己的 DeepSeek API Key。")
        sys.exit(1)

    return api_key, base_url.rstrip("/"), model


def parse_base_url(base_url):
    """把 https://api.deepseek.com 拆成 (主机, 端口, 是否 https)。"""
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname
    if not host:
        host = "api.deepseek.com"
    if parts.port:
        port = parts.port
    else:
        port = 443 if parts.scheme != "http" else 80
    return host, port, parts.scheme != "http"


def type_out(text):
    """把一段文字一个字一个字地打在屏幕上，模拟打字机。"""
    for i, ch in enumerate(text):
        sys.stdout.write(ch)
        sys.stdout.flush()
        # 停顿只在"下一个字不是换行"时发生。
        # 这样换行符前后都不拖沓，每行末尾不会莫名卡一下。
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch != "\n" and nxt != "\n":
            time.sleep(TYPE_DELAY)


def confirm(question):
    """危险操作前问一句，返回 True 才真的动手。

    只有读到 EOF（比如输出被重定向、没有输入源）才当成"No"。
    Ctrl+C 不拦，让它照常往上抛，交给 main 干净退出。
    """
    sys.stdout.write(question + " [y/N] ")
    sys.stdout.flush()
    try:
        answer = input()
    except EOFError:
        print()
        return False
    return answer.strip().lower() in ("y", "yes")


# ---------------------------------------------------------------- getDate 工具

def decode_bytes(raw):
    """命令输出/文件内容可能是 GBK，挨个编码试着解。"""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def find_date_command():
    """找一条能直接执行的 date 命令，返回命令列表；找不到返回 None。

    Windows 上的 date 是 cmd 的内建命令，而且不带参数时会要你"输入新日期"，
    直接调用就卡在那儿等输入了。所以要绕开它，按下面的顺序找：
      1. PATH 里的 date（Linux / macOS / Git Bash / Cygwin 都有）
      2. Git 自带的 date.exe（由 git.exe 的位置反推：<Git根>/usr/bin/date.exe）
      3. PowerShell 的 Get-Date（同样是调系统命令，不是 Python 自己算的）
    """
    exe = shutil.which("date")
    if exe:
        return [exe, DATE_FORMAT]

    if os.name == "nt":
        git = shutil.which("git")
        if git:
            # git.exe 通常在 <Git根>/cmd 下，date.exe 在 <Git根>/usr/bin 下
            root = os.path.dirname(os.path.dirname(os.path.abspath(git)))
            for sub in ("usr/bin/date.exe", "mingw64/bin/date.exe", "bin/date.exe"):
                path = os.path.join(root, *sub.split("/"))
                if os.path.exists(path):
                    return [path, DATE_FORMAT]

        ps = shutil.which("powershell") or shutil.which("pwsh")
        if ps:
            return [ps, "-NoProfile", "-Command",
                    "Get-Date -Format 'yyyy-MM-dd HH:mm:ss dddd'"]

    return None


def run_date_command():
    """执行 date 命令，把它的输出当工具结果返回。"""
    cmd = find_date_command()
    if not cmd:
        return "本机找不到可用的 date 命令，拿不到时间。"

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            # Windows 下别弹黑框
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return "执行 date 命令失败：%s" % e

    out = decode_bytes(proc.stdout).strip()
    if proc.returncode != 0 or not out:
        err = decode_bytes(proc.stderr).strip()
        return "date 命令没有返回结果（退出码 %s）。%s" % (proc.returncode, err)

    return "当前系统日期时间（本机 date 命令的输出）：%s" % out


# ---------------------------------------------------------------- isLeapYear 工具

def leap_reason(year):
    """用一句话说明这一年为什么是/不是闰年，方便模型解释给用户听。"""
    if year % 400 == 0:
        return "能被 400 整除"
    if year % 100 == 0:
        return "能被 100 整除，但不能被 400 整除"
    if year % 4 == 0:
        return "能被 4 整除，且不能被 100 整除"
    return "不能被 4 整除"


def is_leap_year(year):
    """闰年判定，纯 Python 算，不用任何命令。

    规则（公历）：
      1. 能被 400 整除 → 闰年
      2. 能被 100 整除但不能被 400 整除 → 平年
      3. 能被 4 整除 → 闰年
      4. 其余 → 平年
    顺序很重要：先判 400 再判 100，否则 2000 会被错判成平年。
    """
    if year % 400 == 0:
        return True
    if year % 100 == 0:
        return False
    return year % 4 == 0


def parse_year(args_text):
    """从模型给的 arguments 里取出 year，取不到返回 None。

    arguments 是模型拼的 JSON 字符串，可能长这样：{"year": 2024}
    也可能只剩个裸数字（少数实现会这样），所以两种都试。
    """
    try:
        args = json.loads(args_text or "{}")
    except json.JSONDecodeError:
        args = None

    if isinstance(args, dict):
        raw = args.get("year")
    elif isinstance(args, (int, float)):
        raw = args
    else:
        raw = args_text

    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def run_is_leap_year(args_text):
    """isLeapYear 的实际执行体。"""
    year = parse_year(args_text)
    if year is None:
        return "没拿到有效的年份参数，请在 arguments 里提供 year，例如 {\"year\": 2024}。"

    if not 1 <= year <= 9999:
        return "%d 不是一个合理的公历年份，请给 1 到 9999 之间的整数。" % year

    if is_leap_year(year):
        return "%d 年是闰年（%s），2 月有 29 天，全年 366 天。" % (
            year, leap_reason(year))
    return "%d 年不是闰年（%s），2 月有 28 天，全年 365 天。" % (
        year, leap_reason(year))


# ---------------------------------------------------------------- 文件工具的公共零件

def resolve_path(raw):
    """把模型给的路径变成绝对路径。支持 ~、%VAR%、相对路径（相对当前工作目录）。"""
    text = str(raw).strip()
    text = os.path.expanduser(os.path.expandvars(text))
    return os.path.abspath(text)


def need_path(args):
    """取必填的 path 参数。空的话返回 None，让调用方拒绝执行。

    这一层不能省：模型偶尔会漏参数，如果空路径被 abspath("") 变成当前目录，
    deleteDir 就可能把整个工作目录删了。
    """
    raw = str(args.get("path") or "").strip()
    return resolve_path(raw) if raw else None


def is_binary_file(path):
    """靠前 8KB 里有没有 \\x00 判断是不是二进制。文本文件不会有空字节。"""
    with open(path, "rb") as f:
        return b"\x00" in f.read(8192)


def human_size(size):
    """把字节数变成 12.3 KB 这种好看的形式。"""
    if size < 1024:
        return "%d B" % size
    value = float(size)
    for unit in ("KB", "MB", "GB"):
        value /= 1024.0
        if value < 1024:
            return "%.1f %s" % (value, unit)
    return "%.1f TB" % (value / 1024.0)


def is_root_like(path):
    """判断是不是盘根（C:\\ 或 /）。这种路径一律不许删。"""
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    return parent == path or parent == ""


def is_protected(path):
    """再拦一道：用户主目录不许删。"""
    home = os.path.abspath(os.path.expanduser("~"))
    return os.path.abspath(path).rstrip("\\/").lower() == home.rstrip("\\/").lower()


# ---------------------------------------------------------------- 文件工具本体

def tool_get_cwd(args):
    return "当前工作目录：%s" % os.getcwd()


def tool_list_dir(args):
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

    dirs = []
    files = []
    for name in names:
        full = os.path.join(path, name)
        if os.path.isdir(full):
            dirs.append(name)
        else:
            try:
                size = human_size(os.path.getsize(full))
            except OSError:
                size = "?"
            files.append("%s  (%s)" % (name, size))

    lines.append("共 %d 项：%d 个目录，%d 个文件" % (len(names), len(dirs), len(files)))
    if dirs:
        lines.append("[目录]")
        lines.extend("  %s/" % n for n in dirs)
    if files:
        lines.append("[文件]")
        lines.extend("  " + n for n in files)
    return "\n".join(lines)


def tool_get_file_type(args):
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


def tool_read_file(args):
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
    note = ""
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS]
        note = "\n...（文件太长，只返回前 %d 个字符）" % MAX_READ_CHARS
    return "文件：%s\n--- 内容开始 ---\n%s%s\n--- 内容结束 ---" % (path, text, note)


def tool_create_file(args):
    directory = resolve_path(args.get("dir") or ".")
    name = str(args.get("name") or "").strip()
    content = args.get("content") or ""

    if not name:
        return "缺少文件名参数 name。"
    # 只允许纯文件名，避免模型把 "../xx" 拼进来
    if name in (".", "..") or name != os.path.basename(name):
        return "name 只能是文件名，不要带路径：%s" % name
    if not os.path.isdir(directory):
        return "目录不存在：%s（要新建目录请用 createDir）" % directory

    path = os.path.join(directory, name)
    if os.path.exists(path):
        return "文件已存在：%s。本工具不覆盖，要改内容请用 editFile。" % path

    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    except OSError as e:
        return "创建失败：%s" % e
    return "已创建：%s（%d 个字符）" % (path, len(content))


def tool_edit_file(args):
    path = need_path(args)
    if not path:
        return "缺少 path 参数。"
    content = args.get("content")
    mode = str(args.get("mode") or "overwrite").strip().lower()

    if content is None:
        return "缺少内容参数 content。"
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

    after = os.path.getsize(path)
    return "已%s：%s（%d → %d 字节）" % (action, path, before, after)


def tool_create_dir(args):
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


def tool_rename_path(args):
    """重命名文件或目录。只允许在同目录内改名，不搬家。"""
    path = need_path(args)
    if not path:
        return "缺少 path 参数。"

    new_name = str(args.get("new_name") or "").strip()
    if not new_name:
        return "缺少新名字参数 new_name。"
    # 名字里带分隔符就等于偷偷搬家了，这里直接拦掉
    if new_name in (".", "..") or new_name != os.path.basename(new_name):
        return "new_name 只能是名字，不要带路径：%s" % new_name

    if not os.path.exists(path):
        return "路径不存在：%s" % path
    if is_root_like(path) or is_protected(path):
        return "拒绝：不能重命名 %s" % path

    target = os.path.join(os.path.dirname(path), new_name)
    if os.path.abspath(target) == os.path.abspath(path):
        return "新旧名字相同，什么都没做：%s" % path
    # 必须先自己查一遍：POSIX 的 os.rename 遇到已存在的目标是直接覆盖的，
    # Windows 才会报错。统一行为，一律不覆盖。
    if os.path.exists(target):
        return "目标已存在，不覆盖：%s" % target

    kind = "目录" if os.path.isdir(path) else "文件"
    try:
        os.rename(path, target)
    except OSError as e:
        return "重命名失败：%s" % e
    return "已重命名%s：%s → %s" % (kind, path, target)


def tool_delete_file(args):
    path = need_path(args)
    if not path:
        return "缺少 path 参数。"
    if not os.path.exists(path):
        return "文件不存在：%s" % path
    if os.path.isdir(path):
        return "这是目录，删目录请用 deleteDir：%s" % path

    if not confirm("即将删除文件：%s\n确认删除吗？" % path):
        return "用户取消了删除：%s" % path

    try:
        os.remove(path)
    except OSError as e:
        return "删除失败：%s" % e
    return "已删除文件：%s" % path


def tool_delete_dir(args):
    path = need_path(args)
    if not path:
        return "缺少 path 参数。"
    if not os.path.exists(path):
        return "路径不存在：%s" % path
    if not os.path.isdir(path):
        return "这不是目录，删文件请用 deleteFile：%s" % path
    if is_root_like(path):
        return "拒绝：不能删除盘根目录 %s" % path
    if is_protected(path):
        return "拒绝：不能删除用户主目录 %s" % path

    files = 0
    for _, _, names in os.walk(path):
        files += len(names)

    if not confirm("即将删除目录（里面共 %d 个文件）：%s\n确认删除吗？" % (files, path)):
        return "用户取消了删除：%s" % path

    try:
        shutil.rmtree(path)
    except OSError as e:
        return "删除失败：%s" % e
    return "已删除目录：%s（连同其中 %d 个文件）" % (path, files)


# ---------------------------------------------------------------- 工具分发

FILE_TOOLS = {
    "getCwd": tool_get_cwd,
    "listDir": tool_list_dir,
    "getFileType": tool_get_file_type,
    "readFile": tool_read_file,
    "createFile": tool_create_file,
    "editFile": tool_edit_file,
    "createDir": tool_create_dir,
    "renamePath": tool_rename_path,
    "deleteDir": tool_delete_dir,
    "deleteFile": tool_delete_file,
}


def parse_args(args_text):
    """把模型给的 arguments 字符串拆成 dict，拆不动就给个空 dict。"""
    try:
        args = json.loads(args_text or "{}")
    except json.JSONDecodeError:
        return {}
    return args if isinstance(args, dict) else {}


def run_tool(name, args_text):
    """按名字执行工具。新增工具时，往 FILE_TOOLS 里加一行就行。"""
    if name == "getDate":
        return run_date_command()
    if name == "isLeapYear":
        return run_is_leap_year(args_text)

    handler = FILE_TOOLS.get(name)
    if handler is None:
        return "没有这个工具：%s" % name

    try:
        return handler(parse_args(args_text))
    except Exception as e:
        # 工具自己出错（权限、路径非法之类）不该把整个对话搞崩，
        # 把错误当结果回传给模型，让它自己决定怎么跟用户解释。
        return "工具 %s 执行出错：%s" % (name, e)


# ---------------------------------------------------------------- 与接口交互

def stream_chat(api_key, base_url, model, messages):
    """发一轮请求，边收边打印。

    返回 assistant 消息 dict（可能带 tool_calls），失败返回 None。
    注意这里返回的是"整条消息"，因为下一步要把它原样塞回 messages 里，
    模型才知道自己刚才发起过工具调用。
    """
    host, port, use_tls = parse_base_url(base_url)

    # system 提示只在发请求时加在最前面，不进 messages，
    # 这样历史裁剪逻辑不用考虑它
    send_list = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    payload = json.dumps({
        "model": model,
        "messages": send_list,
        "stream": True,
        "tools": TOOLS,
        "tool_choice": "auto",
    }, ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
        "Accept": "text/event-stream",   # 明确告诉服务端我们要流
        "Content-Length": str(len(payload)),
    }

    # 有的环境证书链不全，这里放宽一下，避免直接卡在 SSL 上
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    if use_tls:
        conn = http.client.HTTPSConnection(host, port, context=context, timeout=300)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=300)

    full_text = ""
    tool_calls = []      # 按 index 累积，因为分片不保证一次给全
    started = False      # 真正有字要打时，才打印 "AI: " 前缀

    try:
        conn.request("POST", "/chat/completions", body=payload, headers=headers)
        response = conn.getresponse()

        if response.status != 200:
            detail = response.read().decode("utf-8", errors="ignore")
            if response.status == 400 and "context" in detail.lower():
                print("\n[出错] 对话太长，超出模型上下文上限了。")
                print("        输入 exit 重开程序，或把 MAX_HISTORY_CHARS 调小一点。")
            else:
                print("\n[出错] 接口返回 %s：%s" % (response.status, detail))
            return None

        # 关键点：不要用 response.read() 或 for 迭代。
        # readline() 一旦凑齐一行就立刻返回，配合下面逐块 flush，
        # 才能真正做出打字机效果。
        while True:
            raw_line = response.readline()
            if not raw_line:
                break

            line = raw_line.decode("utf-8", errors="ignore").strip()

            # SSE 只会用到 data: 开头的行，其它（空行、event:、心跳）忽略
            if not line.startswith("data:"):
                continue

            chunk = line[len("data:"):].strip()

            # [DONE] 表示这一轮回复结束
            if chunk == "[DONE]":
                break

            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue

            choices = obj.get("choices") or []
            if not choices:
                continue

            delta = choices[0].get("delta") or {}

            # 1) 正文：边收边打字
            piece = delta.get("content")
            if piece:
                if not started:
                    sys.stdout.write("AI: ")
                    sys.stdout.flush()
                    started = True
                full_text += piece
                # 服务端一次可能推来好几个字，这里拆成单字逐个吐，
                # 才会是"一个字一个字"的打字机效果。
                type_out(piece)

            # 2) 工具调用：分片推来，按 index 拼
            for tc in delta.get("tool_calls") or []:
                index = tc.get("index", 0)
                while len(tool_calls) <= index:
                    tool_calls.append({
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    })
                slot = tool_calls[index]

                if tc.get("id"):
                    slot["id"] = tc["id"]

                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]

    except (OSError, http.client.HTTPException) as e:
        # OSError 覆盖了连不上、超时、SSL 出错等一堆情况；HTTPException 是协议层错误
        print("\n[出错] 请求失败：%s" % e)
        return None
    finally:
        conn.close()

    if started:
        print()
        sys.stdout.flush()

    message = {"role": "assistant", "content": full_text}
    if tool_calls:
        # 万一没拿到 id（少数兼容实现会省），补一个，免得接口拒收
        for i, call in enumerate(tool_calls):
            if not call["id"]:
                call["id"] = "call_%d" % i
        message["tool_calls"] = tool_calls

    return message


def chat_with_tools(api_key, base_url, model, messages):
    """处理一整轮用户提问：可能要来回调几次工具，才拿到最终回答。

    成功返回 True（messages 已补全），这一轮失败返回 False。
    """
    for _ in range(MAX_TOOL_ROUNDS):
        send_messages, dropped = trim_history(list(messages))
        if dropped:
            print("[提示] 对话太长，已自动省略最早的 %d 轮历史。" % dropped)
            messages[:] = send_messages

        reply = stream_chat(api_key, base_url, model, send_messages)
        if reply is None:
            return False

        # 不管有没有工具调用，assistant 这条消息都要记进历史
        messages.append(reply)

        calls = reply.get("tool_calls")
        if not calls:
            return True          # 没有工具调用，回答已经打完

        # 模型要用工具：本地执行，把结果以 role=tool 回传，再去要一次回答
        for call in calls:
            name = call["function"]["name"]
            args_text = call["function"]["arguments"] or "{}"
            print("[工具] %s(%s)" % (name, args_text))
            result = run_tool(name, args_text)
            print("[结果] %s" % result.replace("\n", "\n       "))
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result,
            })

    print("\n[提示] 工具连着调了 %d 轮还没结束，先停在这里。" % MAX_TOOL_ROUNDS)
    return True


def main():
    # 让标准输出变成行缓冲，逐字输出才不会卡在缓冲区里。
    # Windows 下终端默认是块缓冲，这一行是打字机效果的关键之一。
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    api_key, base_url, model = load_config()
    print("已启动，API 地址: %s" % base_url)
    print("模型: %s" % model)
    print("当前工作目录: %s" % os.getcwd())
    print("可用工具:")
    print("  时间/计算  getDate  isLeapYear")
    print("  文件操作   getCwd  listDir  getFileType  readFile  createFile")
    print("             editFile  createDir  renamePath  deleteDir  deleteFile")
    print("  （deleteDir / deleteFile 执行前会问你 y/N，默认不删）")
    print("输入 exit 或 quit 退出，或随时按 Ctrl+C 强行结束")
    print("-" * 40)

    messages = []

    # 整个对话过程包在 try 里，这样不管在哪一步按 Ctrl+C
    # （等你输入时、AI 正在打字时、甚至 y/N 确认时）都能干净退出。
    try:
        while True:
            try:
                user_input = input("\n你: ").strip()
            except EOFError:
                print("\n输入结束，再见。")
                break

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit"):
                print("再见。")
                break

            messages.append({"role": "user", "content": user_input})

            if chat_with_tools(api_key, base_url, model, messages):
                continue

            # 这一轮失败：把本轮留下的消息全撤掉（工具结果、半截回答），
            # 别污染后续上下文。退到下一条 user 就说明退到本轮开头了。
            while messages and messages[-1].get("role") != "user":
                messages.pop()
            if messages:
                messages.pop()

    except KeyboardInterrupt:
        # Ctrl+C 是正常的退出方式，不是错误，所以给一句干净的告别
        print("\n\n已中断，再见。")


if __name__ == "__main__":
    main()
