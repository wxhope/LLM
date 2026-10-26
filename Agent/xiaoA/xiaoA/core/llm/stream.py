# -*- coding: utf-8 -*-
"""SSE 解析：把一行行文本变成 StreamDelta。

单独拆出来的理由：
  这是整个 LLM 层最容易出错、也最值得测试的地方（tool_calls 分片拼接）。
  把它做成纯函数，就能塞几行假数据单测，不用花 token、不用连网。

上游给的数据长这样（每行一个 data:，最后来个 [DONE]）：

    data: {"choices":[{"delta":{"content":"你"}}]}
    data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_x",
            "function":{"name":"getDate","arguments":"{}"}}]}}]}
    data: [DONE]

注意 tool_calls 的分片规则：
  同一次调用会分几片推来，每片只带新增的字符串片段。
  name 和 arguments 都要用 += 拼，而不是覆盖——
  覆盖的话最终只剩最后一片，JSON 必然残缺。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Iterator, Optional

from .types import StreamDelta

DATA_PREFIX = "data:"
DONE_MARK = "[DONE]"


def split_lines(chunks: Iterable[bytes]) -> Iterator[str]:
    """把字节块切成完整行，兼容 \\n 与 \\r\\n。

    真实网络里一个 chunk 可能断在半行中间，也可能是好几行粘一起。
    调用方（client）已经用 readline() 保证是整行了，这里再做一层兜底，
    方便单测直接喂原始字节。
    """
    buffer = b""
    for chunk in chunks:
        buffer += chunk
        while b"\n" in buffer:
            line, _, buffer = buffer.partition(b"\n")
            yield line.decode("utf-8", errors="replace").rstrip("\r")
    if buffer:
        yield buffer.decode("utf-8", errors="replace").rstrip("\r")


def parse_line(line: str) -> Optional[StreamDelta]:
    """解析单行。不是数据行返回 None（空行、event:、注释、心跳都忽略）。"""
    text = line.strip()
    if not text.startswith(DATA_PREFIX):
        return None

    payload = text[len(DATA_PREFIX):].strip()
    if not payload:
        return None
    if payload == DONE_MARK:
        return StreamDelta(done=True)

    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        # 服务端偶尔会推半截 JSON，丢掉即可，不要中断整轮对话
        return None

    if not isinstance(obj, dict):
        return None

    # 有些兼容实现会在流里塞错误对象
    if obj.get("error"):
        err = obj["error"]
        message = err.get("message") if isinstance(err, dict) else str(err)
        return StreamDelta(raw={"error": message})
    # 用量统计可放在顶层 usage，也可能是最后一片
    if obj.get("usage"):
        return StreamDelta(raw={"usage": obj["usage"]})

    choices = obj.get("choices") or []
    if not choices:
        return None

    choice = choices[0] or {}
    delta = choice.get("delta") or {}
    result = StreamDelta(
        content=delta.get("content"),
        finish_reason=choice.get("finish_reason"),
        raw=obj,
    )

    calls = delta.get("tool_calls") or []
    if calls:
        first = calls[0] or {}
        fn = first.get("function") or {}
        result.tool_call_index = first.get("index", 0)
        result.tool_call_id = first.get("id")
        result.tool_name = fn.get("name")
        result.tool_arguments = fn.get("arguments")

    return result


def iter_deltas(lines: Iterable[str]) -> Iterator[StreamDelta]:
    """逐行解析，遇到 [DONE] 就停。"""
    for line in lines:
        delta = parse_line(line)
        if delta is None:
            continue
        yield delta
        if delta.done:
            return


def strip_error(delta: StreamDelta) -> Optional[str]:
    """若这一片其实是错误对象，返回错误文案。"""
    raw = delta.raw or {}
    return raw.get("error") if isinstance(raw, dict) else None


def extract_usage(delta: StreamDelta) -> Optional[Dict[str, Any]]:
    raw = delta.raw or {}
    return raw.get("usage") if isinstance(raw, dict) else None
