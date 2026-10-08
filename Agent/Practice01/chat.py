# -*- coding: utf-8 -*-
"""命令行聊天程序，调用 DeepSeek 的 OpenAI 兼容接口。

只用标准库，不依赖任何第三方包。
运行前请先把 config.example.ini 复制成 config.ini，并填上自己的 api_key。

关于流式：这里用 http.client 而不是 urllib.request。
原因是用 urllib 时，它的响应对象会把数据攒起来，导致回复"一次性蹦出来"、
打字机效果消失。http.client 可以按 chunk 逐段读，才能真正做到边收边打。
"""

import configparser
import http.client
import json
import os
import ssl
import sys
import time
import urllib.parse

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.ini")

# 每个字之间的停顿，单位秒。想快就调小（比如 0.01），想更慢就调大。
TYPE_DELAY = 0.03

# 发送历史的上限（粗略按字符数估算，不引第三方 tokenizer）。
# DeepSeek 的上下文一般是 64K tokens，中文约 1 字 1 token，
# 这里留足余量，超过就把最早的几轮丢掉。
MAX_HISTORY_CHARS = 40000


def trim_history(messages, max_chars=MAX_HISTORY_CHARS):
    """历史太长时，从最早的一轮开始丢，保证不超上限。

    返回 (裁剪后的历史, 丢掉的轮数)。成对丢（user+assistant），
    免得留下半截对话让模型困惑。
    """
    def total(msgs):
        return sum(len(m.get("content") or "") for m in msgs)

    if total(messages) <= max_chars:
        return messages, 0

    kept = list(messages)
    dropped = 0
    # 每次丢最早的两条（一问一答），一直丢到不超上限为止
    while len(kept) > 2 and total(kept) > max_chars:
        kept = kept[2:]
        dropped += 1

    return kept, dropped


def load_config(path=CONFIG_PATH):
    """读配置文件，返回 (api_key, base_url, model)。"""
    if not os.path.exists(path):
        print("找不到 config.ini，请先把 config.example.ini 复制成 config.ini 并填好 api_key。")
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


def stream_chat(api_key, base_url, model, messages):
    """把 messages 发给 API，逐字打印回复，并把完整回复返回。"""
    host, port, use_tls = parse_base_url(base_url)

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": True,
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
            piece = delta.get("content")
            if piece:
                full_text += piece
                # 服务端一次可能推来好几个字，这里拆成单字逐个吐，
                # 才会是"一个字一个字"的打字机效果。
                type_out(piece)

    except (OSError, http.client.HTTPException) as e:
        # OSError 覆盖了连不上、超时、SSL 出错等一堆情况；HTTPException 是协议层错误
        print("\n[出错] 请求失败：%s" % e)
        return None
    finally:
        conn.close()

    print()
    return full_text


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

            # 每次发送前检查一下历史长度，太长就丢掉最早的几轮。
            # 这样"把之前所有聊天记录发给模型"能一直成立，不会撞上下文上限。
            send_messages, dropped = trim_history(messages)
            if dropped:
                print("[提示] 对话太长，已自动省略最早的 %d 轮历史。" % dropped)
                # 同步更新本地历史，避免内存里一直涨
                messages[:] = send_messages

            sys.stdout.write("AI: ")
            sys.stdout.flush()
            reply = stream_chat(api_key, base_url, model, send_messages)

            if reply is None:
                # 这一轮失败，把刚才那条 user 消息撤掉，别污染后续上下文
                messages.pop()
                continue

            messages.append({"role": "assistant", "content": reply})

    except KeyboardInterrupt:
        # Ctrl+C 是正常的退出方式，不是错误，所以给一句干净的告别
        print("\n\n已中断，再见。")


if __name__ == "__main__":
    main()
