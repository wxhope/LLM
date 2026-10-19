# -*- coding: utf-8 -*-
"""命令行聊天程序 + 工具调用（function calling），调用 DeepSeek 的 OpenAI 兼容接口。

课题二：在 chat-stream-tool-getDate.py 的基础上，再加一个工具 isLeapYear。
  - getDate：调本机 date 命令拿当前日期时间（不是 Python 算的）
  - isLeapYear：传一个年份进来，用 Python 代码判断是不是闰年

为什么这种"能算准"的问题要用 function call 而不是让 AI 直接答：
模型回答是"生成"出来的，闰年规则它记得住，但换个刁钻年份就可能顺口说错。
交给代码算，结果 100% 确定；模型只负责把你的话翻译成参数、再把结果讲清楚。

仍然只用标准库。运行前把 config.ini 放在本目录（可从 Practice01 复制）。
"""

import configparser
import http.client
import json
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
MAX_TOOL_ROUNDS = 5

# 让 date 命令输出的格式：年-月-日 时:分:秒 星期
DATE_FORMAT = "+%Y-%m-%d %H:%M:%S %A"

# 告诉模型我们有哪些工具。模型只会看 description 来决定调不调、怎么填参数，
# 所以描述要写清楚"什么时候该用我"。
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


# ---------------------------------------------------------------- getDate 工具

def decode_bytes(raw):
    """命令输出在 Windows 下可能是 GBK，挨个编码试着解。"""
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

    raw = None
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


# ---------------------------------------------------------------- 工具分发

def run_tool(name, args_text):
    """按名字执行工具。新增工具时，只在这里加一个分支就行。"""
    if name == "getDate":
        return run_date_command()
    if name == "isLeapYear":
        return run_is_leap_year(args_text)
    return "没有这个工具：%s" % name


# ---------------------------------------------------------------- 与接口交互

def stream_chat(api_key, base_url, model, messages):
    """发一轮请求，边收边打印。

    返回 assistant 消息 dict（可能带 tool_calls），失败返回 None。
    注意这里返回的是"整条消息"，因为下一步要把它原样塞回 messages 里，
    模型才知道自己刚才发起过工具调用。
    """
    host, port, use_tls = parse_base_url(base_url)

    payload = json.dumps({
        "model": model,
        "messages": messages,
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
            print("[结果] %s" % result)
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
    print("可用工具: getDate（调本机 date 命令取当前日期时间）")
    print("          isLeapYear（Python 代码判断闰年）")
    print("输入 exit 或 quit 退出，或随时按 Ctrl+C 强行结束")
    print("-" * 40)

    messages = []

    # 整个对话过程包在 try 里，这样不管在哪一步按 Ctrl+C
    # （等你输入时、AI 正在打字时）都能干净退出，不会甩出一堆报错。
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
