# -*- coding: utf-8 -*-
"""上下文压缩与历史检索。

问题：模型的上下文窗口是有限的（DeepSeek 128k、GPT 类 128k…），
而长任务会一路把历史堆到爆。爆了的表现不是"变慢"，是接口直接 400。

解法（本阶段刻意做简单，不用向量库、不用 Embedding）：
  1. 量：估算/读取当前上下文占了多少 token，到 70% 就警告并停止接收新提问；
  2. 存：压缩前把完整原始历史按行写成 JSONL，落到 .agents/context/；
  3. 压：让 LLM 把历史写成一份约 5,000 token 的工作摘要，用它替换历史；
  4. 查：摘要里丢了细节时，用 recallContext 在归档里做本地全文检索。

为什么不用向量检索：
  这个项目的目的是让学生看清"上下文管理"这件事本身。关键词检索 60 行就能
  读懂、能调试、能改；向量库要装依赖、要算 embedding、命中率还说不清。
  真需要语义检索时，这里的 search_archives 就是唯一要换掉的函数。

模块边界：本文件不碰 Session、不碰 UI，只做"算 token、写文件、读文件、拼文本"。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------- 常量

CONTEXT_DIRNAME = ".agents"
CONTEXT_SUBDIR = "context"

DEFAULT_WINDOW = 128000          # 拿不到模型窗口时的默认值（题目要求 128k）
DEFAULT_WARN_RATIO = 0.70        # 到这个比例就警告 + 暂停接收新提问
SUMMARY_TARGET_TOKENS = 5000     # 摘要目标体量（给模型的要求，不是硬校验）
ARCHIVE_NOTE_TEMPLATE = (
    "此前上下文已于 %s 完成压缩，原始上下文保存在编号为 %s 的归档文件中。"
    "如需查找历史信息，请使用 recallContext 工具。"
)

# 压缩专用 Prompt：题目给定，一字不改。它是"给另一个 Agent 交接工作"的说明书，
# 目标不是复述对话，而是让没读过原文的 Agent 能接着干。
COMPACT_PROMPT = """你是一名 AI Agent 的上下文管理助手。当前会话即将达到上下文窗口限制，需要将历史对话压缩为一份高质量的工作摘要，以便 Agent 在新的上下文中继续执行任务。

请认真阅读提供的历史上下文，提取对后续工作仍然有价值的信息，并生成一份约 5,000 Tokens 的 Markdown 摘要。

摘要应重点保留以下内容：

1. **用户目标与要求**：用户希望完成什么任务？有哪些必须遵守的约束、偏好和验收条件？
2. **已经完成的工作**：哪些任务已经完成？实现了哪些功能？获得了哪些已经验证的结果？
3. **当前工作状态**：Agent 正在处理什么任务？当前进展到哪一步？是否存在尚未解决的问题？
4. **关键技术信息**：保留重要的文件路径、函数名称、接口定义、配置项、执行命令、错误信息和必要的代码片段。
5. **关键决策与原因**：记录用户和 Agent 已经确定的技术方案，以及重要的设计取舍。
6. **下一步任务**：明确接下来应该执行的操作，以及需要继续验证的事项。

请遵守以下要求：

- 优先保留对后续任务有直接影响的信息，删除寒暄、重复讨论、过时的尝试和无关内容。
- 对已经验证的事实与尚未验证的推测作出明确区分。
- 不要将计划完成的任务描述为已经完成。
- 重要的文件路径、函数名称、配置名称和命令应保留准确原文。
- 对失败的尝试，只保留有助于避免重复错误的信息。
- 如果信息存在冲突，应优先保留用户最新确认的要求，并简要说明仍然存在的冲突。
- 如果某些历史细节无法在摘要中完整保留，可简要说明它们已保存到原始上下文归档中，后续可以通过 `recallContext` 工具重新检索。
- 使用清晰的 Markdown 结构输出摘要，不要附加解释或无关内容。

**最终目标：使一个从未阅读过原始对话的 Agent，仅凭这份摘要，就能够理解当前任务、继承关键决策，并继续开展工作。**"""


class CompactError(Exception):
    """压缩流程里可以预期、且应当原样告诉用户的错误。"""


class ArchiveError(CompactError):
    """归档写失败。这种情况必须中止压缩——宁可没压缩，也不能丢了原文。"""


# --------------------------------------------------------------------- 1. 量

def estimate_tokens(text: str) -> int:
    """粗估一段文字的 token 数。

    为什么不用 tiktoken：多一个依赖、多一份词表，而且各家模型的分词器都不一样。
    中文按 1 字 ≈ 1 token，英文/符号按 4 字符 ≈ 1 token，误差在 ±20% 内——
    用来触发"到 70% 了"完全够用。

    优先用 API 返回的 usage（精确）；估算只是 API 没给时的兜底。
    """
    if not text:
        return 0
    cjk = 0
    for ch in text:
        # 中日韩统一表意文字 + 全角标点，都按 1 字 1 token 算
        if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" \
                or "\uff00" <= ch <= "\uffef":
            cjk += 1
    other = len(text) - cjk
    return int(cjk + other / 4) or 1


def _content_text(content: Any) -> str:
    """messages 里的 content 可能是字符串，也可能是分片数组。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return "" if content is None else str(content)


