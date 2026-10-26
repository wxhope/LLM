# -*- coding: utf-8 -*-
"""联网搜索工具：searchWe。

走百度千帆「智能搜索生成」接口（不是普通搜索）：它自己去百度搜一圈，
再让指定的大模型把结果读一遍写成摘要，最后连参考来源一起还给我们。
对 Agent 来说这就是"能看今天互联网"的那只眼睛 —— 模型的知识有截止日期，搜索没有。

接口事实（2026-10 实测确认，别凭记忆改）：
    POST https://qianfan.baidubce.com/v2/ai_search/chat/completions
    Authorization: Bearer <API Key>
    请求体：messages / model / search_source / resource_type_filter / search_mode
    成功：choices[0].message.content 是摘要，references[] 是来源（title/url/content/date）
    失败：HTTP 200 + {"code": "invalid_model", "message": "..."} 也是错误！
          —— 只看 HTTP 状态码会把"模型没开通"当成"搜到 0 条"，这里必须看 code。

三条必须守住的规矩：
  1. API Key 只从 config.ini 读（[search] 段，兼容写在 [deepseek] 段的旧写法），
     绝不硬编码，也绝不从环境变量偷偷兜底 —— 那样排查起来就是玄学。
  2. 任何失败都变成一句人话 ToolResult，不抛异常。抛出去会被 Tool.invoke 兜成
     "工具执行出错"，把真正的原因（401 / 限流 / 模型没开通）全糊掉。
  3. 返回给模型的文本要**带链接**。不带来源的摘要等于让模型二次编造，
     这正是我们要防的事。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from ..base import DangerLevel, ToolContext, ToolResult, enumeration, integer, params, string, tool

SEARCH_URL = "https://qianfan.baidubce.com/v2/ai_search/chat/completions"
SEARCH_HOST = "qianfan.baidubce.com"

DEFAULT_TOP_K = 5
MAX_TOP_K = 10
MAX_SUMMARY_CHARS = 2000      # 摘要截断：再长就是拿上下文换废话
MAX_SNIPPET_CHARS = 200       # 每条来源的片段
MAX_REFS = 8                  # 最多列几条来源

RECENCY_TEXT = {
    "week": "最近 7 天",
    "month": "最近 30 天",
    "semiyear": "最近 180 天",
    "year": "最近 365 天",
}


class SearchError(Exception):
    """搜索失败。message 已经是可以直接给模型/用户看的中文。"""


# --------------------------------------------------------------------- 组装请求


def build_payload(query: str, model: str, top_k: int, recency: str) -> Dict[str, Any]:
    """拼请求体。字段名照抄文档，少一个都可能被判 400。"""
    body: Dict[str, Any] = {
        "messages": [{"role": "user", "content": query}],
        # 默认 auto 会让接口自己判断"要不要搜"，可我们是**被显式调用**来搜的，
        # 所以强制 required，否则用户问"今天有什么新闻"它可能只回一句闲聊。
        "search_mode": "required",
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": top_k}],
    }
    if model:
        # 传了 model 才是"智能搜索生成"（有摘要）；不传就退化成纯网页搜索。
        body["model"] = model
    if recency in RECENCY_TEXT:
        body["search_recency_filter"] = recency
    return body


# --------------------------------------------------------------------- 发请求


def _decode_error_body(raw: bytes) -> Tuple[str, str]:
    """从错误响应体里挖出 (code, message)。挖不到就返回原始文本。"""
    text = raw.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return "", text[:300]
    if not isinstance(data, dict):
        return "", text[:300]
    code = str(data.get("code") or data.get("error_code") or "").strip()
    message = str(data.get("message") or data.get("error_msg") or data.get("error") or "").strip()
    return code, message or text[:300]


def _http_error(status: int, raw: bytes) -> SearchError:
    """把 HTTP 状态码翻译成"接下来该干什么"。"""
    code, detail = _decode_error_body(raw)
    tail = "（接口原文：%s%s）" % (
        ("code=%s " % code) if code else "",
        detail or "无内容",
    )
    if status == 400:
        return SearchError("搜索请求被拒绝，参数可能有误%s" % tail)
    if status == 401:
        return SearchError(
            "百度千帆鉴权失败（401）：API Key 无效、被删除或已过期。"
            "请检查 config.ini 里 [search] 段的 baidu_qianfan_api_key。%s" % tail)
    if status == 403:
        return SearchError(
            "百度千帆拒绝访问（403）：这个 Key 可能没有开通「智能搜索生成」权限，"
            "去千帆控制台确认服务已开通。%s" % tail)
    if status == 429:
        return SearchError("搜索被限流了（429）：调用太快或免费额度已用完，稍等再试。%s" % tail)
    if status in (500, 502, 503, 504):
        return SearchError("百度千帆服务端出错（%d），等一会儿再试。%s" % (status, tail))
    return SearchError("百度千帆返回了意外的状态码 %d。%s" % (status, tail))


def request_search(body: Dict[str, Any], api_key: str, timeout: float) -> Dict[str, Any]:
    """发一次请求，返回解析后的 JSON。任何失败都抛 SearchError。"""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        SEARCH_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        # HTTPError 也是响应对象，要先把 body 读出来再丢
        try:
            detail = e.read()
        except Exception:   # noqa: BLE001 —— 读不到就算了，不能因为读错误体再炸一次
            detail = b""
        raise _http_error(e.code, detail) from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise SearchError(
                "搜索超时（超过 %.0f 秒没有响应），网络慢或百度那边堵了，稍后再试。"
                % timeout) from e
        text = str(reason)
        if "getaddrinfo" in text or "Name or service not known" in text:
            raise SearchError(
                "解析不了域名 %s：本机可能没有联网，或 DNS 被拦了。" % SEARCH_HOST) from e
        if "SSL" in text or "CERTIFICATE" in text.upper():
            raise SearchError("和 %s 的 TLS 握手失败：%s" % (SEARCH_HOST, text)) from e
        raise SearchError("连不上 %s：%s" % (SEARCH_HOST, text)) from e
    except (socket.timeout, TimeoutError) as e:
        raise SearchError(
            "搜索超时（超过 %.0f 秒没有响应），网络慢或百度那边堵了，稍后再试。" % timeout) from e
    except OSError as e:
        # 断网、连接被重置、被防火墙掐断都落在这里
        raise SearchError("网络请求失败：%s" % e) from e

    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise SearchError("百度千帆返回了空响应体。")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise SearchError("百度千帆返回的不是合法 JSON（前 200 字）：%s" % text[:200]) from e
    if not isinstance(data, dict):
        raise SearchError("百度千帆返回的结构不对，预期是 JSON 对象。")
    return data


# --------------------------------------------------------------------- 读响应


def check_business_error(data: Dict[str, Any]) -> None:
    """HTTP 200 但 body 里有 code —— 这是百度那边的业务错误，必须当错误处理。

    实测：模型没开通时返回
        {"code": "invalid_model", "message": "The model does not exist or ..."}
    如果不看这个 code，就会把一次失败讲成"没搜到相关内容"，排查方向全跑偏。
    """
    code = data.get("code")
    if code in (None, "", 0, "0", "success", "SUCCESS"):
        return
    message = str(data.get("message") or "").strip() or "接口没有给出原因"
    hint = ""
    if "model" in str(code).lower() or "model" in message.lower():
        hint = ("当前搜索模型可能没在这个千帆账号下开通，"
                "可以改 config.ini 里 [search] 段的 model，例如 ernie-4.5-turbo-128k。")
    raise SearchError("百度千帆返回错误 code=%s：%s %s" % (code, message, hint))


def pick_summary(data: Dict[str, Any]) -> str:
    """取摘要正文。choices 可能是空列表，也可能是 [{message:{content:""}}]。"""
    for choice in data.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        content = str(message.get("content") or "").strip()
        if content:
            return content
    return ""


def _clean_field(value: Any) -> str:
    """清掉接口爱填的"空值"。它会给 website 填字面量「无」，直接拼进正文很难看。"""
    text = str(value or "").strip()
    return "" if text in ("无", "-", "None", "null", "未知") else text


def pick_references(data: Dict[str, Any]) -> List[Dict[str, str]]:
    """把 references 洗成统一的 {title,url,snippet,date,site}，字段缺了也不崩。"""
    result: List[Dict[str, str]] = []
    for item in data.get("references") or []:
        if not isinstance(item, dict):
            continue
        url = _clean_field(item.get("url"))
        title = _clean_field(item.get("title"))
        if not url and not title:
            continue
        snippet = _clean_field(item.get("content"))
        result.append({
            "title": title or url,
            "url": url,
            "snippet": _compact(snippet, MAX_SNIPPET_CHARS),
            "date": _clean_field(item.get("date")),
            "site": _clean_field(item.get("website")),
        })
    return result


# --------------------------------------------------------------------- 排版


def _compact(text: str, limit: int) -> str:
    """压成一行再截断：换行会把手写的对齐排版打乱，模型也读不出额外信息。"""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


def format_result(query: str, summary: str, refs: List[Dict[str, str]],
                  recency: str, model: str) -> str:
    """拼成给模型看的文本。刻意用纯文本表格，不用 JSON。

    为什么不用 JSON：模型读 JSON 也读得懂，但人一眼扫过去会很难受；
    而 ToolResult.content 是"人机共读"的（界面也会显示它）。
    """
    lines: List[str] = []
    head = "搜索词：%s" % query
    if recency in RECENCY_TEXT:
        head += "（限定%s）" % RECENCY_TEXT[recency]
    lines.append(head)
    lines.append("")

    if summary:
        lines.append("【搜索摘要】")
        lines.append(_compact_paragraphs(summary, MAX_SUMMARY_CHARS))
    else:
        lines.append("【搜索摘要】")
        lines.append("（接口没有返回摘要，只有网页结果。原因可能是 %s 为空或该模型不支持总结。）"
                     % ("[search] model" if not model else model))

    lines.append("")
    if refs:
        lines.append("【参考来源】共 %d 条" % len(refs))
        for index, ref in enumerate(refs[:MAX_REFS], start=1):
            lines.append("[%d] %s" % (index, ref["title"]))
            if ref["url"]:
                lines.append("    链接：%s" % ref["url"])
            extra = "  ".join(x for x in (ref["site"], ref["date"]) if x)
            if extra:
                lines.append("    出处：%s" % extra)
            if ref["snippet"]:
                lines.append("    片段：%s" % ref["snippet"])
    else:
        lines.append("【参考来源】无")

    lines.append("")
    lines.append("以上内容来自百度实时搜索，不是模型记忆。回答时请把关键结论对应到上面的链接，"
                 "并提醒用户自行核实来源。")
    return "\n".join(lines)


def _compact_paragraphs(text: str, limit: int) -> str:
    """摘要保留原有段落感（模型写的是 markdown），只做长度截断。"""
    body = text.strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "\n…（摘要过长，已截断）"


# --------------------------------------------------------------------- 主流程


def search_web(query: str, api_key: str, model: str = "", top_k: int = DEFAULT_TOP_K,
               recency: str = "", timeout: float = 30.0) -> str:
    """真正的活儿：查一次，返回给模型看的文本。失败抛 SearchError。

    单独抽出来（不放 handler 里）是为了能被 selfcheck 或别的界面直接调。
    """
    if not api_key:
        raise SearchError(
            "没有配置百度千帆 API Key，联网搜索不可用。"
            "请在 config.ini 里加上 [search] 段，写一行 "
            "baidu_qianfan_api_key = 你的千帆APIKey，然后重启 xiaoA。"
            "（Key 在千帆控制台「API Key 管理」里创建，形态类似 bce-v3/ALTAK-xxx/yyy）")

    body = build_payload(query, model, top_k, recency)
    data = request_search(body, api_key, timeout)

    check_business_error(data)

    summary = pick_summary(data)
    refs = pick_references(data)

    if not summary and not refs:
        raise SearchError(
            "搜索返回了空结果：没有摘要也没有来源。换个更具体的关键词再试，"
            "或者这条 query 可能被内容安全策略拦下了。")

    return format_result(query, summary, refs, recency, model)


def _arg_text(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    return str(value).strip() if value is not None else ""


def _arg_top_k(args: Dict[str, Any]) -> int:
    raw = args.get("top_k")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_TOP_K
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_TOP_K
    return max(1, min(value, MAX_TOP_K))


@tool(
    name="searchWe",
    description=(
        "联网搜索。用百度实时检索互联网，返回一段搜索摘要和若干参考来源（标题 + 链接）。\n"
        "凡是涉及「最新」「最近」「现在」「今年」「刚刚发布」这类超出你知识范围的问题——"
        "新闻、股价、赛事、软件版本、政策变动、人物职位、产品价格——都应该先调这个工具，"
        "不要凭记忆回答。\n"
        "query 要写成一句人能听懂的话或几个关键词（例：2026年10月 上证指数 收盘）。"
    ),
    parameters=params(
        {
            "query": string("要搜索的问题或关键词，用自然语言写，越具体越好。"),
            "top_k": integer("最多返回几条网页来源，1-10，默认 5。"),
            "recency": enumeration(
                "只搜指定时间内发布的网页。不填表示不限时间；问「最新消息」时建议填 week 或 month。",
                ["week", "month", "semiyear", "year"],
            ),
        },
        required=["query"],
    ),
    danger=DangerLevel.SAFE,
    tags=["web", "search", "net"],
)
def search_we(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    query = _arg_text(args, "query")
    if not query:
        return ToolResult(
            "searchWe 需要一个非空的 query 参数，例如 {\"query\": \"今天的 A 股收盘情况\"}。"
            "请重新调用。", ok=False)

    recency = _arg_text(args, "recency").lower()
    if recency and recency not in RECENCY_TEXT:
        return ToolResult(
            "recency 只能取 week / month / semiyear / year，收到的是「%s」。"
            "不限定时间就不传这个参数。" % recency, ok=False)

    try:
        text = search_web(
            query=query,
            api_key=(ctx.search_api_key or "").strip(),
            model=(ctx.search_model or "").strip(),
            top_k=_arg_top_k(args),
            recency=recency,
            timeout=float(ctx.search_timeout or 30.0),
        )
    except SearchError as e:
        return ToolResult("联网搜索失败：%s" % e, ok=False)
    except Exception as e:  # noqa: BLE001 —— 兜底也不能让整轮对话炸掉
        return ToolResult("联网搜索时出现未预期的错误（%s）：%s"
                          % (type(e).__name__, e), ok=False)
    return ToolResult(text, ok=True)


ALL_TOOLS = [search_we]

__all__ = [
    "ALL_TOOLS",
    "search_we",
    "search_web",
    "build_payload",
    "request_search",
    "check_business_error",
    "pick_summary",
    "pick_references",
    "format_result",
    "SearchError",
    "SEARCH_URL",
    "DEFAULT_TOP_K",
]
