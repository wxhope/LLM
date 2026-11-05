# -*- coding: utf-8 -*-
"""Textual 界面。

结构：
    HeaderBar   顶栏：模型、目录、工具数
    Transcript  消息区（VerticalScroll）：一条消息一个 Static
    Composer    输入框：Enter 发送，Shift+Enter 换行
    StatusBar   底栏：状态 + 快捷键提示

线程模型（关键）：
    UI 线程只做渲染；
    Session.ask() 丢进 @work(thread=True) 的后台线程；
    后台线程通过 QueueSink 往队列里丢事件；
    UI 线程用 set_interval 每 40ms 排空队列并渲染。

    绝对不要在 UI 线程里直接调 Session.ask()，界面会整个冻住。

为什么不是 RichLog（逐字输出的关键）：
    RichLog 只能"追加整行"，一行写进去就定了，没法回头改。
    想做到一个字一个字往外冒，就必须有"能被反复重写的那一行"。
    所以换成 VerticalScroll + Static：
        正在生成的那条 = 一个 Static，每帧 update() 整个重绘；
        生成结束 = 不再 update，它就自然变成历史消息。
"""

from __future__ import annotations

import os
import time
from typing import Optional

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Input, Static

from ..core.config import AppConfig, ConfigError, load_config
from ..core.events import (
    ContextUsageChanged,
    ErrorOccurred,
    Notice,
    ReplyCompleted,
    RoundStarted,
    TextDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from ..core.session import Session, build_session
from .bridge import QueueSink, TextualConfirm

# 队列轮询间隔（秒）。40ms ≈ 25fps，看着顺滑又不浪费 CPU
POLL_INTERVAL = 0.04

# 打字机的加速闸门：模型一次性吐来的字如果积压超过这个数，
# 就不再按固定速率慢慢放，而是按积压量追帧，把延迟压在几百毫秒内。
# 没有这个兜底，模型 3 秒生成的 300 字要放 9 秒，等于把"流式"变成"回放"。
FAST_FORWARD_CHARS = 40
FAST_FORWARD_DIVISOR = 5      # 追帧时每帧吃掉积压的 1/5 ≈ 5 帧（200ms）追平

# 收尾阶段（模型已经说完，但字还没放完）的追赶速度。
# 3 字/帧是下限：再大就不是"逐字"，而是一帧蹦出一整段，
# 余量小的短回答会直接闪过去，看不出打字机效果。
DRAIN_MIN_STEP = 3
DRAIN_DIVISOR = 8             # 每帧吃掉积压的 1/8 ≈ 8 帧（320ms）追平

# 光标：只在生成过程中挂在末尾，定格时去掉
CURSOR = "▍"

# 助手消息的行首前缀。续行用等宽空格补齐，避免换行后顶格成新段落。
PREFIX = "xiaoA ▸ "
PREFIX_WIDTH = len(PREFIX)


def fmt_k(tokens: int) -> str:
    """按 1000 进位显示（模型窗口都以 128k = 128000 这种十进制标称）。

    128000 → "128k"，42000 → "42k"，4200 → "4.2k"，900 → "900"。
    用 1024 会把 128k 显示成 125.0k，看着像另一个模型，白白让人怀疑配置。
    """
    if tokens < 1000:
        return str(tokens)
    value = tokens / 1000.0
    if value >= 100:
        return "%.0fk" % value
    return "%.1fk" % value


def format_context(tokens: int, window: int, percent: float,
                   window_is_default: bool) -> str:
    """状态栏里的那一段：上下文 42.3k / 128k（33%）默认

    「默认」两个字是要求里点名的：窗口大小不是从模型拿的，就得说明白，
    否则用户会以为 128k 是这家模型的真实上限。
    """
    text = "上下文 %s / %s（%.0f%%）" % (fmt_k(tokens), fmt_k(window), percent)
    if window_is_default:
        text += " 默认"
    return text

HELP_TEXT = (
    "命令：/help 帮助  /clear 清空历史  /compact 压缩上下文  /quit 退出  "
    "/model 查看模型  /tools 列出工具"
)


class HeaderBar(Static):
    """顶栏。"""


class StatusBar(Static):
    """底栏。"""


class Message(Static):
    """消息区里的一条消息。

    markup=False 是硬性要求：工具结果里全是 [ ] { } 和反斜杠路径，
    开着 markup 会被 Textual 当标签解析，轻则显示错乱，重则抛异常。

    plain 是自己留的一份纯文本：
      - 不用回调 Textual 内部结构去取，换 Textual 版本也不会坏；
      - 流式时每帧改的就是它，测试也读它。
    """

    def __init__(self, text: str = "", **kwargs) -> None:
        kwargs.setdefault("markup", False)
        super().__init__(text, **kwargs)
        self.plain = text

    def set_text(self, text: str) -> None:
        self.plain = text
        self.update(text)


class XiaoAApp(App):
    """主界面。"""

    CSS_PATH = "app.tcss"
    TITLE = "xiaoA · 本机助手"
    SUB_TITLE = "core + tui 分层架构"
    BINDINGS = [
        ("ctrl+q", "quit_app", "退出"),
        ("ctrl+l", "clear_history", "清空历史"),
        ("ctrl+t", "show_tools", "工具清单"),
        ("f1", "show_help", "帮助"),
    ]

    def __init__(self, config: Optional[AppConfig] = None,
                 config_path: Optional[str] = None) -> None:
        super().__init__()
        self._config_path = config_path
        self.config_obj = config
        self.sink = QueueSink()
        self.confirm_port = TextualConfirm()
        self.session: Optional[Session] = None
        self.busy = False
        self._started_at = time.time()
        self._pending_confirm = False          # 有一个确认框正等着回答

        # ---- 上下文占用（状态栏显示 + 到阈值后拦输入）
        self._ctx_text = ""                    # 例如 "上下文 42.3k / 128k（33%）默认"
        self._ctx_blocked = False              # True = 等 /compact，暂停接收提问
        self._status_text = "启动中"
        self.status_line = ""                  # 状态栏最后渲染的那一行（便于测试）

        # ---- 流式状态（逐字输出）
        # 收到 = _live_target（模型已经推来的全文）
        # 显示 = _live_shown（已放到屏幕上的字符数）
        # 两者之差就是积压，_advance_stream 每帧追一点
        self._live: Optional[Message] = None   # 正在生成的那条 Static
        self._live_target = ""
        self._live_shown = 0
        self._live_drain = False               # 模型已说完，正在快速放完余量
        self._credit = 0.0                     # 打字机速率的小数余额，免得被 int() 抹掉
        self._type_delay = (config.type_delay if config else 0.03)
        self._scroll_scheduled = False          # 这一帧是否已经排过滚动

    # ---------------------------------------------------------------- 布局

    def compose(self) -> ComposeResult:
        with Vertical():
            yield HeaderBar(id="header-bar")
            # 消息区：VerticalScroll 里一条消息一个 Static。
            # 用它可以"重写最后一条"，RichLog 不行（它只认追加整行）。
            yield VerticalScroll(id="transcript")
            yield StatusBar(id="status")
            yield Input(placeholder="说点什么，Enter 发送，Shift+Enter 换行…",
                        id="composer")
            yield Static(HELP_TEXT, id="hint")

    async def on_mount(self) -> None:
        # 1) 读配置。失败不退出程序，而是把错误显示在界面上——
        #    用户看到的是"红字提示"，不是"窗口一闪而过"。
        #    这个方法必须是 async：sync 版的 on_mount 里调 @work 不会被派发。
        try:
            self.config_obj = self.config_obj or load_config(self._config_path)
        except ConfigError as e:
            self.refresh_header("未加载配置")
            self.write_error(str(e))
            self.write_notice("修好 config.ini 后重启程序即可。")
            self.set_status("配置缺失")
            self.query_one("#composer", Input).disabled = True
            return

        # 2) 装配 core：注入 sink 与 confirm，session 本身不知道 Textual 存在。
        #    已经预置了 session 的（测试 / 自定义客户端）不覆盖，只补上 sink。
        if self.session is None:
            self.session = build_session(
                self.config_obj, sink=self.sink, confirm=self.confirm_port)
            registry = self.session.registry
        else:
            self.session.sink = self.sink
            self.session.confirm = self.confirm_port
            registry = self.session.registry

        self.refresh_header()
        self._type_delay = self.config_obj.type_delay
        self.write_notice(
            "就绪。工具 %d 个，模型 %s。" % (len(registry), self.config_obj.model))
        if self.session.skills:
            self.write_notice(self.session.skills.summary())
            for problem in self.session.skills.problems:
                self.write_notice("技能提醒：%s" % problem)
        if self.session.mcp is not None:
            self.write_notice(self.session.mcp.summary)
            for problem in self.session.mcp.problems:
                self.write_notice("MCP提醒：%s" % problem)
        # 上下文占用：先把当前值算出来贴到状态栏（空历史就是 0）
        self._sync_context_usage()
        self.set_status("空闲")
        self.query_one("#composer", Input).focus()

        # 3) 开始轮询队列
        self.set_interval(POLL_INTERVAL, self.pump_events)

    # ---------------------------------------------------------------- 事件排空

    def pump_events(self) -> None:
        """UI 线程的固定节拍：取事件、渲染、看有没有待确认的请求。"""
        for event in self.sink.drain():
            self.render_event(event)

        # 事件处理完再推进打字机：这一帧新收到的字也一并算进积压，
        # 顺序反过来会出现"先放完旧字、再突然多出一段"的顿挫感。
        self._advance_stream()

        request = self.confirm_port.poll()
        if request is not None:
            self.show_confirm(*request)

    def render_event(self, event) -> None:
        if isinstance(event, TextDelta):
            self.append_text(event.text)
        elif isinstance(event, ToolCallStarted):
            self.write_tool_call(event.name, event.arguments)
            self.set_status("执行工具 %s…" % event.name)
        elif isinstance(event, ToolCallFinished):
            self.write_tool_result(event.result, event.ok)
            # 工具结果是"过程信息"。危险工具在等确认后迟迟没有新事件，
            # 这里把状态改成"继续中"，免得界面看起来像卡死。
            self.set_status("继续中…")
        elif isinstance(event, ReplyCompleted):
            self.end_stream()
            # 先复位 busy 再刷状态栏：set_status 里的圆点看的是 busy，
            # 顺序反了会把"空闲"画成 ●，看着像还在跑。
            self.busy = False
            self.set_status("空闲（%d 轮 / %d 次工具调用）"
                            % (event.rounds, event.tool_calls))
        elif isinstance(event, RoundStarted):
            if event.round_index > 1:
                self.finish_stream()
                self.set_status("第 %d 轮请求…" % event.round_index)
        elif isinstance(event, Notice):
            self.write_notice(event.text)
        elif isinstance(event, ContextUsageChanged):
            # 占用变了：刷状态栏。到阈值后同时把输入框提示换掉，
            # 让"为什么发不出去"一眼可见。
            self._ctx_text = format_context(event.tokens, event.window,
                                            event.percent, event.window_is_default)
            self._ctx_blocked = event.blocked
            self.set_status(self._status_text)
            self._update_placeholder()
        elif isinstance(event, ErrorOccurred):
            self.finish_stream()
            self.write_error(event.message)
            self.busy = False
            self.set_status("出错")

    # ---------------------------------------------------------------- 渲染小工具

    @property
    def transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def write(self, text: str) -> None:
        """往消息区追加一条定型消息。"""
        # 流式途中插进别的输出（工具调用、提示、错误），先把上文定格，
        # 否则新消息会挂在"还在变的"那条之后，看起来像被吞掉了。
        self.finish_stream()
        self.transcript.mount(Message(text, classes="msg"))
        self._scroll_end()

    # ---- 逐字流式：收到只登记，显示由每帧的 _advance_stream 决定

    def append_text(self, chunk: str) -> None:
        if not chunk:
            return
        if self._live is None:
            live = Message(PREFIX + CURSOR, classes="msg")
            self.transcript.mount(live)
            self._live = live
            self._live_target = ""
            self._live_shown = 0
            self._live_drain = False
            self._credit = 0.0
        self._live_target += chunk

    def _live_text(self, cursor: bool = True) -> str:
        """前缀 + 已显示的部分。续行补等宽空格，空行保持空行。"""
        lines = self._live_target[:self._live_shown].split("\n")
        head, rest = lines[0], lines[1:]
        if rest:
            body = "\n".join((" " * PREFIX_WIDTH + ln) if ln else "" for ln in rest)
            head = head + "\n" + body
        return PREFIX + head + (CURSOR if cursor else "")

    def _advance_stream(self) -> None:
        """每帧往屏幕上放几个字 —— 打字机效果的真正实现。"""
        if self._live is None:
            return

        total = len(self._live_target)
        if self._live_shown >= total:
            if self._live_drain:      # 收尾也追完了，定格
                self.finish_stream()
            return

        backlog = total - self._live_shown

        if self._type_delay <= 0:
            step = backlog            # 配置关掉了打字机，一次到底
        else:
            # 用小数余额累积，否则 int(0.04/0.03)=1 会把 33 字/秒压成 25 字/秒
            self._credit += POLL_INTERVAL
            step = int(self._credit / self._type_delay)
            if step > 0:
                self._credit -= step * self._type_delay
            if self._live_drain:
                # 模型已经说完，剩下的别让用户干等：8 帧内追平
                step = max(DRAIN_MIN_STEP, step, backlog // DRAIN_DIVISOR)
            elif backlog > FAST_FORWARD_CHARS:
                # 关键：显示速度不能长期慢于模型吐字速度，
                # 否则回答结束了还在"回放"，流式就白做了。
                step = max(step, backlog // FAST_FORWARD_DIVISOR)

        if step <= 0:
            return

        self._live_shown = min(total, self._live_shown + step)
        self._live.set_text(self._live_text())
        self._scroll_end()

    def end_stream(self) -> None:
        """本轮回复结束。余量少就直接定格，余量大就让打字机快速追完。"""
        if self._live is None:
            return
        if len(self._live_target) - self._live_shown <= 0:
            self.finish_stream()
        else:
            self._live_drain = True

    def finish_stream(self) -> None:
        """立即放完余量并定格 —— 用于插话、出错、清屏、退出。"""
        live = self._live
        if live is None:
            return
        self._live_shown = len(self._live_target)
        live.set_text(self._live_text(cursor=False))
        self._live = None
        self._live_target = ""
        self._live_shown = 0
        self._live_drain = False
        self._credit = 0.0

    def _scroll_end(self) -> None:
        """滚到底。

        节流按"帧"来，不是按"时间"：
          - 同一帧里 update 好几次、事件处理又调一次，只排一次滚动；
          - 跨帧必须各排一次——按时间节流会把最后一帧的滚动吞掉，
            表现就是回答越长、越看不到结尾（内容超出了可视区却没滚）。
        """
        if self._scroll_scheduled:
            return
        self._scroll_scheduled = True
        self.call_after_refresh(self._do_scroll_end)

    def _do_scroll_end(self) -> None:
        self._scroll_scheduled = False
        # force=True 必须给：否则 Textual 会认为"内容没变"而不滚。
        # 这里内容确实每帧都变了，但这个判断有缓存，靠不住。
        self.transcript.scroll_end(animate=False, force=True)

    # ---- 各类消息

    def write_user(self, text: str) -> None:
        self.write("你▸ " + text)

    def write_tool_call(self, name: str, args: str) -> None:
        self.write("  ⚙ 调用 %s(%s)" % (name, args or "{}"))

    def write_tool_result(self, result: str, ok: bool) -> None:
        head = "  ✓ 结果：" if ok else "  ✗ 结果："
        body = result.replace("\n", "\n    ")
        self.write(head + body)

    def write_notice(self, text: str) -> None:
        self.write("· " + text)

    def write_error(self, text: str) -> None:
        self.write("✗ " + text)

    def set_status(self, text: str) -> None:
        self._status_text = text
        model = self.config_obj.model if self.config_obj else "-"
        parts = ["●" if self.busy else "○", "模型 %s" % model]
        if self._ctx_text:
            parts.append(self._ctx_text)
        parts.append(text)
        line = " " + "   │   ".join(parts)
        self.status_line = line            # 留一份纯文本：调试和冒烟测试读它
        self.query_one("#status", StatusBar).update(line)

    # ---------------------------------------------------------------- 上下文占用

    def _sync_context_usage(self) -> None:
        """从会话层拉一次占用，贴到状态栏。

        为什么要在 UI 侧也主动拉：启动时还没有任何事件，
        状态栏不能空着——空着用户就不知道窗口多大、现在用了多少。
        """
        if self.session is None:
            return
        usage = self.session.context_usage()
        self._ctx_text = format_context(usage.tokens, usage.window,
                                        usage.percent, usage.window_is_default)
        self._ctx_blocked = self.session.blocked
        self._update_placeholder()

    def _update_placeholder(self) -> None:
        try:
            box = self.query_one("#composer", Input)
        except Exception:      # noqa: BLE001 - 界面还没挂载完时别炸
            return
        if self._pending_confirm:
            return
        box.placeholder = ("上下文已达阈值，输入 /compact 压缩后继续…"
                           if self._ctx_blocked
                           else "说点什么，Enter 发送，Shift+Enter 换行…")

    def refresh_header(self, extra: str = "") -> None:
        cwd = os.getcwd()
        tools = len(self.session.registry) if self.session else 0
        text = " xiaoA │ 工具 %d │ %s" % (tools, cwd)
        if extra:
            text += " │ " + extra
        self.query_one("#header-bar", HeaderBar).update(text)

    # ---------------------------------------------------------------- 确认框

    def show_confirm(self, title: str, message: str) -> None:
        """把删除确认做成一条醒目的消息 + 提示回 y/n。

        用 Input 收 y/n 而不是弹带按钮的模态框，是因为：
        输入框一直在焦点上，用户手指不用离开键盘，符合 TUI 的手感。
        """
        self._pending_confirm = True
        self.write(" ")          # 空行分隔：Static 内容为空时高度是 0，得塞个空格
        self.write("⚠ %s" % title)
        for line in message.splitlines():
            self.write("   " + line)
        self.write("   → 输入 y 确认，输入其它任意内容取消")
        self.set_status("等待确认")
        box = self.query_one("#composer", Input)
        box.placeholder = "确认执行？y / n"
        box.focus()

    def resolve_confirm(self, text: str) -> None:
        ok = text.strip().lower() in ("y", "yes", "是")
        self._pending_confirm = False
        self.write("   → %s" % ("已确认" if ok else "已取消"))
        self.confirm_port.reply(ok)
        box = self.query_one("#composer", Input)
        box.placeholder = "说点什么，Enter 发送，Shift+Enter 换行…"
        self.set_status("继续中…" if ok else "已取消，继续…")

    # ---------------------------------------------------------------- 输入

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # 兼容不同 Textual 版本：有的版本 Submitted 带 value，有的要回读控件
        text = (getattr(event, "value", "") or event.input.value or "").strip()
        event.input.value = ""

        if self._pending_confirm:
            self.resolve_confirm(text)
            return
        if not text:
            return
        if text.startswith("/"):
            self.handle_command(text)
            return
        if self.busy:
            self.write_notice("上一句还在处理中，稍等一下。")
            return
        if self.session is None:
            return
        # 到阈值后只认 /compact：普通提问在这里就拦下，不发请求（省钱也省配额）
        if self.session.blocked:
            self.write_notice("上下文已到阈值，请先输入 /compact 压缩上下文。")
            self._sync_context_usage()
            self.set_status(self._status_text)
            return

        self.write_user(text)
        self.busy = True
        self.set_status("思考中…")
        self.run_turn(text)

    @work(thread=True, exclusive=True)
    def run_turn(self, text: str) -> None:
        """后台线程里跑一整轮。注意：这里不能碰任何控件。"""
        assert self.session is not None
        try:
            self.session.ask(text)
        except Exception as e:  # noqa: BLE001 —— 后台线程抛异常会让界面无声卡住
            self.call_from_thread(self.write_error, "后台任务异常：%s" % e)
            self.call_from_thread(self._turn_done)

    def _turn_done(self) -> None:
        self.busy = False
        self._sync_context_usage()
        self.set_status(self._status_text)

    # ---------------------------------------------------------------- 压缩

    def start_compact(self) -> None:
        """由用户敲 /compact 触发。压缩在后台线程跑，界面照常响应。"""
        if self.session is None:
            return
        if self.busy:
            self.write_notice("上一句还在处理中，等它结束再压缩。")
            return
        if not self.session.messages:
            self.write_notice("当前没有对话历史，不需要压缩。")
            return
        self.finish_stream()
        self.busy = True
        self.set_status("压缩中…（期间不接受新提问）")
        self.run_compact()

    @work(thread=True, exclusive=True)
    def run_compact(self) -> None:
        """后台线程执行压缩。所有界面改动都通过 call_from_thread 回主线程。"""
        assert self.session is not None
        ok = False
        try:
            ok = bool(self.session.compact())
        except Exception as e:  # noqa: BLE001 —— 压缩失败不能让界面卡死
            self.call_from_thread(self.write_error, "压缩异常：%s" % e)
        self.call_from_thread(self._compact_done, ok)

    def _compact_done(self, ok: bool) -> None:
        self.busy = False
        self.end_stream()          # 摘要的流式输出收尾
        self._sync_context_usage()
        self.set_status("压缩完成" if ok else "压缩失败，可重试 /compact")

    # ---------------------------------------------------------------- 命令

    def handle_command(self, text: str) -> None:
        name, _, rest = text.partition(" ")
        cmd = name.lower()
        if cmd in ("/quit", "/exit", "/q"):
            self.action_quit_app()
        elif cmd == "/clear":
            if self.session is not None:
                self.session.clear()
                self.finish_stream()
                self.transcript.remove_children()
                self.write_notice("历史已清空。")
                self._sync_context_usage()
                self.set_status("空闲")
        elif cmd == "/compact":
            self.start_compact()
        elif cmd == "/help":
            self.action_show_help()
        elif cmd == "/tools":
            self.action_show_tools()
        elif cmd == "/model":
            if self.config_obj:
                self.write_notice("模型：%s ｜ 接口：%s ｜ Key：%s"
                                  % (self.config_obj.model,
                                     self.config_obj.base_url,
                                     self.config_obj.masked_key()))
        else:
            self.write_notice("不认识的命令：%s（/help 看帮助）" % name)

    def action_show_help(self) -> None:
        self.write_notice("命令：" + HELP_TEXT.replace("命令：", ""))
        self.write_notice("快捷键：Ctrl+Q 退出 │ Ctrl+L 清空 │ Ctrl+T 工具清单 │ F1 帮助")

    def action_show_tools(self) -> None:
        if self.session is None:
            return
        reg = self.session.registry
        items = []
        for name in reg.names():
            item = reg.get(name)
            mark = "⚠ " if item and item.danger.value == "destructive" else "  "
            items.append("%s%s" % (mark, name))
        self.write_notice("工具（%d）：%s" % (len(items), "  ".join(items)))

    def action_clear_history(self) -> None:
        self.handle_command("/clear")

    def action_quit_app(self) -> None:
        if self.busy:
            self.write_notice("还在处理中，再按一次 Ctrl+Q 可强制退出。")
            self.busy = False
        self.exit()


def run(config_path: Optional[str] = None) -> None:
    """入口：python -m xiaoA.tui"""
    XiaoAApp(config_path=config_path).run()