def estimate_message_tokens(message: Dict[str, Any]) -> int:
    """单条消息的 token：正文 + 工具调用的名字和参数 + 固定开销。"""
    total = 4                                   # 每条消息的格式开销（role 等）
    total += estimate_tokens(_content_text(message.get("content")))
    for call in (message.get("tool_calls") or []):
        fn = (call or {}).get("function") or {}
        total += estimate_tokens(str(fn.get("name") or ""))
        total += estimate_tokens(str(fn.get("arguments") or ""))
    if message.get("name"):
        total += estimate_tokens(str(message["name"]))
    return total


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    return sum(estimate_message_tokens(m) for m in (messages or []))


@dataclass
class ContextUsage:
    """当前上下文占用情况。UI 直接拿去显示。"""

    tokens: int
    window: int
    ratio: float                    # 0~1
    source: str = "estimate"        # api | estimate（怎么算出来的）
    window_is_default: bool = False  # 窗口大小是默认值（拿不到真实值时）

    @property
    def percent(self) -> float:
        return round(self.ratio * 100, 1)

    def short(self) -> str:
        """状态栏用的一行：42.3k / 128k（33%）默认"""
        text = "%s / %s（%d%%）" % (
            _k(self.tokens), _k(self.window), round(self.ratio * 100))
        if self.window_is_default:
            text += " 默认"
        return text


def _k(tokens: int) -> str:
    """按 1000 进位显示：128000 → 128k（模型窗口都按十进制标称）。"""
    if tokens < 1000:
        return str(tokens)
    value = tokens / 1000.0
    if value >= 100:
        return "%.0fk" % value
    return "%.1fk" % value


def compute_usage(messages: List[Dict[str, Any]], system_prompt: str,
                  window: int, api_prompt_tokens: Optional[int] = None,
                  window_is_default: bool = False) -> ContextUsage:
    """算占用。

    API 给了 prompt_tokens 就用它（那是服务端真实计数，含 system 和 tools）；
    但 API 的数字只代表"上次请求那一刻"，之后又追加的消息要补上——
    所以取 max(api 值, 本地估算)，宁可早一点警告，也不要等到 400。
    """
    estimated = estimate_tokens(system_prompt) + estimate_messages_tokens(messages)
    if api_prompt_tokens and api_prompt_tokens > 0:
        tokens = max(api_prompt_tokens, estimated)
        source = "api"
    else:
        tokens = estimated
        source = "estimate"
    window = max(int(window or DEFAULT_WINDOW), 1000)
    return ContextUsage(
        tokens=tokens,
        window=window,
        ratio=min(tokens / float(window), 1.5),   # 超了也照实显示（150%），不截到 100%
        source=source,
        window_is_default=window_is_default,
    )


# --------------------------------------------------------------------- 2. 存

@dataclass
class ArchiveRecord:
    """一次归档的结果。"""

    archive_id: str
    path: str
    message_count: int
    created_at: str


def context_dir(project_root: str) -> str:
    """<项目根>/.agents/context。和技能目录同一个项目根。"""
    return os.path.join(project_root, CONTEXT_DIRNAME, CONTEXT_SUBDIR)


def archive_messages(messages: List[Dict[str, Any]], directory: str,
                     now: Optional[datetime] = None) -> ArchiveRecord:
    """把当前历史完整写成 JSONL。写完还要读回来核对——写不全就不许压缩。

    文件格式：
        第一行  {"type":"meta", "archive_id":..., "created_at":..., "message_count":...}
        之后每行一条消息 {"type":"message", "index":0, "role":..., ...}
    保留 role/content/tool_calls/tool_call_id/name，顺序就是 index 的顺序。
    """
    if not messages:
        raise ArchiveError("当前没有可归档的消息。")

    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    created_at = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as e:
        # 目录位置被一个同名文件占了、没有写权限……都在这里拦下
        raise ArchiveError("建不了归档目录（%s）：%s" % (directory, e)) from e

    archive_id = stamp
    path = os.path.join(directory, archive_id + ".jsonl")
    bump = 1
    while os.path.exists(path):          # 同一秒压缩两次也不会覆盖
        bump += 1
        archive_id = "%s-%d" % (stamp, bump)
        path = os.path.join(directory, archive_id + ".jsonl")

    meta = {
        "type": "meta",
        "archive_id": archive_id,
        "created_at": created_at,
        "message_count": len(messages),
    }
    try:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            for index, message in enumerate(messages):
                row = {"type": "message", "index": index}
                row.update(message)      # role / content / tool_calls / tool_call_id / name
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())       # 真落盘：断电/崩溃时文件也还在
    except OSError as e:
        raise ArchiveError("写归档失败（%s）：%s" % (path, e)) from e

    # 写完立刻核对：行数对不对、文件是不是空的。
    # 原始记录必须先成功保存，才允许后面替换上下文——这是本功能的底线。
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) != len(messages) + 1:
            raise ArchiveError("归档校验失败：应有 %d 行，实际 %d 行（%s）"
                               % (len(messages) + 1, len(lines), path))
        if os.path.getsize(path) <= 0:
            raise ArchiveError("归档校验失败：文件是空的（%s）" % path)
    except OSError as e:
        raise ArchiveError("归档校验失败，读不回来（%s）：%s" % (path, e)) from e

    return ArchiveRecord(archive_id=archive_id, path=path,
                         message_count=len(messages), created_at=created_at)


