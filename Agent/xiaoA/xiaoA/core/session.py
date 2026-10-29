# -*- coding: utf-8 -*-
"""会话层：整个助手的大脑。

职责（且只有这些）：
  1. 保存对话历史
  2. 调用 LLM 客户端，把流式增量拼成完整消息
  3. 判断要不要调工具，调完了把结果回填，再问一轮
  4. 把过程转成事件，交给 sink

它不 print、不读键盘、不知道"终端"和"浏览器"的区别。
换界面只需换 sink 和 confirm，这个文件一个字都不用改——这就是 UI 分离。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import AppConfig
from .events import (
    ErrorOccurred,
    Notice,
    ReplyCompleted,
    RoundStarted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
    TokenUsage,
)
from .llm import AssistantMessage, LLMClient, LLMError, StreamDelta, ToolCall
from .ports import ConfirmPort, EventSink
from .skills import SkillCatalog, discover_skills
from .tools import DangerLevel, ToolContext, ToolRegistry, build_default_registry

SYSTEM_PROMPT = (
    "你是一个运行在用户本机的命令行助手，可以调用工具查看和修改本地文件，也可以联网搜索。"
    "凡是涉及具体文件、目录的问题，都要先用工具查证（getCwd、listDir、readFile），"
    "不要凭猜测编造路径或文件内容。调用文件工具时传绝对路径。"
    "删除操作会请用户现场确认，用户取消就如实说明，不要换个路径再试。"
    "凡是问到实时信息——新闻、行情、版本更新、政策变动、人物职位、价格——"
    "先用 searchWe 搜一遍再回答，不要拿训练数据当最新事实。"
    "搜索摘要只是一段概括，需要核对细节、看原文、读文档或用户给了链接时，"
    "再用 FetchWeb 把那个网页的正文抓下来读。"
    "引用搜索结果时，要把结论对应到它给出的链接；查不到就直说查不到，不要补全。"
    "需要执行本机程序时（查版本、跑 git/npm/python 脚本、看系统信息）用 RunCli："
    "程序名放 command，参数逐个放进 args 数组，不要拼整条命令。"
    "读文件、列目录、写文件优先用专用文件工具，不要用 dir/type/copy 这类 cmd 内建命令。"
    "RunCli 会返回退出码、stdout 和 stderr：退出码不为 0 就是没跑成，"
    "先按 stderr 里的原因调整，不要拿同样的参数重复调用。"
)

# 有技能时拼在基础提示词后面。清单只给 name 和 description（注册阶段就这些），
# 完整说明让模型自己用 loadSkill 按需取。
SKILL_PROMPT_SECTION = (
    "\n\nYou have access to the following skills:\n"
    "%s\n"
    'Use the "loadSkill" tool to load the full instructions of a skill when needed. '
    "You can also load additional files referenced by a skill: "
    "传 loadSkill 的 filename 参数（支持技能目录内相对路径、项目相对路径、"
    "绝对路径或裸文件名）。"
    "任务和某个技能的 description 对得上、或用户点名要用某个技能时，"
    "先 loadSkill 拿到完整指引再动手；只是顺便一提的事不必加载。"
)


def build_system_prompt(skills: Optional[SkillCatalog] = None) -> str:
    """组装系统提示词：无技能返回原文；有技能追加 name/description 清单。"""
    if not skills or not skills.skills:
        return SYSTEM_PROMPT
    listing = "\n".join(
        '- "%s"：%s' % (s.name, s.description) for s in skills.skills)
    return SYSTEM_PROMPT + SKILL_PROMPT_SECTION % listing


@dataclass
class TurnStats:
    """一轮问答的小结，界面可以拿去做状态栏。"""
    rounds: int = 0
    tool_calls: int = 0
    prompt_tokens: Optional[int] = None


class Session:
    """一次会话。通常是"一个用户开一个界面"对应一个 Session。"""

    def __init__(
        self,
        config: AppConfig,
        client: Optional[LLMClient] = None,
        registry: Optional[ToolRegistry] = None,
        *,
        sink: Optional[EventSink] = None,
        confirm: Optional[ConfirmPort] = None,
        allow_tools: bool = True,
        skills: Optional[SkillCatalog] = None,
    ) -> None:
        self.config = config
        # 依赖全部从外部注入，默认值只是"常用的那一套"
        self.client = client or LLMClient(config.base_url, config.api_key)
        self.registry = registry if registry is not None else build_default_registry()
        self.sink = sink
        self.confirm = confirm
        self.allow_tools = allow_tools
        # 技能发现与注册：初始化阶段只读 name/description，正文按需再说。
        # 传进来的 catalog 优先（测试时好替换）；找不到技能目录就得到空表，
        # 不报错——没有技能是常态，不是故障。
        self.skills = skills if skills is not None else discover_skills()
        # 系统提示词在初始化时拼好一次：技能清单来自注册表，注册表不在会话中途变
        self._system_prompt = build_system_prompt(self.skills)

        # messages 里不放 system：它每次请求时临时拼在最前面，
        # 这样历史裁剪不用考虑它的位置
        self.messages: List[Dict[str, Any]] = []
        self.stats = TurnStats()
        self._closed = False

    # ---------------------------------------------------------------- 生命周期

    def attach(self, sink: EventSink) -> None:
        """界面启动后把自己的 sink 接上来。"""
        self.sink = sink

    def clear(self) -> None:
        """清空历史（对应 /clear 命令）。"""
        self.messages.clear()
        self._emit(Notice("对话历史已清空。"))

    @property
    def history_size(self) -> int:
        return len(self.messages)

    # ---------------------------------------------------------------- 事件出口

    def _emit(self, event) -> None:
        if self.sink is not None:
            self.sink.emit(event)

    def _notice(self, text: str, level: str = "info") -> None:
        self._emit(Notice(text, level))

    # ---------------------------------------------------------------- 主流程

    def ask(self, user_text: str) -> Optional[AssistantMessage]:
        """处理一句话，直到拿到最终回答。

        返回最后那条 assistant 消息；整轮失败返回 None（历史会回滚到本轮之前）。
        这个方法是同步阻塞的：TUI 里要放到线程或 async worker 里跑。
        """
        text = (user_text or "").strip()
        if not text:
            return None
        if self._closed:
            self._notice("会话已关闭。", "warning")
            return None

        snapshot = len(self.messages)          # 失败时回滚到这里
        self.messages.append({"role": "user", "content": text})
        self.stats = TurnStats()

        try:
            final = self._tool_loop()
        except LLMError as e:
            # 只回滚"这一轮还没落地的部分"。
            # 注意不能退回到 snapshot：assistant(tool_calls) 已经写进历史了，
            # 只删它后面的 tool 结果，会留下"发起了调用却没有响应"的残缺结构，
            # 下一轮请求会被接口判 400。所以这里保持历史原样，只报告错误。
            self._emit(ErrorOccurred(str(e), self.stats.rounds))
            return None
        except Exception as e:  # noqa: BLE001 —— 兜底，别让界面线程炸掉
            self._emit(ErrorOccurred("未预期的错误：%s" % e, self.stats.rounds))
            return None

        if final is not None:
            self._emit(ReplyCompleted(
                text=final.content,
                rounds=self.stats.rounds,
                tool_calls=self.stats.tool_calls,
            ))
        return final

    def _tool_loop(self) -> Optional[AssistantMessage]:
        """一轮提问可能要好几次"模型→工具→模型"，这里循环处理。"""
        max_rounds = max(int(self.config.max_tool_rounds or 1), 1)

        for round_index in range(1, max_rounds + 1):
            self.stats.rounds = round_index
            self._emit(RoundStarted(round_index))

            self._trim_history()
            payload = self._build_messages()
            message = self._collect(payload)

            # 不管有没有工具调用，assistant 这条消息都要进历史，
            # 否则下一轮模型不知道自己刚发起过调用
            self.messages.append(message.to_api())

            if not message.tool_calls:
                return message

            self._run_tools(message.tool_calls)

        self._notice("工具连着调了 %d 轮还没结束，先停在这里。" % max_rounds, "warning")
        return AssistantMessage(content="")

    # ---------------------------------------------------------------- 单次请求

    def _collect(self, payload: List[Dict[str, Any]]) -> AssistantMessage:
        """发一次请求，把 SSE 增量拼成完整消息。"""
        text_parts: List[str] = []
        calls: Dict[int, ToolCall] = {}      # index -> 累积中的调用

        for delta in self.client.chat_stream(
            payload, self.config.model, self.registry.schemas() if self.allow_tools else None
        ):
            if delta.done:
                break

            if delta.content:
                text_parts.append(delta.content)
                # 逐片往外推，UI 想做成打字机就现成可用
                self._emit(TextDelta(delta.content))

            self._accumulate_call(calls, delta)

            usage = (delta.raw or {}).get("usage") if isinstance(delta.raw, dict) else None
            if usage:
                self._emit(TokenUsage(
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                    total_tokens=usage.get("total_tokens"),
                ))
                self.stats.prompt_tokens = usage.get("prompt_tokens")

        ordered = [calls[i] for i in sorted(calls)]
        return AssistantMessage(content="".join(text_parts), tool_calls=ordered)

    @staticmethod
    def _accumulate_call(calls: Dict[int, ToolCall], delta: StreamDelta) -> None:
        """tool_calls 是分片推来的：name 和 arguments 都要 += 拼，不能覆盖。"""
        if delta.tool_call_index is None:
            return
        idx = delta.tool_call_index
        call = calls.get(idx)
        if call is None:
            call = ToolCall(id="", name="")
            calls[idx] = call
        if delta.tool_call_id:
            call.id = delta.tool_call_id
        if delta.tool_name:
            call.name += delta.tool_name
        if delta.tool_arguments:
            call.arguments += delta.tool_arguments

    # ---------------------------------------------------------------- 工具执行

    def _run_tools(self, calls: List[ToolCall]) -> None:
        ctx = ToolContext(
            max_read_chars=self.config.max_read_chars,
            confirm=self._ask,
            # 搜索相关的凭据从配置流进来，工具自己不碰文件、不碰环境变量
            search_api_key=self.config.search_api_key,
            search_model=self.config.search_model,
            search_timeout=self.config.search_timeout,
            fetch_timeout=self.config.fetch_timeout,
            fetch_max_chars=self.config.fetch_max_chars,
            cli_timeout=self.config.cli_timeout,
            cli_max_chars=self.config.cli_max_chars,
            skills=self.skills,
            config_path=self.config.config_path,
        )
        for i, call in enumerate(calls):
            if not call.id:
                # 少数兼容实现会漏 id，补一个，否则回传时接口拒收
                call.id = "call_%d" % i
            self.stats.tool_calls += 1
            self._emit(ToolCallStarted(call.id, call.name, call.arguments))

            entry = self.registry.get(call.name)
            # 危险工具在执行前问一声；确认口由界面提供。
            # 要点：用户拒绝时也必须回一条 role=tool ——
            # 协议要求 assistant 的每个 tool_call 都有对应的结果，
            # 少一条，下一轮请求就会因为"工具调用没有响应"被拒。
            if entry is not None and entry.danger is DangerLevel.DESTRUCTIVE:
                if not self._confirm_danger(call):
                    result_text = "用户取消了操作：%s" % call.name
                    self._emit(ToolCallFinished(call.id, call.name, result_text, ok=False))
                    self.messages.append({
                        "role": "tool", "tool_call_id": call.id, "content": result_text,
                    })
                    continue

            result = self.registry.invoke(call.name, call.arguments, ctx)
            self._emit(ToolCallFinished(call.id, call.name, result.content, ok=result.ok))
            self.messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": result.content,
            })

    def _confirm_danger(self, call: ToolCall) -> bool:
        """把"要删东西了"翻译成人类语言，再交给确认口。"""
        args = call.arguments or ""
        title = "删除文件" if call.name == "deleteFile" else "删除目录"
        return self._ask(title, "模型请求%s，参数：\n%s" % (title, args))

    def _ask(self, title: str, message: str) -> bool:
        if self.confirm is None:
            # 没有确认口就一律不执行危险操作 —— 宁可不动手
            return False
        return bool(self.confirm.ask(title, message))

    # ---------------------------------------------------------------- 历史管理

    def _build_messages(self) -> List[Dict[str, Any]]:
        return [{"role": "system", "content": self._system_prompt}] + self.messages

    def _trim_history(self) -> None:
        """超长历史按 user 边界整组丢弃。

        不能两条两条地丢：tool 消息必须紧跟它对应的 assistant(tool_calls)，
        切散了对，接口会直接报 400。
        """
        limit = int(self.config.max_history_chars or 0)
        if not limit:
            return

        def total() -> int:
            return sum(len(str(m.get("content") or "")) for m in self.messages)

        dropped = 0
        while len(self.messages) > 1 and total() > limit:
            cut = next((i for i in range(1, len(self.messages))
                        if self.messages[i].get("role") == "user"), None)
            if cut is None:
                break
            del self.messages[:cut]
            dropped += 1

        if dropped:
            self._notice("对话太长，已自动省略最早的 %d 轮历史。" % dropped, "warning")


def build_session(config: AppConfig, sink: Optional[EventSink] = None,
                  confirm: Optional[ConfirmPort] = None,
                  allow_tools: bool = True) -> Session:
    """给界面用的便捷构造函数：注册表默认全量内置工具。"""
    return Session(config, sink=sink, confirm=confirm, allow_tools=allow_tools)


__all__ = ["Session", "TurnStats", "SYSTEM_PROMPT", "build_session",
           "build_system_prompt", "SKILL_PROMPT_SECTION"]
