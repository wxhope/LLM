# -*- coding: utf-8 -*-
"""recallContext：在压缩归档里检索原始上下文。

背景：/compact 之后，历史被换成一份约 5,000 token 的摘要。摘要一定丢细节，
而长任务恰恰会回头问"当初那个错误码是多少""改的是哪个文件"。
归档里存着原文，这个工具就是那扇门。

设计取舍：
  - 只做本地全文关键词匹配（子串、大小写不敏感），不装向量库。
    检索是"找回具体事实"，关键词够用，而且行为可预测、出错好查。
  - 结果要短：检索是为了补细节，不是为了把归档再灌回上下文。
    命中按相关度排序，总长有上限，超了宁可少给几条。
  - 参数校验都返回文本而不是抛异常：模型给错参数时，它需要一句能读懂的
    "怎么改"，而不是程序栈。
"""

from __future__ import annotations

from typing import Any, Dict

from ...compact import format_hits, search_archives
from ..base import ToolContext, ToolResult, params, string, tool


@tool(
    name="recallContext",
    description=(
        "检索被压缩归档的原始对话记录。\n"
        "当上下文被 /compact 压缩后，当前历史里只剩一份摘要；"
        "如果摘要里缺少某个细节（具体路径、命令、报错、参数、早期约定），"
        "用这个工具按关键词去归档里查原文。\n"
        "text 是关键词（必需），可以是中文短语或英文标识符；"
        "id 是归档编号（可选，摘要末尾的归档说明里会写），不填就在所有归档里搜。\n"
        "返回命中的归档编号、消息序号、角色和原文片段。"
    ),
    parameters=params(
        {
            "text": string(
                "要查找的关键词或文本片段，例如 \"BACKEND_API_ERROR\"、\"/compact\"、"
                "\"8090\"、\"归档格式\"。中文不需要分词，整串也能匹配。"),
            "id": string(
                "归档编号，例如 20261022-153000。省略则在 .agents/context 下"
                "所有归档中检索。"),
        },
        required=["text"],
    ),
    tags=["context", "memory"],
)
def recall_context_tool(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    text = str(args.get("text") or "").strip()
    if not text:
        return ToolResult(
            "recallContext 需要一个非空的 text 参数，例如 {\"text\": \"8090\"}。"
            "请重新调用。", ok=False)

    directory = ctx.context_dir
    if not directory:
        return ToolResult(
            "recallContext 没有可用的归档目录（当前会话未挂载上下文归档）。", ok=False)

    archive_id = str(args.get("id") or "").strip() or None
    if archive_id:
        # 防目录穿越：编号只允许字母数字和连字符
        if not all(ch.isalnum() or ch in "-_" for ch in archive_id):
            return ToolResult("归档编号只能包含字母、数字、连字符，收到「%s」。" % archive_id,
                              ok=False)

    hits, error = search_archives(directory, text, archive_id=archive_id)
    if error:
        return ToolResult(error, ok=False)
    return ToolResult(format_hits(hits, text))


ALL_TOOLS = [recall_context_tool]

__all__ = ["ALL_TOOLS", "recall_context_tool"]
