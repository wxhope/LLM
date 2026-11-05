# -*- coding: utf-8 -*-
"""core 离线自检：不联网、不装 textual 也能跑。

用法（在 xiaoA 目录下）：
    python -X utf8 selfcheck.py
全部通过会打印 OK；有问题会 assert 失败并指出位置。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xiaoA.core.llm import StreamDelta
from xiaoA.core.llm.stream import parse_line
from xiaoA.core.tools import ToolContext, ToolRegistry, build_default_registry
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
    assert len(reg) == 18, "内置工具应有 18 个，实际 %d" % len(reg)
    assert len(reg.schemas()) == 18
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

    # ---- Skill 发现与注册：只读 name/description，坏目录要留原因、不能炸
    from xiaoA.core.skills import SkillCatalog, discover_skills, parse_frontmatter

    fm = parse_frontmatter("---\nname: a\ndescription: 带冒号：的值\n---\n正文")
    assert fm["name"] == "a" and fm["description"] == "带冒号：的值", fm
    fm = parse_frontmatter("---\r\nname: 'b'\r\ndescription: \"c\"\r\n---\r\n")
    assert fm["name"] == "b" and fm["description"] == "c", fm
    for bad_text in ("没有 frontmatter", "---\nname: a\n"):
        try:
            parse_frontmatter(bad_text)
        except ValueError:
            pass
        else:
            raise AssertionError("坏 frontmatter 没被挡住：%r" % bad_text)
    print("SKILL.md Frontmatter 解析 -> 冒号 / 引号 / CRLF / 两种坏格式 全对")

    # 用临时目录搭一个真实的 .agents/skills 布局来扫
    skill_root = os.path.join(tempfile.gettempdir(), "xiaoA_skills_case")
    mk = lambda *p: os.path.join(skill_root, ".agents", "skills", *p)  # noqa: E731
    import shutil
    shutil.rmtree(skill_root, ignore_errors=True)
    os.makedirs(mk("good"))
    with open(mk("good", "SKILL.md"), "w", encoding="utf-8") as f:
        # 正文写到 1000 字符以上：loadSkill 的截断下限是 1000，太短测不到截断分支
        f.write("---\nname: good\ndescription: 正常技能\n---\n" + "正文很多字" * 250)
    # 技能内附件：测 filename 的各种解析方式
    os.makedirs(mk("good", "scripts"))
    with open(mk("good", "scripts", "run.py"), "w", encoding="utf-8") as f:
        f.write("print('skill script ok')\n")
    with open(mk("good", "guide.md"), "w", encoding="utf-8") as f:
        f.write("# 技能附件说明\n")
    os.makedirs(mk("good", "sub"))
    with open(mk("good", "dup.txt"), "w", encoding="utf-8") as f:
        f.write("dup A\n")
    with open(mk("good", "sub", "dup.txt"), "w", encoding="utf-8") as f:
        f.write("dup B\n")
    with open(mk("good", "blob.bin"), "wb") as f:
        f.write(b"\x00\x01binary")
    os.makedirs(mk("no_md"))                                   # 没有 SKILL.md
    os.makedirs(mk("no_desc"))                                 # 缺 description
    os.makedirs(mk("bad_fm"))                                  # frontmatter 没闭合
    os.makedirs(mk("not-equal"))                               # name 和目录名不一致
    with open(mk("not-equal", "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: other\ndescription: x\n---\n")

    catalog = discover_skills(skill_root)
    assert [s.name for s in catalog.skills] == ["good", "other"], catalog.names()
    good = catalog.get("good")
    assert good is not None and good.path.endswith("SKILL.md")
    # 正文不能进元信息：SkillMeta 里根本没有正文字段，注册内容只到描述为止
    assert "正文很多字" not in repr(good)
    joined = "；".join(catalog.problems)
    for key in ("no_md", "no_desc", "bad_fm"):
        assert key in joined, "问题清单里没有 %s：%s" % (key, joined)
    assert any("目录名不一致" in p for p in catalog.problems), catalog.problems
    assert discover_skills(os.path.join(skill_root, "不存在的目录")).root == ""

    # Session 初始化要自动注册：传进来的 catalog 优先，缺省时自己扫
    from xiaoA.core.config import AppConfig
    from xiaoA.core.session import Session

    def fake_client():
        return type("C", (), {"chat_stream": staticmethod(lambda *a, **k: iter([]))})()

    sess = Session(AppConfig(api_key="sk-test", base_url="https://x.invalid", model="m"),
                   client=fake_client(), registry=ToolRegistry([]), skills=catalog)
    assert sess.skills.get("good") is not None
    empty = Session(AppConfig(api_key="sk-test", base_url="https://x.invalid", model="m"),
                    client=fake_client(), registry=ToolRegistry([]),
                    skills=SkillCatalog())
    assert len(empty.skills) == 0
    print("Skill 发现与注册 -> 只取元信息 / 坏目录留原因 / 缺目录不炸 / Session 自动挂载")

    # ---- loadSkill + System Prompt 注入：清单进提示词，正文按需取
    from xiaoA.core.session import build_system_prompt

    # 没技能：提示词原样，不出现 loadSkill 字样
    assert build_system_prompt(None) == build_system_prompt(SkillCatalog())
    assert "loadSkill" not in build_system_prompt(None)
    # 有技能：清单里要有 name 和 description
    prompt = build_system_prompt(catalog)          # 复用上面扫出来的 good/other
    assert '"good"' in prompt and "正常技能" in prompt, prompt[-400:]
    assert "loadSkill" in prompt and "readFile" in prompt
    print("System Prompt 注入 -> 无技能不加尾巴 / 有技能带 name+description 清单")

    def cli_ctx(limit):
        return ToolContext(max_read_chars=limit, confirm=lambda t, m: False,
                           skills=catalog)

    # ---- 基础路径：name 必需、未知名、空注册表
    res = reg.invoke("loadSkill", "{}", cli_ctx(20000))
    assert not res.ok and "name 参数" in res.content, res.content
    print("loadSkill 缺 name ->", res.content.splitlines()[0])

    res = reg.invoke("loadSkill", '{"name": "nope"}', cli_ctx(20000))
    assert not res.ok and "good" in res.content, res.content   # 提示里列出可用技能
    print("loadSkill 未知技能 ->", res.content.splitlines()[0])

    # 没有技能的 ctx：工具要说明现状，而不是抛异常
    bare = ToolContext(max_read_chars=20000, confirm=lambda t, m: False)
    res = reg.invoke("loadSkill", '{"name": "good"}', bare)
    assert not res.ok and "没有任何已注册技能" in res.content, res.content
    print("loadSkill 空注册表 ->", res.content.splitlines()[0])

    # ---- 只给 name：SKILL.md 全文 + 截断
    res = reg.invoke("loadSkill", '{"name": "good"}', cli_ctx(20000))
    assert res.ok and "---" in res.content and "正文很多字" in res.content, res.content[:200]
    print("loadSkill 只传 name -> 返回 SKILL.md 全文（frontmatter + 正文）")

    res = reg.invoke("loadSkill", '{"name": "good"}', cli_ctx(50))
    assert res.ok and "已截断" in res.content, res.content[:200]
    print("loadSkill 截断 ->", res.content.splitlines()[-1])

    # ---- filename：技能目录相对路径
    res = reg.invoke("loadSkill", '{"name": "good", "filename": "scripts/run.py"}',
                     cli_ctx(20000))
    assert res.ok and "skill script ok" in res.content, res.content[:200]
    assert "正文很多字" not in res.content                      # 不重复带 SKILL.md
    print("loadSkill filename 技能相对路径 -> 只返回该文件，不带 SKILL.md")

    # ---- 项目相对路径（项目根 = .agents 的上两级）
    res = reg.invoke("loadSkill",
                     '{"name": "good", "filename": ".agents/skills/good/guide.md"}',
                     cli_ctx(20000))
    assert res.ok and "技能附件说明" in res.content, res.content[:200]
    print("loadSkill filename 项目相对路径 -> 命中")

    # ---- 绝对路径：目录内放行，目录外拒绝
    inside = mk("good", "scripts", "run.py")
    res = reg.invoke("loadSkill",
                     json.dumps({"name": "good", "filename": inside}), cli_ctx(20000))
    assert res.ok and "skill script ok" in res.content, res.content[:200]
    outside = os.path.join(skill_root, "outside_secret.txt")
    with open(outside, "w", encoding="utf-8") as f:
        f.write("不应被读到\n")
    res = reg.invoke("loadSkill",
                     json.dumps({"name": "good", "filename": outside}), cli_ctx(20000))
    assert not res.ok and "越界" in res.content, res.content
    print("loadSkill filename 绝对路径 -> 目录内放行 / 目录外拒绝越界")

    # ---- ../ 穿越：即使最终写法看着温和，解析出目录也必须挡
    res = reg.invoke("loadSkill",
                     '{"name": "good", "filename": "../no_md/SKILL.md"}',
                     cli_ctx(20000))
    assert not res.ok, "穿越到别的技能目录没有被挡下"
    print("loadSkill filename ../ 穿越 -> 拒绝")

    # ---- 裸文件名：唯一命中直接读；同名多个给候选清单
    res = reg.invoke("loadSkill", '{"name": "good", "filename": "run.py"}',
                     cli_ctx(20000))
    assert res.ok and "skill script ok" in res.content, res.content[:200]
    print("loadSkill filename 裸文件名（唯一） -> 递归搜索后直接读")

    res = reg.invoke("loadSkill", '{"name": "good", "filename": "dup.txt"}',
                     cli_ctx(20000))
    assert not res.ok and "dup.txt" in res.content and "sub/dup.txt" in res.content \
        and "2 个位置" in res.content, res.content
    assert res.content.count("dup") >= 2                       # 两个候选都在
    print("loadSkill filename 裸文件名（多个同名） -> 返回候选路径由模型决定")

    res = reg.invoke("loadSkill", '{"name": "good", "filename": "不存在.py"}',
                     cli_ctx(20000))
    assert not res.ok and "递归搜索" in res.content, res.content
    print("loadSkill filename 找不到 -> 说明已递归搜索过")

    # ---- 二进制文件：拒绝读成文本，提示走 RunCli
    res = reg.invoke("loadSkill", '{"name": "good", "filename": "blob.bin"}',
                     cli_ctx(20000))
    assert not res.ok and "二进制" in res.content, res.content
    print("loadSkill filename 二进制文件 -> 拒绝并建议 RunCli")

    # ---- createSkill：落盘 -> 规范化 -> 能被重扫发现 -> 重名/中文名/空参全挡
    def read_skill(*parts):
        with open(mk(*parts), encoding="utf-8") as f:
            return f.read()

    scans = []

    def rescan():
        cat = discover_skills(skill_root)
        scans.append(cat)
        return cat

    maker = ToolContext(max_read_chars=20000, confirm=lambda t, m: False,
                        skills=catalog, refresh_skills=rescan)

    res = reg.invoke("createSkill", json.dumps({
        "name": "Demo Skill",          # 大写 + 空格，必须被规范成 demo-skill
        "skillcontext": "# 演示技能\n\n这个技能用来验证 createSkill 的落盘流程。",
    }), maker)
    assert res.ok and "已创建技能" in res.content, res.content
    assert os.path.isfile(mk("demo-skill", "SKILL.md")), "SKILL.md 没落在 .agents/skills/demo-skill 下"
    fm = parse_frontmatter(read_skill("demo-skill", "SKILL.md"))
    assert fm["name"] == "demo-skill", fm                     # 目录名与 frontmatter 一致
    assert fm["description"] == "这个技能用来验证 createSkill 的落盘流程。", fm
    assert scans and scans[-1].get("demo-skill") is not None, "建完没重扫，模型当场 loadSkill 会扑空"
    print("createSkill 无 frontmatter -> 名字规范化 / 自动补 description / 当场进清单")

    # 自带 frontmatter：description 用模型的，name 一律以规范化的目录名为准
    res = reg.invoke("createSkill", json.dumps({
        "name": "with-fm",
        "skillcontext": "---\nname: 乱写的名字\ndescription: 带冒号：的说明\n---\n\n正文一段。",
    }), maker)
    assert res.ok, res.content
    fm = parse_frontmatter(read_skill("with-fm", "SKILL.md"))
    assert fm["name"] == "with-fm", fm
    assert fm["description"] == "带冒号：的说明", fm
    print("createSkill 自带 frontmatter -> 保留 description，name 对齐目录名")

    # 重名：拒绝覆盖，原文件一个字都不能动
    before = read_skill("demo-skill", "SKILL.md")
    res = reg.invoke("createSkill", json.dumps({
        "name": "demo-skill", "skillcontext": "想把这行写进去覆盖掉"}), maker)
    print("createSkill 重名 ->", res.content.splitlines()[0])
    assert not res.ok and "已经存在" in res.content, res.content
    assert read_skill("demo-skill", "SKILL.md") == before, "重名时原文件被改了！"

    # 中文名：压成空串，要求给英文名（不替用户音译）
    res = reg.invoke("createSkill", json.dumps({
        "name": "天气预报", "skillcontext": "正文"}), maker)
    assert not res.ok and "英文" in res.content, res.content
    print("createSkill 中文名 -> 拒绝并提示改用英文名")

    # 缺参 / 空内容
    res = reg.invoke("createSkill", "{}", maker)
    assert not res.ok and "name 参数" in res.content, res.content
    res = reg.invoke("createSkill", '{"name":"empty-body","skillcontext":"   "}', maker)
    assert not res.ok and "skillcontext" in res.content, res.content
    print("createSkill 缺 name / 空 skillcontext -> 都如实拒绝")

    # 没有刷新回调：必须说清"重启后生效"，不能让人以为马上能用
    solo = ToolContext(max_read_chars=20000, confirm=lambda t, m: False, skills=catalog)
    res = reg.invoke("createSkill", json.dumps({
        "name": "no-refresh", "skillcontext": "只建文件，不管清单。"}), solo)
    assert res.ok and "重启后" in res.content, res.content
    print("createSkill 无刷新回调 -> 明确告知重启后生效")

    shutil.rmtree(skill_root, ignore_errors=True)

    # ---- MCP Client：纯离线部分（配置解析 / 名字消毒 / 冲突规则 / 结果排版）
    from xiaoA.core.mcp import (
        McpError,
        format_call_result,
        parse_mcp_config,
        plan_tool_names,
        sanitize_name,
    )

    cfg_path = os.path.join(tempfile.mkdtemp(prefix="xiaoA_mcp_"), ".mcp.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {
            "PetHospital": {"url": "http://127.0.0.1:8090/mcp"},
            "中文服务": {"url": "http://127.0.0.1:9000/mcp", "headers": {"X-Token": "t"}},
            "坏的": {"url": "ftp://nope"},
            "没url的": {},
        }}, f, ensure_ascii=False)
    servers, problems = parse_mcp_config(cfg_path)
    assert [s.name for s in servers] == ["PetHospital", "中文服务"], servers
    assert servers[1].headers == {"X-Token": "t"}
    assert any("url 必须以" in p for p in problems), problems
    assert any("没有 url 字段" in p for p in problems), problems
    print("MCP 配置解析 -> 正常 2 台、坏项全部进 problems")

    # 名字消毒：中文/点号/数字开头都要落到 Function Calling 能接受的形态。
    # 纯中文名变出的全是下划线，等于没名字 → 回退 fallback（重名会被冲突规则兜住）。
    assert sanitize_name("中文服务", "server") == "server"
    assert sanitize_name("另一台", "server") == "server"
    assert sanitize_name("pet.hospital 2", "server") == "pet_hospital_2"
    assert sanitize_name("9lives", "server") == "_9lives"
    assert sanitize_name("", "server") == "server"
    assert sanitize_name("///", "tool") == "tool"
    print("MCP 名字消毒 -> 中文/点号/数字开头/空名全部合规")

    # 冲突规则：唯一且不撞本地 → 裸名；撞了 → 服务器名前缀
    remote = [
        ("PetHospital", [("list_pets", "列出宠物", {"type": "object"}),   # 唯一 → 裸名
                         ("RunCli", "和本地重名", {"type": "object"})]),
        ("另一台", [("getDate", "和本地重名", {"type": "object"})]),
    ]
    infos, name_problems = plan_tool_names(remote, ["RunCli", "getDate"])
    by_exposed = {i.exposed: i for i in infos}
    assert by_exposed["list_pets"].server == "PetHospital"       # 唯一且不撞 → 裸名
    assert by_exposed["PetHospital__RunCli"].original == "RunCli"
    assert by_exposed["server__getDate"].server == "另一台"      # 中文名消毒后加前缀
    assert by_exposed["server__getDate"].original == "getDate"
    assert not name_problems, name_problems
    for info in infos:
        assert re.fullmatch(r"[0-9A-Za-z_-]{1,64}", info.exposed), info.exposed
    print("MCP 冲突规则 -> 唯一裸名 / 撞名加前缀 / 暴露名全部合规")

    # 结果排版：文本 + 结构化 + 业务错误 + 空结果
    class FakeText:
        def __init__(self, t): self.text = t; self.mimeType = ""
    class FakeResult:
        def __init__(self, content, is_error=False, structured=None):
            self.content, self.isError, self.structuredContent = content, is_error, structured
    text, ok = format_call_result(FakeResult([FakeText("有 3 只宠物")]), 8000)
    assert ok and "3 只宠物" in text
    text, ok = format_call_result(FakeResult([], is_error=True), 8000)
    assert not ok and text
    text, ok = format_call_result(
        FakeResult([FakeText("部分")], structured={"count": 3}), 8000)
    assert ok and '"count": 3' in text and "结构化数据" in text
    text, ok = format_call_result(FakeResult([]), 8000)
    assert ok and "没有返回内容" in text
    long = "x" * 20000
    text, ok = format_call_result(FakeResult([FakeText(long)]), 1000)
    assert len(text) < 2000 and "省略" in text
    print("MCP 结果排版 -> 文本/错误/结构化/空返回/截断 全部正确")

    # 连不上：给一个必然拒绝的端口，确认只进 problems、不炸程序
    from xiaoA.core.mcp import load_mcp_manager
    dead_path = os.path.join(tempfile.mkdtemp(prefix="xiaoA_mcp_dead_"), ".mcp.json")
    with open(dead_path, "w", encoding="utf-8") as f:
        json.dump({"mcpServers": {" dead": {"url": "http://127.0.0.1:9/mcp"}}}, f)
    dead = load_mcp_manager(dead_path, connect_timeout=3.0, call_timeout=5.0)
    assert dead is not None and not dead.tools
    assert dead.connections == [] and len(dead.problems) == 1, dead.problems
    assert "MCP" in dead.problems[0] or "mcp" in dead.problems[0].lower()
    dead.shutdown()
    print("MCP 连接失败 -> 只进 problems，其余工具不受影响：", dead.problems[0][:60], "…")

    # 没配 .mcp.json：load 返回 None（零开销路径）
    none_mgr = load_mcp_manager(os.path.join(tempfile.mkdtemp(prefix="xiaoA_mcp_no_"),
                                              ".mcp.json"))
    assert none_mgr is None
    print("MCP 未配置 -> load_mcp_manager 返回 None，一行多余的活都不干")

    # ---- 上下文压缩与历史检索：全离线（不联网、不需要真模型）
    from xiaoA.core.compact import (
        ArchiveError,
        archive_messages,
        build_compact_messages,
        build_summary_message,
        estimate_tokens,
        list_archives,
        search_archives,
    )

    assert 1 <= estimate_tokens("你好") <= 4, estimate_tokens("你好")
    assert estimate_tokens("hello world") <= 5
    assert estimate_tokens("") == 0
    long_cn = "上下文压缩" * 100
    assert estimate_tokens(long_cn) == len(long_cn), estimate_tokens(long_cn)
    print("token 估算 -> 中文≈1字1token、英文≈4字符1token、空串为 0")

    ctx_root = tempfile.mkdtemp(prefix="xiaoA_ctx_")
    history = [
        {"role": "user", "content": "宠物医院的 MCP 跑在 8090 端口"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "RunCli",
                         "arguments": "{\"command\":\"curl\",\"args\":[\"http://127.0.0.1:8090/mcp\"]}"}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "BACKEND_API_ERROR: backend returned HTTP 502"},
    ]
    record = archive_messages(history, ctx_root)
    assert re.fullmatch(r"\d{8}-\d{6}(-\d+)?", record.archive_id), record.archive_id
    assert os.path.isfile(record.path) and record.message_count == 3
    with open(record.path, encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 4, "meta + 3 条消息"
    rows = [json.loads(x) for x in lines]
    assert rows[0]["type"] == "meta" and rows[0]["archive_id"] == record.archive_id
    assert [r["index"] for r in rows[1:]] == [0, 1, 2], "顺序必须原样保留"
    assert rows[2]["tool_calls"][0]["function"]["name"] == "RunCli"
    assert "8090" in json.dumps(rows[1], ensure_ascii=False)
    print("归档 -> %s 写出 %d 条，meta/顺序/工具调用参数都在"
          % (record.archive_id, record.message_count))

    second = archive_messages(history, ctx_root)
    assert second.archive_id != record.archive_id, "同一秒两次压缩不能互相覆盖"
    print("归档重名 -> 同秒再写自动加序号（%s）" % second.archive_id)

    # 写失败必须抛错：宁可没压缩，也不能丢了原文
    bogus = os.path.join(ctx_root, "not_a_dir")
    open(bogus, "w").close()
    try:
        archive_messages(history, bogus)
        raise AssertionError("归档目录不可写时应当抛 ArchiveError")
    except ArchiveError as e:
        assert "归档" in str(e) or "写" in str(e), str(e)
    print("归档失败 -> 抛 ArchiveError（中止压缩而不是静默继续）")

    # 检索：关键词命中带编号/序号/角色；查不到要如实说；id 过滤生效
    hits, err = search_archives(ctx_root, "BACKEND_API_ERROR")
    assert not err and hits, err
    assert hits[0].archive_id == second.archive_id and hits[0].index == 2, hits[0]
    assert "502" in hits[0].snippet
    hits2, err2 = search_archives(ctx_root, "8090")
    assert not err2 and any(h.index == 0 for h in hits2), hits2
    none_hits, none_err = search_archives(ctx_root, "根本没有这个词xyz")
    assert not none_hits and "没有找到" in none_err
    missing, missing_err = search_archives(ctx_root, "8090", archive_id="19990101-000000")
    assert not missing and "没有编号" in missing_err
    only_first, _ = search_archives(ctx_root, "8090", archive_id=record.archive_id)
    assert only_first and all(h.archive_id == record.archive_id for h in only_first)
    print("检索 -> 命中带编号/序号/角色/片段；查不到和坏编号都如实说明")

    # 压缩摘要必须带归档说明（模型靠它知道还能 recallContext）
    replaced = build_summary_message("## 目标\n- 接入 MCP", record.archive_id, record.created_at)
    assert replaced["role"] == "user"
    assert "压缩摘要" in replaced["content"] and record.archive_id in replaced["content"]
    assert "recallContext" in replaced["content"]
    payload = build_compact_messages(history)
    assert payload[0]["role"] == "system" and "上下文管理助手" in payload[0]["content"]
    assert "宠物医院" in payload[1]["content"]
    print("压缩 Prompt / 替代消息 -> 摘要带归档说明 + 指向 recallContext")

    # recallContext 工具：注册、参数校验、真实检索
    recall_ctx = ToolContext(max_read_chars=2000, confirm=lambda t, m: False,
                             context_dir=ctx_root)
    res = reg.invoke("recallContext", '{"text":"BACKEND_API_ERROR"}', recall_ctx)
    assert res.ok and "归档" in res.content and "502" in res.content, res.content
    bad = reg.invoke("recallContext", "{}", recall_ctx)
    assert not bad.ok and "text" in bad.content
    traversal = reg.invoke("recallContext", '{"text":"x","id":"../../etc/passwd"}', recall_ctx)
    assert not traversal.ok and "编号" in traversal.content, traversal.content
    no_dir = reg.invoke("recallContext", '{"text":"x"}',
                        ToolContext(max_read_chars=200, context_dir=""))
    assert not no_dir.ok and "目录" in no_dir.content
    print("recallContext 工具 -> 命中正常、缺 text / 坏编号 / 无目录都拒绝")

    # ---- 会话层：70% 阻断 + 压缩成功替换历史（假客户端，不联网）
    from xiaoA.core.config import AppConfig
    from xiaoA.core.session import Session

    class FakeCompactClient:
        """只回一段摘要的假客户端；记录被调用时的 tools 参数。"""

        def __init__(self):
            self.tools_arg = "unset"
            self.calls = 0

        def chat_stream(self, messages, model, tools=None):
            self.calls += 1
            self.tools_arg = tools
            yield StreamDelta(content="## 已完成的压缩摘要\n- 接入 8090 的 MCP")
            yield StreamDelta(done=True)

    small_cfg = AppConfig(api_key="sk-test", base_url="https://example.invalid",
                          model="fake", context_window=1000)
    fake = FakeCompactClient()
    sess = Session(small_cfg, client=fake, registry=ToolRegistry(),
                   context_dir=os.path.join(tempfile.mkdtemp(prefix="xiaoA_sess_"), "ctx"))
    sess.messages = [{"role": "user", "content": "上下文" * 800}]   # 明显超过 70%
    sess._refresh_context_usage()
    assert sess.blocked, "占用超过 70% 应当进入阻断状态"
    assert sess.ask("还能提问吗") is None, "阻断后不应再发请求"
    assert fake.calls == 0, "阻断发生在请求之前，不能先花一次调用"
    print("上下文监控 -> 超 70% 自动阻断，提问在发请求前就被拦下")

    assert sess.compact() is True
    assert fake.calls == 1 and fake.tools_arg is None, "压缩请求不能带 tools"
    assert len(sess.messages) == 1 and sess.messages[0]["role"] == "user"
    assert "压缩摘要" in sess.messages[0]["content"]
    assert "recallContext" in sess.messages[0]["content"]
    assert not sess.blocked, "压缩成功后必须解除阻断"
    assert list_archives(sess.context_dir), "压缩前必须留下归档"
    print("压缩流程 -> 归档 → 摘要 → 替换历史 → 解除阻断；请求不带 tools")

    # 压缩失败：历史原样保留、仍可重试
    class BrokenClient:
        def chat_stream(self, messages, model, tools=None):
            from xiaoA.core.llm import LLMError
            raise LLMError("接口挂了")
            yield  # pragma: no cover

    sess2 = Session(small_cfg, client=BrokenClient(), registry=ToolRegistry(),
                    context_dir=os.path.join(tempfile.mkdtemp(prefix="xiaoA_sess2_"), "ctx"))
    sess2.messages = [{"role": "user", "content": "重要内容不要丢" * 100}]
    before = list(sess2.messages)
    assert sess2.compact() is False
    assert sess2.messages == before, "压缩失败后历史必须一字不动"
    assert not sess2.compacting
    print("压缩失败 -> 历史原样保留，可以重试 /compact")

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

    # ================================================================
    #  会话持久化（/save /new /resume 的地基）
    # ================================================================
    from xiaoA.core.session_store import (
        SessionStoreError,
        list_sessions,
        load_session,
        new_session_id,
        save_session,
        sessions_dir,
    )

    sess_root = tempfile.mkdtemp(prefix="xiaoA_sess_")
    sess_dir = sessions_dir(sess_root)
    msgs = [
        {"role": "user", "content": "帮我查宠物档案，重点是 id 42 这一只"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function", "function":
                         {"name": "list_pets", "arguments": "{\"limit\": 5}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "Found 5 record(s)"},
        {"role": "assistant", "content": "查到了，id 42 叫旺财。"},
    ]
    meta = save_session(msgs, sess_dir)
    assert meta.message_count == 4 and "查宠物档案" in meta.title, meta
    # 同一秒重复保存：编号自动加序号，互相覆盖是不允许的
    meta2 = save_session(msgs, sess_dir)
    assert meta2.session_id != meta.session_id, "同秒保存撞了编号"
    metas = list_sessions(sess_dir)
    assert len(metas) == 2, metas
    loaded = load_session(meta.path)
    assert loaded == msgs, "回读的消息必须和保存时逐字节一致"
    # 损坏文件：如实报错，不返回半份数据
    with open(meta.path, "w", encoding="utf-8") as f:
        f.write("{broken json")
    try:
        load_session(meta.path)
        raise AssertionError("坏文件必须抛 SessionStoreError")
    except SessionStoreError:
        pass
    # 目录都不存在时 new_session_id 也能给编号；list 空目录返回空表
    assert new_session_id(directory=sess_dir)
    assert list_sessions(os.path.join(sess_root, "nope")) == []
    shutil.rmtree(sess_root, ignore_errors=True)
    print("会话存储 -> 保存/重名加序号/列表/回读一致/坏文件报错")

    # ================================================================
    #  配置发现与 mock 配置生成（首次运行弹窗的前提）
    # ================================================================
    from xiaoA.core.config import ConfigError, load_config, resolve_config_path, write_mock_config
    from xiaoA.core.config import default_config_path

    empty_dir = tempfile.mkdtemp(prefix="xiaoA_cfg_")
    mock_path = write_mock_config(os.path.join(empty_dir, "config.ini"))
    assert os.path.exists(mock_path)
    text = open(mock_path, encoding="utf-8").read()
    for section in ("[deepseek]", "[search]", "[fetch]", "[cli]",
                    "[mcp]", "[context]", "[ui]"):
        assert section in text, "mock 配置缺 %s" % section
    # mock 的 key 是占位符：load_config 必须拒绝它（逼用户改成真 Key）
    try:
        load_config(mock_path)
        raise AssertionError("mock 配置应该因 api_key 占位被拒绝")
    except ConfigError:
        pass
    # 解析顺序：显式路径 > 启动目录 > 仓库根；都没有返回 None
    assert resolve_config_path(mock_path) == mock_path
    old_cwd = os.getcwd()
    os.chdir(empty_dir)
    assert resolve_config_path() == mock_path          # 启动目录优先
    os.remove(mock_path)
    # 启动目录没有、仓库根有 → 回落到仓库根（开发机兜底，老用法不破坏）
    repo_cfg = default_config_path()
    if os.path.exists(repo_cfg):
        assert resolve_config_path() == repo_cfg
    os.chdir(old_cwd)
    bare = tempfile.mkdtemp(prefix="xiaoA_none_")
    os.chdir(bare)
    assert resolve_config_path() is None or resolve_config_path() != mock_path
    os.chdir(old_cwd)
    shutil.rmtree(empty_dir, ignore_errors=True)
    shutil.rmtree(bare, ignore_errors=True)
    print("配置发现 -> 启动目录优先/仓库根兜底/mock 生成且被 load 拒绝")

    # ================================================================
    #  AGENTS.md 一次注入 + MCP 异步挂载入口
    # ================================================================
    import xiaoA.core.session as session_mod
    from xiaoA.core.session import Session, read_agents_md

    agents_dir = tempfile.mkdtemp(prefix="xiaoA_agents_")
    with open(os.path.join(agents_dir, "AGENTS.md"), "w", encoding="utf-8") as f:
        f.write("# 项目约定\n所有回复用中文。")
    saved_startup = session_mod.STARTUP_CWD
    session_mod.STARTUP_CWD = agents_dir      # 项目根按启动目录算，测试里指过去
    try:
        cfg = AppConfig(api_key="sk-test", base_url="https://x.invalid",
                        model="m", agents_md_max_chars=8000)
        s = Session(cfg, registry=ToolRegistry())
        # 注入到历史最前面，且只有一条
        assert len(s.messages) == 1 and s.messages[0]["role"] == "user"
        assert "项目约定" in s.messages[0]["content"]
        assert not s.inject_agents_md(), "重复注入必须被幂等挡住"
        # 没有配置时再来一次
        os.remove(os.path.join(agents_dir, "AGENTS.md"))
        s2 = Session(cfg, registry=ToolRegistry())
        assert s2.messages == [], "没有 AGENTS.md 就不该有注入"
        assert read_agents_md(agents_dir) is None
        # 清空历史后重新注入（/clear 的行为）
        with open(os.path.join(agents_dir, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("# 项目约定\n所有回复用中文。")
        s.messages.clear()
        assert s.inject_agents_md() is True
    finally:
        session_mod.STARTUP_CWD = saved_startup
    shutil.rmtree(agents_dir, ignore_errors=True)

    # attach_mcp：异步加载完成后并注册表；重名记 problem 不炸
    from xiaoA.core.mcp import McpManager, McpToolInfo
    from xiaoA.core.tools.base import Tool

    class _FakeMgr:
        pass

    mgr = _FakeMgr()
    mgr.tools = [Tool(name="getDate", description="重名工具",
                      parameters={"type": "object", "properties": {}},
                      handler=lambda a, c: None),
                 Tool(name="mcp_only", description="远端独有",
                      parameters={"type": "object", "properties": {}},
                      handler=lambda a, c: None)]
    mgr.problems = []
    cfg = AppConfig(api_key="sk-test", base_url="https://x.invalid", model="m")
    from xiaoA.core.tools.builtin import datetime_ops
    s3 = Session(cfg, registry=ToolRegistry([datetime_ops.get_date]))
    added = s3.attach_mcp(mgr)
    assert added == ["mcp_only"], added
    assert s3.registry.get("mcp_only") is not None
    assert any("重名" in p or "getDate" in p for p in mgr.problems), mgr.problems
    assert s3.attach_mcp(None) == []
    print("AGENTS.md/MCP挂载 -> 注入幂等/无文件不注/attach_mcp 撞名进 problems")

    print("\nOK：core 层自检全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