# --------------------------------------------------------------------- 3. 压

def build_transcript(messages: List[Dict[str, Any]], max_chars: int = 120000) -> str:
    """把历史拍平成给压缩模型读的纯文本。

    为什么要拍平：压缩请求只需要"读"，不需要模型再调工具；
    保留结构化 tool_calls 反而会让它误以为可以继续调用。
    但工具名、参数、结果必须留——那是长任务里最值钱的部分。
    """
    chunks: List[str] = []
    for index, message in enumerate(messages):
        role = message.get("role") or "unknown"
        head = "[%d] %s" % (index, role)
        if role == "tool":
            if message.get("tool_call_id"):
                head += "（tool_call_id=%s）" % message["tool_call_id"]
            chunks.append(head + ":\n" + _content_text(message.get("content")))
            continue
        body = _content_text(message.get("content"))
        if body:
            chunks.append(head + ":\n" + body)
        for call in (message.get("tool_calls") or []):
            fn = (call or {}).get("function") or {}
            chunks.append("%s 调用工具 %s，参数：\n%s"
                          % (head, fn.get("name"), fn.get("arguments")))
    text = "\n\n".join(chunks)
    if len(text) > max_chars:
        # 太长的历史按"留头留尾"截断：开头的目标、结尾的当前状态最有价值
        keep = max_chars - 200
        head = int(keep * 0.35)
        text = (text[:head] + "\n\n…（中间省略 %d 字符，完整内容见归档）…\n\n"
                % (len(text) - keep) + text[-int(keep * 0.65):])
    return text


def build_compact_messages(messages: List[Dict[str, Any]],
                           max_chars: int = 120000) -> List[Dict[str, Any]]:
    """压缩请求的 messages：system=压缩专用 Prompt，user=拍平后的历史。"""
    return [
        {"role": "system", "content": COMPACT_PROMPT},
        {"role": "user", "content": "以下是需要压缩的历史上下文：\n\n"
                                    + build_transcript(messages, max_chars)},
    ]


def build_summary_message(summary: str, archive_id: str,
                          created_at: str) -> Dict[str, Any]:
    """压缩后的替代消息：摘要 + 归档说明，作为一条 user 消息放进历史。

    为什么用 user 而不是 system：system 是程序的规矩，摘要是"上一段工作的
    交接说明"，属于对话内容。放在 user 里，模型不会把它当成新的最高指令。
    """
    note = ARCHIVE_NOTE_TEMPLATE % (created_at, archive_id)
    content = ("【此前对话的压缩摘要】\n\n%s\n\n---\n\n%s" % (summary.strip(), note))
    return {"role": "user", "content": content}


# --------------------------------------------------------------------- 4. 查

@dataclass
class ArchiveHit:
    """一条检索命中。"""

    archive_id: str
    index: int              # 消息在原归档里的序号
    role: str
    snippet: str
    score: int


def list_archives(directory: str) -> List[str]:
    """按时间倒序列出归档编号（新的在前：越新越可能是当前任务的上下文）。"""
    if not os.path.isdir(directory):
        return []
    names = [n[:-6] for n in os.listdir(directory) if n.endswith(".jsonl")]
    return sorted(names, reverse=True)


