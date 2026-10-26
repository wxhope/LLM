# -*- coding: utf-8 -*-
"""HTTP 客户端：唯一碰 http.client 的地方。

只用标准库。为什么不用 urllib：
  urllib 的响应对象会把数据攒一段才吐，打字机效果会变成"整段蹦出来"。
  http.client 的 readline() 可以凑齐一行立刻返回，才能真正边收边推。

对外只有一个方法 chat_stream()：
  传入 messages 与 tools，返回一个生成器，逐片吐出 StreamDelta。
调用方（session）不需要知道 HTTP、TLS、SSE 的任何细节。
"""

from __future__ import annotations

import http.client
import json
import ssl
import urllib.parse
from typing import Any, Dict, Iterator, List, Optional

from .stream import iter_deltas, parse_line
from .types import StreamDelta

CHAT_PATH = "/chat/completions"
DEFAULT_TIMEOUT = 300


class LLMError(Exception):
    """接口层错误。消息已经是可以直接给用户看的中文。"""

    def __init__(self, message: str, status: int = 0, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


def split_base_url(base_url: str) -> tuple:
    """把 https://api.deepseek.com 拆成 (主机, 端口, 是否 TLS)。"""
    parts = urllib.parse.urlsplit(base_url)
    host = parts.hostname or "api.deepseek.com"
    if parts.port:
        port = parts.port
    else:
        port = 443 if parts.scheme != "http" else 80
    return host, port, parts.scheme != "http"


class LLMClient:
    """OpenAI 兼容接口的流式客户端。线程安全性：每次请求独立连接，可并发。"""

    def __init__(self, base_url: str, api_key: str, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    # ---------------------------------------------------------------- 内部

    def _connect(self, host: str, port: int, use_tls: bool) -> http.client.HTTPConnection:
        if not use_tls:
            return http.client.HTTPConnection(host, port, timeout=self.timeout)
        # 部分环境证书链不全，放宽校验，否则会卡在 SSL 握手上
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return http.client.HTTPSConnection(host, port, context=ctx, timeout=self.timeout)

    def _build_payload(self, messages: List[Dict[str, Any]], model: str,
                       tools: Optional[List[Dict[str, Any]]]) -> bytes:
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return json.dumps(body, ensure_ascii=False).encode("utf-8")

    # ---------------------------------------------------------------- 对外

    def chat_stream(self, messages: List[Dict[str, Any]], model: str,
                    tools: Optional[List[Dict[str, Any]]] = None
                    ) -> Iterator[StreamDelta]:
        """发起流式请求，逐片 yield。

        连接在生成器结束时关闭，所以一定要把返回的生成器消费完（或用 with 包住）。
        出错时抛 LLMError，调用方负责转成给用户看的提示。
        """
        host, port, use_tls = split_base_url(self.base_url)
        payload = self._build_payload(messages, model, tools)
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.api_key,
            "Accept": "text/event-stream",
            "Content-Length": str(len(payload)),
        }

        conn = self._connect(host, port, use_tls)
        try:
            conn.request("POST", CHAT_PATH, body=payload, headers=headers)
            response = conn.getresponse()

            if response.status != 200:
                detail = response.read().decode("utf-8", errors="ignore")
                raise self._http_error(response.status, detail)

            while True:
                raw_line = response.readline()
                if not raw_line:
                    break
                delta = parse_line(raw_line.decode("utf-8", errors="ignore"))
                if delta is None:
                    continue

                # 流里夹带的错误对象
                if delta.raw and isinstance(delta.raw, dict) and delta.raw.get("error"):
                    raise LLMError("接口返回错误：%s" % delta.raw["error"],
                                   status=response.status)
                yield delta
                if delta.done:
                    break

        except (OSError, http.client.HTTPException) as e:
            # OSError 覆盖连不上、超时、SSL 失败；HTTPException 是协议层错误
            raise LLMError("请求失败：%s" % e) from e
        finally:
            conn.close()

    def _http_error(self, status: int, detail: str) -> LLMError:
        if status == 401:
            return LLMError("API Key 无效或已过期，请检查 config.ini。", status, detail)
        if status == 402:
            return LLMError("账户余额不足。", status, detail)
        if status == 429:
            return LLMError("请求太频繁，被限流了，稍等一会儿再试。", status, detail)
        if status == 400 and "context" in detail.lower():
            return LLMError("对话太长，超出模型上下文上限。可以输入 /clear 清空历史。",
                            status, detail)
        return LLMError("接口返回 %s：%s" % (status, detail[:500]), status, detail)


__all__ = ["LLMClient", "LLMError", "split_base_url", "iter_deltas", "CHAT_PATH"]
