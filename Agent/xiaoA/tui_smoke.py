# -*- coding: utf-8 -*-
"""TUI 无头验证：不需要真终端，也能跑一遍界面逻辑。

用 Textual 的 run_test() 在后台驱动 App，用假 LLM 客户端代替真接口，
所以不联网、不花 token、不用人盯着屏幕。

验证三件事：
  1. 布局挂得上，命令有反应
  2. tool-calling 完整回路能跑（模型要工具 → 工具返回 → 模型作答）
  3. 删除确认框出现，回答 n 之后文件确实没被删
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from xiaoA.core.config import AppConfig
from xiaoA.core.llm.types import StreamDelta
from xiaoA.core.session import Session
from xiaoA.core.tools import ToolRegistry
from xiaoA.core.tools.builtin import datetime_ops, fs_ops, math_ops
from xiaoA.tui.app import HELP_TEXT, Message, XiaoAApp

FAILS = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(("  [OK]  " if ok else "  [!!]  ") + label + (("  " + extra) if extra else ""))
    if not ok:
        FAILS.append(label)


class FakeClient:
    """按脚本吐 SSE 片段。每调用一次 chat_stream 就消耗一段脚本。

    gap：每片之间睡多久。默认 0（瞬间推完，测快路径）；
    测逐字输出时要给一个真实的值，否则服务端"秒回全文"，
    再怎么打字机也只能追帧，测不出中间过程。
    """

    def __init__(self, scripted, gap: float = 0.0):
        self.scripted = list(scripted)
        self.calls = 0
        self.seen_tools = None
        self.gap = gap

    def chat_stream(self, messages, model, tools=None):
        self.calls += 1
        self.seen_tools = tools
        for delta in self.scripted.pop(0):
            if self.gap:
                time.sleep(self.gap)
            yield delta
        yield StreamDelta(done=True)


def text_deltas(text: str, size: int = 3):
    return [StreamDelta(content=text[i:i + size]) for i in range(0, len(text), size)]


def call_deltas(call_id: str, name: str, arguments: str):
    """模拟分片推来的 tool_calls：名字和参数各来一片。"""
    half = max(len(arguments) // 2, 1)
    return [
        StreamDelta(tool_call_index=0, tool_call_id=call_id,
                    tool_name=name, tool_arguments=arguments[:half]),
        StreamDelta(tool_call_index=0, tool_arguments=arguments[half:]),
    ]


def make_app(registry: ToolRegistry, client) -> XiaoAApp:
    cfg = AppConfig(api_key="sk-test", base_url="https://example.invalid",
                    model="fake-model", max_read_chars=1000, max_tool_rounds=3)
    app = XiaoAApp(config=cfg)
    app.session = Session(cfg, client=client, registry=registry,
                          sink=app.sink, confirm=app.confirm_port)
    return app


def screen_text(app: XiaoAApp) -> str:
    """把消息区里所有 Static 的纯文本拼起来。

    改成 VerticalScroll 之后不能再读 RichLog.lines，
    这里读的是 Message.plain —— 我们自己留的那份原文，
    不依赖 Textual 内部结构（流式期间它每帧都在变，读到的就是"当前屏幕上"）。
    """
    return "\n".join(m.plain for m in app.query(Message))


async def settle(pilot, frames: int = 40) -> None:
    """多推几帧，让后台线程跑完、队列排空。

    每帧真的 sleep 一小会儿：逐字输出是靠 40ms 定时器推进的，
    纯 pause() 几乎不耗真实时间，打字机根本来不及把字放完。
    """
    for _ in range(frames):
        await pilot.pause()
        await asyncio.sleep(0.01)


async def case_layout() -> None:
    print("\n【场景一】布局与命令")
    client = FakeClient([])
    app = make_app(ToolRegistry([datetime_ops.get_date, math_ops.is_leap_year_tool]), client)
    async with app.run_test(size=(110, 32)) as pilot:
        await settle(pilot, 5)
        check("transcript 存在", bool(app.query("#transcript")))
        check("composer 存在", bool(app.query("#composer")))
        check("status 存在", bool(app.query("#status")))
        check("hint 存在", bool(app.query("#hint")))
        await pilot.press("ctrl+t")
        await settle(pilot, 5)
        check("Ctrl+T 列出工具", "getDate" in screen_text(app))
        # 上下文占用必须一直在状态栏上：当前 k / 窗口 k（%）+「默认」提示
        status = app.status_line
        check("状态栏显示上下文占用", "上下文" in status and "128k" in status, status)
        check("窗口是默认值时标注「默认」", "默认" in status, status)
        app.query_one("#composer").value = "/model"
        await pilot.press("enter")
        await settle(pilot, 5)
        check("/model 显示假模型名", "fake-model" in screen_text(app))
        check("帮助里提到 /compact", "/compact" in HELP_TEXT)


async def case_tool_round() -> None:
    print("\n【场景二】tool-calling 完整回路")
    client = FakeClient([
        call_deltas("call_1", "getDate", "{}"),
        text_deltas("现在是 10 月 22 日。"),
    ])
    reg = ToolRegistry([datetime_ops.get_date])
    app = make_app(reg, client)
    async with app.run_test(size=(110, 32)) as pilot:
        await settle(pilot, 5)
        app.query_one("#composer").value = "现在几点"
        await pilot.press("enter")
        await settle(pilot, 60)
        text = screen_text(app)
        check("渲染了工具调用", "调用 getDate" in text, "")
        check("渲染了工具结果", "当前系统日期时间" in text)
        check("渲染了最终回答", "现在是 10 月 22 日" in text)
        check("一共发了 2 次请求", client.calls == 2, "calls=%d" % client.calls)
        check("请求带上了 tools", bool(client.seen_tools), "tools=%s"
              % ("有" if client.seen_tools else "无"))
        check("busy 已复位", app.busy is False)


async def case_confirm() -> None:
    print("\n【场景三】删除确认：回答 n，文件必须还在")
    work = os.path.join(tempfile.gettempdir(), "xiaoA_tui_check")
    os.makedirs(work, exist_ok=True)
    victim = os.path.join(work, "victim.txt")
    with open(victim, "w", encoding="utf-8") as f:
        f.write("keep me")

    client = FakeClient([call_deltas(
        "call_2", "deleteFile", '{"path": "%s"}' % victim.replace("\\", "\\\\"))])
    app = make_app(ToolRegistry([fs_ops.delete_file]), client)

    async with app.run_test(size=(110, 32)) as pilot:
        await settle(pilot, 5)
        app.query_one("#composer").value = "删掉它"
        await pilot.press("enter")

        # 等确认框出现
        for _ in range(120):
            await pilot.pause()
            if app._pending_confirm:
                break
        check("确认框出现了", app._pending_confirm is True)
        check("提示里有文件名", "victim.txt" in screen_text(app))

        app.query_one("#composer").value = "n"
        await pilot.press("enter")
        await settle(pilot, 120)

        check("回答 n 后显示已取消", "已取消" in screen_text(app))
        check("文件确实还在", os.path.exists(victim))
        # 确认是 ConfirmPort 拦下的：文件完好，且工具结果明确回了"取消"
        msgs = app.session.messages
        tools = [m for m in msgs if m.get("role") == "tool"]
        check("取消已回填给模型", "取消" in str(tools[-1].get("content")) if tools else False,
              "tool 消息 %d 条" % len(tools))
        # 只喂了一段脚本，第二次请求拿不到内容 → 必然抛 LLMError；
        # 这里要验证的是"出错后历史没被切碎"，而不是"有没有成功收尾"
        check("出错后历史结构完整", len(msgs) >= 3
              and msgs[0].get("role") == "user"
              and bool(msgs[1].get("tool_calls"))
              and msgs[-1].get("role") == "tool", "共 %d 条" % len(msgs))
        check("确认状态已清除", app._pending_confirm is False)

    os.remove(victim)
    os.rmdir(work)


async def case_clear() -> None:
    print("\n【场景四】/clear 清空消息区")
    client = FakeClient([text_deltas("这是一段会被清掉的回答，看看还在不在。")])
    app = make_app(ToolRegistry([]), client)
    async with app.run_test(size=(110, 32)) as pilot:
        await settle(pilot, 5)
        app.query_one("#composer").value = "说点话"
        await pilot.press("enter")
        await settle(pilot, 40)
        check("回答已渲染", "这是一段会被清掉" in screen_text(app))

        app.query_one("#composer").value = "/clear"
        await pilot.press("enter")
        await settle(pilot, 20)
        left = [m.plain for m in app.query(Message)]
        joined = " / ".join(left)
        # core 的 session.clear() 自己也会发一条 Notice，所以"只剩一条"是错的断言，
        # 要验的是：旧回答没了、提示还在。
        check("旧回答已被清掉", "这是一段会被清掉" not in joined, joined)
        check("留下了清空提示", "历史已清空" in joined, joined)
        check("流式状态已复位", app._live is None)


async def case_typing() -> None:
    print("\n【场景五】逐字输出：字是一个一个冒出来的")
    text = "".join("第%d段：这里用来验证逐字输出，每一帧屏幕只该多出几个字。" % i
                   for i in range(1, 6))
    client = FakeClient([text_deltas(text, 4)], gap=0.01)
    app = make_app(ToolRegistry([]), client)
    async with app.run_test(size=(110, 32)) as pilot:
        await settle(pilot, 3)
        app.query_one("#composer").value = "说说"
        await pilot.press("enter")

        seq = []
        for _ in range(150):
            await pilot.pause()
            await asyncio.sleep(0.01)
            live = app._live
            seq.append(len(live.plain) if live is not None else -1)
            if app.busy is False and live is None and len(seq) > 5:
                break

        shown = [n for n in seq if n >= 0]
        distinct = sorted(set(shown))
        check("出现多个中间态（不是一次性蹦完）", len(distinct) >= 5,
              "中间态 %d 个，轨迹 %s" % (len(distinct), distinct[:8]))
        check("字数只增不减", all(b >= a for a, b in zip(shown, shown[1:])))
        final = screen_text(app)
        check("最终文本完整", text[:24] in final)
        check("定格后没有残留光标", "▍" not in final)


async def case_compact() -> None:
    print("\n【场景六】/compact：阈值拦截 + 压缩 + 解禁")
    ctxdir = os.path.join(tempfile.mkdtemp(prefix="xiaoA_tui_ctx_"), "ctx")
    cfg = AppConfig(api_key="sk-test", base_url="https://example.invalid",
                    model="fake-model", max_read_chars=1000, max_tool_rounds=3,
                    context_window=1000)      # 故意给小窗口，方便触发 70%
    client = FakeClient([text_deltas("## 工作摘要\n- 已完成冒烟测试")])
    app = XiaoAApp(config=cfg)
    app.session = Session(cfg, client=client, registry=ToolRegistry(),
                          sink=app.sink, confirm=app.confirm_port,
                          context_dir=ctxdir)
    async with app.run_test(size=(110, 32)) as pilot:
        await settle(pilot, 5)
        # 塞一大段历史，把占用顶过 70%
        app.session.messages = [{"role": "user", "content": "重要内容" * 200}]
        app.session._refresh_context_usage()
        await settle(pilot, 3)
        check("到阈值后状态栏显示占用", "上下文" in app.status_line, app.status_line)
        check("输入框提示已切换", "compact" in app.query_one("#composer").placeholder)

        app.query_one("#composer").value = "还想继续问"
        await pilot.press("enter")
        await settle(pilot, 5)
        check("阻断时提问被拦下", "请先输入 /compact" in screen_text(app))
        check("阻断时没有发请求", client.calls == 0, "calls=%d" % client.calls)

        app.query_one("#composer").value = "/compact"
        await pilot.press("enter")
        await settle(pilot, 80)
        check("压缩完成提示出现", "压缩完成" in screen_text(app))
        check("压缩只发 1 次请求且不带 tools",
              client.calls == 1 and client.seen_tools is None,
              "calls=%d tools=%s" % (client.calls, client.seen_tools))
        check("历史已替换为摘要",
              len(app.session.messages) == 1
              and "压缩摘要" in app.session.messages[0]["content"])
        check("归档文件已生成",
              os.path.isdir(ctxdir) and bool(os.listdir(ctxdir)), ctxdir)
        check("压缩后解除阻断", not app.session.blocked)


async def case_sessions() -> None:
    print("\n【场景七】命令补全面板 + /save /new /resume")
    tmp = tempfile.mkdtemp(prefix="xiaoA_tui_sess_")
    client = FakeClient([text_deltas("第一段回答。"), text_deltas("第二段回答。")])
    app = make_app(ToolRegistry([]), client)
    app.session.sessions_dir = tmp          # 会话存到临时目录，不碰真实数据
    async with app.run_test(size=(110, 32)) as pilot:
        await settle(pilot, 5)
        # ---- 补全面板：敲 / 就该浮出来，Tab 能补全
        app.query_one("#composer").value = "/s"
        await settle(pilot, 3)
        panel = app.query_one("#cmd-panel")
        check("输入 /s 弹出补全面板", panel.has_class("visible"))
        check("候选里包含 /save 与 /sessions",
              "/save" in str(panel.render()) and "/sessions" in str(panel.render()))
        await pilot.press("tab")
        await settle(pilot, 3)
        check("Tab 补全成第一个候选", app.query_one("#composer").value.startswith("/save"))
        # ---- 对话一轮，/save 落盘
        app.query_one("#composer").value = "第一句话"
        await pilot.press("enter")
        await settle(pilot, 30)
        app.query_one("#composer").value = "/save"
        await pilot.press("enter")
        await settle(pilot, 10)
        from xiaoA.core.session_store import list_sessions
        metas = list_sessions(tmp)
        check("会话已保存", len(metas) == 1 and metas[0].message_count >= 2,
              "存了 %d 个" % len(metas))
        # ---- /new：自动保存当前，历史清零
        app.query_one("#composer").value = "第二句话"
        await pilot.press("enter")
        await settle(pilot, 30)
        app.query_one("#composer").value = "/new"
        await pilot.press("enter")
        await settle(pilot, 10)
        check("/new 自动保存了旧会话", len(list_sessions(tmp)) == 2)
        check("/new 清空了历史", app.session.messages == [], str(app.session.messages)[:60])
        check("/new 后消息区已清屏",
              "第一段回答" not in screen_text(app))
        # ---- /resume：面板出现，ESC 取消
        app.query_one("#composer").value = "/resume"
        await pilot.press("enter")
        await settle(pilot, 10)
        check("恢复面板弹出", bool(app.screen.has_class("ResumePanel"))
              or app.screen.__class__.__name__ == "ResumePanel",
              app.screen.__class__.__name__)
        await pilot.press("escape")
        await settle(pilot, 10)
        check("ESC 取消恢复", "已取消恢复" in screen_text(app))
        # ---- /resume：回车选中第一条（最新保存的）并恢复
        app.query_one("#composer").value = "/resume"
        await pilot.press("enter")
        await settle(pilot, 10)
        await pilot.press("enter")
        await settle(pilot, 15)
        text = screen_text(app)
        check("面板回车后恢复会话", "已恢复会话" in text)
        check("历史被回放到消息区", "第一句话" in text, text[:200])
        check("恢复后 core 历史条数正确", len(app.session.messages) >= 3,
              "%d 条" % len(app.session.messages))
    # 清理临时会话目录
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


async def main() -> int:
    await case_layout()
    await case_tool_round()
    await case_confirm()
    await case_clear()
    await case_typing()
    await case_compact()
    await case_sessions()
    print()
    if FAILS:
        print("有问题（%d 项）：%s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("OK：TUI 七个场景全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(asyncio.wait_for(main(), timeout=120)))