def _split_terms(text: str) -> List[str]:
    """把查询拆成关键词。

    中文没有空格，所以：整串也算一个词（子串匹配），再按空白/标点拆出片段。
    这样"归档文件格式"既能整串命中，也能拆成"归档""文件""格式"分别命中。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    terms = {raw.lower()}
    for piece in raw.replace("\n", " ").replace("\t", " ").split(" "):
        piece = piece.strip()
        if len(piece) >= 2:
            terms.add(piece.lower())
    return [t for t in terms if t]


def _message_blob(message: Dict[str, Any]) -> str:
    """一条消息用于检索的全文：正文 + 工具名 + 参数 + 结果。"""
    parts = [_content_text(message.get("content"))]
    for call in (message.get("tool_calls") or []):
        fn = (call or {}).get("function") or {}
        parts.append(str(fn.get("name") or ""))
        parts.append(str(fn.get("arguments") or ""))
    if message.get("tool_call_id"):
        parts.append(str(message["tool_call_id"]))
    return "\n".join(p for p in parts if p)


def _snippet(blob: str, terms: List[str], width: int = 160) -> str:
    """取第一处命中前后的文字，太长就收窄。"""
    lowered = blob.lower()
    pos = -1
    for term in terms:
        found = lowered.find(term)
        if found >= 0 and (pos < 0 or found < pos):
            pos = found
    if pos < 0:
        return blob[:width]
    start = max(0, pos - width // 2)
    end = min(len(blob), pos + width // 2)
    text = blob[start:end].replace("\r", "")
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(blob) else ""
    return prefix + text + suffix


def search_archives(directory: str, text: str, archive_id: Optional[str] = None,
                    max_hits: int = 8, max_chars: int = 4000
                    ) -> Tuple[List[ArchiveHit], str]:
    """在归档里做本地全文关键词检索。返回 (命中列表, 错误说明)。

    匹配方式：大小写不敏感的子串匹配。命中次数当分数，多的排前面。
    """
    terms = _split_terms(text)
    if not terms:
        return [], "recallContext 需要一个非空的 text 参数（要查找的关键词）。"

    if archive_id:
        path = os.path.join(directory, archive_id + ".jsonl")
        if not os.path.isfile(path):
            return [], ("没有编号为 %s 的归档。可用归档：%s"
                        % (archive_id, "、".join(list_archives(directory)[:10]) or "（无）"))
        paths = [(archive_id, path)]
    else:
        ids = list_archives(directory)
        if not ids:
            return [], ("%s 下还没有归档。执行 /compact 压缩上下文后，原始记录会保存在这里。"
                        % directory)
        paths = [(i, os.path.join(directory, i + ".jsonl")) for i in ids]

    hits: List[ArchiveHit] = []
    scanned = 0
    for aid, path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("type") != "message":
                        continue
                    scanned += 1
                    blob = _message_blob(row)
                    lowered = blob.lower()
                    score = sum(lowered.count(t) for t in terms)
                    if score <= 0:
                        continue
                    hits.append(ArchiveHit(
                        archive_id=aid,
                        index=int(row.get("index", 0)),
                        role=str(row.get("role") or "?"),
                        snippet=_snippet(blob, terms),
                        score=score,
                    ))
        except OSError as e:
            return [], "读归档失败（%s）：%s" % (path, e)

    if not hits:
        return [], ("没有找到包含「%s」的内容（检索了 %d 条消息、%d 个归档）。"
                    % (text, scanned, len(paths)))

    hits.sort(key=lambda h: (h.score, h.archive_id, h.index), reverse=True)

    # 结果长度要控制：检索是为了补细节，不是为了把归档再灌回上下文
    picked: List[ArchiveHit] = []
    budget = max(500, max_chars)
    for hit in hits[:max_hits]:
        cost = len(hit.snippet) + 60
        if picked and cost > budget:
            break
        budget -= cost
        picked.append(hit)
    return picked, ""


def format_hits(hits: List[ArchiveHit], text: str) -> str:
    """把命中排成给模型读的文本：编号 + 位置 + 角色 + 片段。"""
    lines = ["【recallContext】关键词「%s」命中 %d 条（按相关度排序）：" % (text, len(hits))]
    for hit in hits:
        lines.append("")
        lines.append("· 归档 %s ｜ 第 %d 条 ｜ %s"
                     % (hit.archive_id, hit.index, hit.role))
        lines.append(hit.snippet.strip())
    lines.append("")
    lines.append("以上为原始记录片段；需要更多细节可以用更精确的关键词或指定 id 再查一次。")
    return "\n".join(lines)


__all__ = [
    "COMPACT_PROMPT",
    "ARCHIVE_NOTE_TEMPLATE",
    "SUMMARY_TARGET_TOKENS",
    "DEFAULT_WINDOW",
    "DEFAULT_WARN_RATIO",
    "CONTEXT_DIRNAME",
    "CONTEXT_SUBDIR",
    "CompactError",
    "ArchiveError",
    "ArchiveRecord",
    "ArchiveHit",
    "ContextUsage",
    "estimate_tokens",
    "estimate_message_tokens",
    "estimate_messages_tokens",
    "compute_usage",
    "context_dir",
    "archive_messages",
    "build_transcript",
    "build_compact_messages",
    "build_summary_message",
    "list_archives",
    "search_archives",
    "format_hits",
]
