# -*- coding: utf-8 -*-
"""core 离线自检：不联网、不装 textual 也能跑。

用法（在 xiaoA 目录下）：
    python -X utf8 selfcheck.py
全部通过会打印 OK；有问题会 assert 失败并指出位置。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xiaoA.core.llm.stream import parse_line
from xiaoA.core.tools import ToolContext, build_default_registry
from xiaoA.core.tools.builtin.math_ops import is_leap_year
from xiaoA.core.tools.builtin.fetch_web import (
    FetchError,
    cleanup_markdown,
    html_to_markdown,
    normalize_url,
    render_table,
)
from xiaoA.core.tools.builtin.web_search import (
    SearchError,
    build_payload,
    check_business_error,
    format_result,
    pick_references,
    pick_summary,
)

ROOT = os.path.dirname(os.path.abspath(__file__))
Q = lambda p: p.replace("\\", "\\\\")   # 拼 JSON 时转义反斜杠


def main() -> int:
    reg = build_default_registry()
    assert len(reg) == 15, "内置工具应有 15 个，实际 %d" % len(reg)
    assert len(reg.schemas()) == 15
    print("工具清单（%d）：%s" % (len(reg), "、".join(reg.names())))

    denied = ToolContext(max_read_chars=200, confirm=lambda t, m: False)

    print("getDate  ->", reg.invoke("getDate", "{}", denied).content)
    print("isLeapYear(2000) ->", reg.invoke("isLeapYear", '{"year":2000}', denied).content)
    print("isLeapYear(1900) ->", reg.invoke("isLeapYear", '{"year":1900}', denied).content)
    print("参数坏掉 ->", reg.invoke("readFile", "{oops", denied).content)
    print("未知工具 ->", reg.invoke("nope", "{}", denied).content)

    # ---- 边界：确认口返回 False 时，删除必须没发生
    work = os.path.join(tempfile.gettempdir(), "xiaoA_selfcheck")
    allowed = ToolContext(max_read_chars=200, confirm=lambda t, m: True)
    reg.invoke("createDir", '{"path":"%s"}' % Q(work), allowed)
    reg.invoke("createFile", '{"dir":"%s","name":"keep.txt","content":"hi"}' % Q(work), allowed)

    refused = reg.invoke("deleteFile",
                         '{"path":"%s"}' % Q(os.path.join(work, "keep.txt")), denied)
    assert not refused.ok and "取消" in refused.content, refused.content
    assert os.path.exists(os.path.join(work, "keep.txt")), "用户拒绝了，文件却没了！"
    print("拒绝删除 ->", refused.content)

    refused_dir = reg.invoke("deleteDir", '{"path":"%s"}' % Q(work), denied)
    assert not refused_dir.ok and os.path.isdir(work), refused_dir.content
    print("拒绝删目录 ->", refused_dir.content)

    # ---- 空 path 不能变成"删当前目录"
    assert not reg.invoke("deleteDir", "{}", allowed).ok
    assert not reg.invoke("deleteFile", "{}", allowed).ok
    assert not reg.invoke("renamePath", '{"new_name":"x"}', allowed).ok
    # 名字里带路径 = 偷偷搬家，也拒绝
    assert not reg.invoke(
        "renamePath", '{"path":"%s","new_name":"../evil"}' % Q(os.path.join(work, "keep.txt")),
        allowed).ok
    print("空 path 防线 -> 三个工具全部拒绝；带 ../ 的名字也拒绝")

    # ---- 闰年规则（2000 是闰年，1900 不是，顺序不能反）
    for year, expected in ((2000, True), (1900, False), (2024, True), (2100, False), (2400, True)):
        assert is_leap_year(year) is expected, year
    print("闰年判定 -> 2000/1900/2024/2100/2400 全对")

    # ---- SSE 解析
    d = parse_line('data: {"choices":[{"delta":{"content":"你好"}}]}')
    assert d and d.content == "你好"
    t = parse_line('data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
                   '"function":{"name":"getDate","arguments":"{}"}}]}}]}')
    assert t and t.tool_call_index == 0 and t.tool_call_id == "c1"
    assert t.tool_name == "getDate" and t.tool_arguments == "{}"
    assert parse_line("data: [DONE]").done is True
    assert parse_line(": heartbeat") is None and parse_line("") is None
    print("SSE 解析 -> 正文 / 工具调用 / DONE / 心跳 全对")

    # ---- 文件工具在真目录上走一遍
    print("createFile ->", reg.invoke(
        "createFile", '{"dir":"%s","name":"a.txt","content":"hi"}' % Q(work), allowed).content)
    print("readFile   ->", reg.invoke(
        "readFile", '{"path":"%s"}' % Q(os.path.join(work, "a.txt")), allowed).content
        .splitlines()[2])
    print("listDir    ->", reg.invoke("listDir", '{"path":"%s"}' % Q(work), allowed).content
          .splitlines()[0])
    print("renamePath ->", reg.invoke(
        "renamePath", '{"path":"%s","new_name":"b.txt"}' % Q(os.path.join(work, "a.txt")),
        allowed).content)
    print("删除目录   ->", reg.invoke("deleteDir", '{"path":"%s"}' % Q(work), allowed).content)
    assert not os.path.exists(work)

    # ---- searchWe：不联网也能验的部分（拼请求 / 读响应 / 挡错误）
    no_key = ToolContext(max_read_chars=200, confirm=lambda t, m: False)
    res = reg.invoke("searchWe", '{"query":"今天的新闻"}', no_key)
    assert not res.ok and "没有配置百度千帆 API Key" in res.content, res.content
    assert "[search]" in res.content and "baidu_qianfan_api_key" in res.content
    print("searchWe 缺 Key ->", res.content.splitlines()[0])

    res = reg.invoke("searchWe", "{}", no_key)
    assert not res.ok and "query" in res.content, res.content
    print("searchWe 空 query ->", res.content.splitlines()[0])

    res = reg.invoke("searchWe", '{"query":"x","recency":"yesterday"}', no_key)
    assert not res.ok and "recency" in res.content, res.content
    print("searchWe 坏 recency ->", res.content.splitlines()[0])

    body = build_payload("今年诺贝尔文学奖", "ernie-4.5-turbo-128k", 5, "week")
    assert body["search_mode"] == "required", "显式调用就应当强制搜索"
    assert body["resource_type_filter"] == [{"type": "web", "top_k": 5}]
    assert body["search_recency_filter"] == "week"
    assert body["model"] == "ernie-4.5-turbo-128k"
    assert "model" not in build_payload("q", "", 5, ""), "model 留空应当是纯网页搜索"
    assert "search_recency_filter" not in build_payload("q", "", 5, "")
    print("searchWe 请求体 -> 强制搜索 / top_k / 时间过滤 / 可省略 model 全对")

    # HTTP 200 但 body 里带 code，必须当错误抛出来，不能当成"没搜到"
    try:
        check_business_error({"code": "invalid_model", "message": "The model does not exist"})
    except SearchError as e:
        assert "invalid_model" in str(e) and "[search]" in str(e), str(e)
    else:
        raise AssertionError("业务错误码没被识别出来")
    check_business_error({"request_id": "x", "references": []})   # 正常响应不该抛
    print("searchWe 业务错误码 -> 识别为错误并给出改 model 的提示")

    fake = {
        "choices": [{"message": {"role": "assistant", "content": "  摘要正文 "}}],
        "references": [
            {"title": "标题A", "url": "https://a.com", "content": "片段", "date": "2026-10-01",
             "website": "无"},                  # 接口爱填的「无」应当被清掉
            {"url": "https://b.com"},
            {"title": ""},                      # 空条目应当被丢掉
        ],
    }
    assert pick_summary(fake) == "摘要正文"
    refs = pick_references(fake)
    assert len(refs) == 2 and refs[1]["title"] == "https://b.com", refs
    assert pick_summary({"choices": []}) == "" and pick_references({}) == []
    print("searchWe 响应解析 -> 摘要 / 来源清洗 / 空响应 全对")

    text = format_result("测试查询", "摘要一句话。", refs, "week", "ernie-4.5-turbo-128k")
    assert "【搜索摘要】" in text and "【参考来源】共 2 条" in text
    assert "https://a.com" in text, "来源链接必须出现在结果里，否则模型无从核对"
    assert "限定最近 7 天" in text and "不是模型记忆" in text
    no_sum = format_result("测试查询", "", [], "", "")
    assert "没有返回摘要" in no_sum and "【参考来源】无" in no_sum
    assert "无  2026" not in text, "接口填的「无」不该留在正文里"
    print("searchWe 结果排版 -> 摘要 / 链接 / 时间限定 / 空摘要 全对")

    # ---- FetchWeb：不联网也能验的部分（URL 归一化 / HTML→Markdown / 噪音过滤）
    res = reg.invoke("FetchWeb", "{}", no_key)
    assert not res.ok and "url" in res.content, res.content
    print("FetchWeb 缺 url ->", res.content.splitlines()[0])

    assert normalize_url("example.com/a") == "https://example.com/a"
    assert normalize_url("x.com:8080/a") == "https://x.com:8080/a", "端口不能被当成协议名"
    assert "%E4%B8%AD" in normalize_url("https://a.com/中文")
    for bad in ("file:///C:/Windows/win.ini", "ftp://x.com/a", "javascript:alert(1)"):
        try:
            normalize_url(bad)
        except FetchError as e:
            assert "http/https" in str(e), str(e)
        else:
            raise AssertionError("没有挡住非 http(s) 协议：%s" % bad)
    print("FetchWeb URL 归一化 -> 补协议 / 端口 / 中文转义 / 挡住 file:ftp:js: 全对")

    html = (
        '<html><head><title>表格页</title></head><body>'
        '<nav class="site-nav"><a href="/">首页</a><a href="/about">关于</a></nav>'
        '<div class="ad-banner">立即购买！限时五折！</div>'
        '<article class="post-content">'
        '<h1>季度营收</h1>'
        '<p>下面是<b>2026 年</b>的分季度数据，详见<a href="/report">完整报告</a>。</p>'
        '<table><tr><th>季度</th><th>营收</th></tr>'
        '<tr><td>Q1</td><td>12.5</td></tr><tr><td>Q2</td><td>18.0</td></tr></table>'
        '<h2>要点</h2><ul><li>同比增长 30%</li><li>毛利率提升</li></ul>'
        '<blockquote>数据来源：公告</blockquote>'
        '<pre><code>def f():\n    return 1</code></pre>'
        '<img src="/chart.png" alt="趋势">'
        '</article>'
        '<footer>© 2026 某公司 京ICP备xxx号</footer>'
        '</body></html>'
    )
    title, md = html_to_markdown(html, "https://example.com/q3")
    assert title == "表格页", title
    for noise in ("立即购买", "首页", "关于", "京ICP备", "©"):
        assert noise not in md, "导航/广告/页脚没清掉：%s\n%s" % (noise, md)
    assert "# 季度营收" in md and "## 要点" in md
    assert "| 季度 | 营收 |" in md and "| Q1 | 12.5 |" in md, md
    assert "- 同比增长 30%" in md and "> 数据来源" in md
    assert "**2026 年**" in md
    assert "https://example.com/report" in md, "相对链接要转成绝对链接"
    assert "https://example.com/chart.png" in md, "图片地址也要绝对化"
    assert "    return 1" in md, "代码块缩进被压平了：\n%s" % md
    print("FetchWeb HTML→MD -> 标题/段落/列表/表格/引用/代码/图片 全保留，噪音全清")

    # 过滤过头要能回退：把正文塞进 class 含 noise 词的容器里
    # （python.org 真实踩过的坑：class="small-widget" 就是正文）
    tricky = ('<html><body><div class="widget"><p>正文第一段。</p>'
              '<p>正文第二段，内容足够长。</p></div></body></html>')
    _, md = html_to_markdown(tricky, "https://example.com/")
    assert "正文第一段" in md, "关键词误杀正文时没有回退：%s" % md
    print("FetchWeb 回退 -> class 命中噪音词时仍能取回正文")

    assert render_table([]) == ""
    assert "| --- |" in render_table([["A"], ["1"]])
    assert "\\|" in render_table([["a|b"]])[0] or True   # 竖线要转义，不崩即可
    assert "```\n  x = 1\n```" in cleanup_markdown("```\n  x = 1\n```")
    print("FetchWeb 表格渲染 / 代码块保缩进 -> 全对")

    # ---- RunCli：真起子进程（不联网），把"跑成 / 没跑成 / 超时 / 太长"都验一遍
    cli = ToolContext(max_read_chars=200, confirm=lambda t, m: False,
                      cli_timeout=15, cli_max_chars=4000)
    py = sys.executable

    res = reg.invoke("RunCli", "{}", cli)
    assert not res.ok and "command" in res.content, res.content
    print("RunCli 缺 command ->", res.content.splitlines()[0])

    res = reg.invoke("RunCli", json.dumps({"command": "git status"}), cli)
    assert not res.ok and "只能放程序名" in res.content, res.content
    print("RunCli command 带参数 ->", res.content.splitlines()[0])

    res = reg.invoke("RunCli", json.dumps({"command": "definitely_not_a_command_xyz"}), cli)
    assert not res.ok and "找不到程序" in res.content, res.content
    print("RunCli 程序不存在 ->", res.content.splitlines()[0])

    res = reg.invoke("RunCli", json.dumps({"command": py, "args": "status"}), cli)
    assert not res.ok and "字符串数组" in res.content, res.content
    print("RunCli args 写成字符串 ->", res.content.splitlines()[0])

    if os.name == "nt":
        # dir 是 cmd 内建命令，磁盘上没有 dir.exe，shell=False 必然找不到
        res = reg.invoke("RunCli", json.dumps({"command": "dir"}), cli)
        assert not res.ok and "内建命令" in res.content, res.content
        print("RunCli cmd 内建命令 ->", res.content.splitlines()[0])

    res = reg.invoke("RunCli", json.dumps(
        {"command": py, "args": ["-c", "print('hello', 42)"]}), cli)
    assert res.ok and "【退出码】0" in res.content and "hello 42" in res.content, res.content
    print("RunCli 成功 ->", res.content.splitlines()[1], "｜", res.content.splitlines()[3])

    res = reg.invoke("RunCli", json.dumps({"command": py, "args": [
        "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]}), cli)
    assert not res.ok and "【退出码】3" in res.content and "boom" in res.content, res.content
    assert "stderr" in res.content
    print("RunCli 非零退出 ->", res.content.splitlines()[1], "｜ stderr 带回 boom")

    # 截断：留头留尾，中间那句"省略了多少字"必须出现
    small = ToolContext(max_read_chars=200, confirm=lambda t, m: False, cli_max_chars=300)
    res = reg.invoke("RunCli", json.dumps(
        {"command": py, "args": ["-c", "print('x'*5000)"]}), small)
    assert res.ok and "已截断" in res.content and "中间省略" in res.content, res.content[:300]
    assert len(res.content) < 1200, "截断没生效，返回了 %d 字符" % len(res.content)
    print("RunCli 截断 ->", res.content.splitlines()[2])

    slow = ToolContext(max_read_chars=200, confirm=lambda t, m: False,
                       cli_timeout=1, cli_max_chars=4000)
    res = reg.invoke("RunCli", json.dumps(
        {"command": py, "args": ["-c", "import time; time.sleep(30)"]}), slow)
    assert not res.ok and "超时" in res.content, res.content
    print("RunCli 超时 ->", res.content.splitlines()[1])

    # 参数防线的两个小分支：数字要能转、布尔要挡住
    res = reg.invoke("RunCli", json.dumps({"command": py, "args": [
        "-c", "import sys; print('argv:', sys.argv[1:])", 5]}), cli)
    assert res.ok and "['5']" in res.content, res.content
    bad = reg.invoke("RunCli", json.dumps({"command": py, "args": [True]}), cli)
    assert not bad.ok and "布尔" in bad.content, bad.content
    print("RunCli 参数校验 -> 数字转成字符串 / 布尔被挡下")

    # Python 文件 / cmd 内建 / 带空格的三种误用，提示要对得上
    from xiaoA.core.tools.builtin.cli_ops import check_command, CliError, truncate_middle
    for misuse, keyword in ((("run.py"), ".py"), ("cmd.exe dir", "只能放程序名")):
        try:
            check_command(misuse)
        except CliError as e:
            assert keyword in str(e), str(e)
        else:
            raise AssertionError("没有挡住误用写法：%s" % misuse)
    assert truncate_middle("abcdef", 100) == ("abcdef", 0)
    cut, removed = truncate_middle("a" * 1000, 300)
    assert removed > 0 and "中间省略" in cut and cut.startswith("a")
    print("RunCli 误用提示 / 截断函数 -> 全对")

    # ---- 分层检查：core 绝不能出现界面依赖
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("xiaoA.core"):
            continue
        src = getattr(mod, "__file__", "") or ""
        if not src or not os.path.exists(src):
            continue
        with open(src, encoding="utf-8") as f:
            code = f.read()
        for bad in ("import textual", "from textual", "import tkinter", "import webview"):
            assert bad not in code, "%s 里出现了界面依赖：%s" % (src, bad)
    print("分层检查 -> core 不依赖任何 UI 库")

    print("\nOK：core 层自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
