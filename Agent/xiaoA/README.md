# xiaoA

一个能读写本机文件的命令行助手。**core 与 ui 彻底分离**，同一个大脑可以接终端、浏览器、桌面窗口。

## 为什么这么分层

四节课下来最容易长成"一个大文件"：网络、工具、打印、键盘输入全缠在一起。
现在把这条缠成一团的线剪成几段，每一段只认识下一段：

```
        ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
        │    tui/      │   │    web/      │   │  desktop/    │   ← 界面层（可替换）
        │  Textual     │   │  (未实现)     │   │  (未实现)     │
        └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
               └──────────────┬───┴──────────────────┘
                              ▼
                    ┌────────────────────┐
                    │   core/ 业务核心    │   ← 不认识任何界面
                    │  session / tools   │
                    │  llm / config      │
                    └────────────────────┘
```

依赖永远单向：**界面 → core**。core 里没有一句 `print`，也没有 `import textual`，
`selfcheck.py` 里有一项就是专门检查这件事。

## 目录

```
xiaoA/
├── main.py                     统一入口：--tui / --check / --web / --desktop
├── selfcheck.py                离线自检，不联网、不装界面库也能跑
├── requirements.txt            只有 TUI 需要的 textual
├── config.example.ini
├── .agents/                    Skill 目录（项目根下，随仓库走）
│   └── skills/
│       └── example-hello/     示例技能，看懂后可删
│           └── SKILL.md
└── xiaoA/
    ├── core/                   ★ 业务核心（纯标准库）
    │   ├── config.py           读 ini → AppConfig，出错抛 ConfigError
    │   ├── events.py           往 UI 发的消息（TextDelta / ToolCallStarted / ...）
    │   ├── ports.py            对 UI 提的能力要求（EventSink / ConfirmPort）
    │   ├── session.py          对话编排：tool-calling 主循环
    │   ├── session_store.py    会话持久化（/save /new /resume 的存取）
    │   ├── skills.py           Skill 发现与注册（只读 name/description）
    │   ├── compact.py          上下文压缩：token 估算 / 归档 / 摘要 / 检索
    │   ├── mcp.py              MCP Client：连接外部 Server，工具动态并入注册表
    │   ├── llm/
    │   │   ├── client.py       唯一碰 http.client 的地方
    │   │   ├── stream.py       SSE 解析（纯函数，好测）
    │   │   └── types.py        ToolCall / AssistantMessage / StreamDelta
    │   └── tools/
    │       ├── base.py         工具契约 + @tool 装饰器 + DangerLevel
    │       ├── registry.py     注册表：自动收集、按名分发
    │       ├── defaults.py     默认装配（可剔除、可追加）
    │       └── builtin/        具体工具，按领域分文件
    │           ├── fs_guard.py    路径与安全防线
    │           ├── datetime_ops.py getDate
    │           ├── math_ops.py    isLeapYear
    │           ├── cli_ops.py     RunCli（调本机任意命令行程序）
    │           ├── skill_ops.py   loadSkill（按需加载全文/附件）+ createSkill（新建技能）
    │           ├── context_ops.py recallContext（在压缩归档里检索原始上下文）
    │           ├── web_search.py  searchWe（联网搜索，百度千帆）
    │           ├── fetch_web.py   FetchWeb（抓网页正文 → Markdown，走本机 curl）
    │           └── fs_ops.py      10 个文件操作
    └── tui/                    ★ 终端界面
        ├── bridge.py           QueueSink / TextualConfirm（线程握手）
        ├── dialogs.py          模态浮层：配置提示对话框 / 会话恢复面板
        ├── app.py              App 主体（含左侧 MCP 边栏）
        ├── app.tcss            样式，跟逻辑分开
        └── __main__.py         python -m xiaoA.tui
```

## 跑起来

```powershell
cd E:\LLM\Agent\xiaoA
copy config.example.ini config.ini      # 然后填入自己的 API Key
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

.\.venv\Scripts\python main.py --check   # 只看配置和工具能不能加载
.\.venv\Scripts\python main.py           # 启动 TUI
```

不用虚拟环境也行，直接 `pip install textual`。

**首次运行没写 config.ini？** 程序会自动在当前目录生成一份完整的初始配置（占位 Key），
并弹出对话框告诉你：改好 `api_key`、重启即可。配置文件按「当前目录 → 仓库根」的顺序查找，
从别的目录（比如技能工作区）启动时，仓库根的 config.ini 依然生效，不会弹窗打扰。

## 界面操作

| 操作 | 说明 |
|---|---|
| Enter | 发送 |
| Shift+Enter | 换行（输入多行） |
| 输入 `/` | 弹出命令补全面板，Tab 选中第一个候选 |
| Ctrl+Q / Ctrl+L / Ctrl+R / Ctrl+T / F1 | 快捷键 |
| y / n | 删除确认时回答（默认 n） |

### 斜杠命令

| 命令 | 说明 |
|---|---|
| `/help` | 查看全部命令 |
| `/new` | 新建会话（当前会话**自动保存**，AGENTS.md 重新注入） |
| `/resume` | 弹出历史会话列表面板：↑/↓ 选择、Enter 恢复、ESC 取消 |
| `/save` | 保存当前会话到 `.agents/sessions/` |
| `/sessions` | 列出已保存的会话 |
| `/clear` | 清空当前历史（不保存） |
| `/compact` | 压缩上下文（见下文） |
| `/model` `/tools` `/mcp` | 查看模型 / 工具 / MCP 服务器状态 |
| `/quit`（`/exit` `/q`） | 退出 |

删除类工具会先把文件路径打在屏幕上，等你按 y 才动手——**模型没有删除权限，你有**。

会话文件存在 `<启动目录>/.agents/sessions/<时间戳>.json`，完整保留消息顺序、
工具调用参数和返回结果，恢复后连多轮工具调用都能继续接上。

### 左侧边栏：MCP 服务器

边栏常驻显示每台已连接的 MCP 服务器（`●`）和它提供的远端工具；
没配置 `.mcp.json` 时显示引导文案。MCP 是**异步加载**的：界面先就绪、可以正常打字，
后台线程连上服务器后动态刷新边栏和工具清单，连接等待不会卡住启动。

## AGENTS.md：项目级约定

启动目录放一个 `AGENTS.md`（项目约定、编码规范、注意事项），会话首次加载时
它的内容会作为**首条用户消息**注入聊天历史，之后每轮请求都带着——
"一次注入，全程复用"，位置固定在历史最前面，前缀缓存友好。
`/clear` 和 `/new` 之后会重新注入；`/resume` 恢复的历史里已含注入，不重复。
字符上限由 `[context] agents_md_max_chars` 控制（默认 8000）。

## 流式打字机

回答是一个字一个字冒出来的，不是等整段生成完才出现。

链路本来就有三段，前两段一直是流式的，问题只出在第三段：

| 环节 | 状态 |
|---|---|
| `core/llm/client.py` 逐行读 SSE | 本来就逐块到达 |
| `core/session.py` 每个 delta 立刻 `TextDelta` | 本来就逐个转发 |
| `tui` 渲染 | **原来的瓶颈在这里** |

原来的渲染用的是 `RichLog`。它只能"追加整行"，一行写进去就定了，没法回头改——
所以代码只能攒够一个换行才往外写。结果是：**多行回答看着像流式，单行长回答会一直空着**，
直到出现第一个换行，那几百字才一次性蹦出来。

现在换成 `VerticalScroll` + 一条消息一个 `Static`：

- 正在生成的那条是普通 `Static`，每帧 `set_text()` 整个重写
- 生成结束就停止重写，它自然变成一条历史消息
- 这个"能被反复重写的最后一条"，就是逐字输出的物理前提

### 节奏怎么定

`_advance_stream()` 每 40ms 跑一次，收到的是 `_live_target`，显示的是 `_live_shown`，
两者之差就是积压。放字速度分三种情况：

| 情况 | 速度 |
|---|---|
| 正常 | 按 `type_delay` 走（默认 0.03 秒/字 ≈ 33 字/秒） |
| 积压 > 40 字 | 每帧吃掉积压的 1/5，约 200ms 追平 |
| 模型已说完（收尾） | 每帧吃掉积压的 1/8，约 320ms 追平，下限 3 字/帧 |

第二条是必须的。模型吐字通常比 33 字/秒快，如果死守固定速率，
300 字的回答模型 3 秒就完了，屏幕上却要放 9 秒——那不是流式，是回放。
追帧保证**显示速度永远不会长期落后于模型的吐字速度**。

`type_delay` 现在真的生效了（以前这个配置项读了但没人用）。
设成 `0` 就是关掉打字机，一次到底。

### 三个踩过的坑

**1. `scroll_end` 不能 await。** Textual 8 里它已经是同步方法，`await` 会抛
`TypeError: object NoneType can't be used in 'await' expression`。

**2. 按时间节流滚动会吞掉最后一帧。** 原来每 100ms 才滚一次，结果内容涨到
38 行、可视区只有 22 行，`scroll_y` 还是 0——越长的回答越看不到结尾。
改成按"帧"节流：同一帧多次请求合并，跨帧各排一次。

**3. `scroll_end` 要传 `force=True`。** 它内部有"内容没变就不滚"的判断，而那个判断
有缓存，靠不住。

## 工具清单（18 个）

时间 `getDate`（调本机 date 命令）｜计算 `isLeapYear`｜
联网 `searchWe`（搜索）`FetchWeb`（读网页正文）｜
执行 `RunCli`（调任意本机命令行程序）｜
技能 `loadSkill`（按需取技能全文/附件）`createSkill`（新建技能）｜
上下文 `recallContext`（在被压缩的归档里检索原文）｜
文件 `getCwd` `listDir` `getFileType` `readFile` `createFile` `createDir`
`editFile` `renamePath` `deleteDir`⚠ `deleteFile`⚠

`searchWe` 找 URL，`FetchWeb` 读全文，两步配合才能既知道"有什么"又看清"具体写了什么"。
`RunCli` 管"程序世界"：版本、构建、git、脚本；文件读写仍然走专用文件工具——
后者有路径校验和删除确认，`RunCli` 没有。

除此之外，**MCP 外部工具**会按 `.mcp.json` 动态并入这份清单（见下节）。

## 联网搜索（searchWe）

模型的知识有截止日期，`searchWe` 是它看今天的唯一途径。它调的是百度千帆
「智能搜索生成」：百度先搜一圈，再让指定模型把结果写成摘要，最后连参考来源一起返回。
工具把三样东西交回给模型 —— **摘要 + 每条来源的标题 + 链接**，所以回答里能落到具体出处。

### 配置

`config.ini` 里加一段（不配也能用，只是这个工具会说"没配 Key"）：

```ini
[search]
baidu_qianfan_api_key = bce-v3/ALTAK-xxxxx/yyyyy
model = ernie-4.5-turbo-128k
timeout = 30
```

| 项 | 说明 |
|---|---|
| `baidu_qianfan_api_key` | 千帆控制台 → API Key 管理。也兼容写在 `[deepseek]` 段的旧写法 |
| `model` | 出摘要用的模型，必须是这个账号已开通的。**留空 = 只返回网页来源，没有摘要** |
| `timeout` | 单次搜索超时秒数，默认 30 |

Key 只从 ini 读，不硬编码、也不偷偷读环境变量 —— 出问题只可能是一个地方写错了。

### 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `query` | 是 | 问题或关键词，自然语言写，越具体越好 |
| `top_k` | 否 | 返回几条来源，1-10，默认 5 |
| `recency` | 否 | `week` / `month` / `semiyear` / `year`，问「最新」时建议填 |

### 出错时

所有失败都变成一句人话回填给模型，而不是抛异常炸掉对话 ——
模型看到"401 Key 无效"就会换个策略或如实告诉用户，而不是瞎编一个答案。

| 情况 | 模型收到的内容 |
|---|---|
| 没配 Key | 指出 config.ini 的 `[search]` 段该怎么填 |
| Key 无效（401） | 鉴权失败 + 接口原文 code |
| 没开通服务（403） | 提示去千帆控制台开通 |
| 模型没开通 | 原文 `invalid_model` + 建议换个 model |
| 限流（429） | 提示额度/频率 |
| 超时 / 断网 / DNS 失败 | 分别说明，不停在同一句"请求失败" |
| 返回空结果 | 建议换更具体的关键词 |

## 读网页正文（FetchWeb）

搜索结果只有摘要；要核对原文、读官方文档、看用户给的那个链接，就得把整页抓下来。
`FetchWeb` 用**本机 curl** 下载，再把 HTML 转成 Markdown。

### 为什么调 curl、为什么不用 shell

- 压缩、重定向、TLS、反爬的 UA 检查，curl 都替我们扛了，Windows 10 1803+ 自带。
- 命令用**参数列表**传，`shell=True` 一律不用。URL 是模型给的，天然不可信；
  走 shell 的话，链接里一个 `&` 或 `$(...)` 就是命令注入。
- 另外设了 `--proto =http,https` 和 `--proto-redir =http,https`，重定向也跳不出这两个协议。

### 保留什么、丢掉什么

| 保留 | 丢掉 |
|---|---|
| 标题（h1-h6）、段落、列表、表格 | `<script>` `<style>` `<nav>` `<footer>` `<aside>` `<form>` |
| 链接（相对地址自动转绝对）、图片 | class/id 含 nav / ad / banner / sidebar / comment / cookie 等词的容器 |
| 引用块、代码块（**缩进原样保留**） | 页脚版权、分享按钮、推荐位、分页器 |

两个关键取舍：

1. **正文容器优先**。`<article>` `<main>` `role="main"` 或 class 含 content/post/body 的容器
   会被单独收集，最后和整页文本比长度——但必须同时满足"够长"和"占整页 25% 以上"，
   否则不敢只留它（有的站点 `class="content"` 只包了个侧栏）。
2. **过滤过头会回退**。第一遍按 class/id 狠狠砍，第二遍只砍标签级噪音；
   如果第一遍砍完只剩不到 15%，就认定是关键词误伤，用第二遍的结果。
   这个回退是踩出来的：python.org 的文档目录用 `class="small-widget"` 装正文，
   早先把 `widget` 当广告词，整页内容被删空。

### 配置

```ini
[fetch]
timeout = 30
max_chars = 12000
```

`max_chars` 是**一次抓取最多吃多少上下文**的闸门。超过就截断并在正文末尾说明，
模型看到提示可以换别的页面补。`max_chars` 也能在单次调用里用参数临时改。

### 出错时

| 情况 | 模型收到的内容 |
|---|---|
| 不是 http/https | 指出去用 `readFile` 读本地文件 |
| 404 / 403 / 429 | 分别说明：链接过期 / 要登录或被拒绝 / 被限流 |
| 超时 | curl 的 `--max-time` 到点，报文里带 `curl: (28)` |
| DNS 失败 / 连不上 | 提示可能断网或走了代理 |
| 返回的不是 HTML | 报出真实 content-type（PDF、图片会被挡） |
| 抓回来不像 HTML | 说明可能是 JavaScript 现场渲染的空壳 |
| 正文不足 80 字符 | 判定正文提取失败，说明可能是登录墙或 iframe |
| curl 不存在 | 指出 Windows 10 1803+ 自带，或去 curl.se 下载 |

## 执行本地命令（RunCli）

`RunCli` 让 Agent 能调用本机已经装好的任何命令行程序：`git`、`npm`、`python`、`node`、
`ffmpeg`、`ipconfig`……问"装没装 Python""这个仓库改了什么""跑一下这个脚本"时用它。

### 三个不能省的设计

**1. 参数列表，`shell=False`**

```python
subprocess.run([程序, *参数], shell=False, ...)   # ✅
subprocess.run(f"{程序} {' '.join(参数)}", shell=True)   # ❌ 绝不这么写
```

`program` 和 `args` 是模型给的，天然不可信。走 shell 的话，参数里一个 `&`、`|`、`$()`
就会被当成命令分隔符——那不是"参数"，那是"第二条命令"。用列表直接交给
`CreateProcess`/`execve`，`"; rm -rf /"` 只是一个普通字符串，不会被解释。

**2. stdout、stderr、退出码，三样一起回**

只看 stdout 是新手最常犯的错：很多命令失败时一个字都不打印，答案全在 stderr 和
退出码里。模型拿到三样才能判断"跑成了没有"，而不是靠猜。

```
【命令】E:\Git\cmd\git.EXE status --short
【退出码】0（成功）
【stdout】共 24 字符
?? api/ai.js
【stderr】（空）
```

**3. 超时 + 截断**

- 超时：命令可能永远不返回（等输入、死循环、网络卡住）。没有超时，整个 Agent 就卡在
  这里，用户只能杀进程。到点强制结束，并把它死之前吐出来的输出交回来——那正是排查
  "卡在哪一步"的线索。
- `stdin` 接的是 `DEVNULL`，不是键盘。等输入的命令会立刻拿到 EOF 退出，而不是挂到超时。
- 截断：`npm install` 能刷几千行。stdout 和 stderr **各自**有上限，超了**砍中间、留两头**，
  并写明"省略了多少字符"。只留开头不行——测试汇总、编译报错数、`git status` 的统计行
  都在结尾。

### 参数

| 参数 | 必填 | 说明 |
|---|---|---|
| `command` | 是 | 程序名，只写程序本身：`git`、`python`、`npm`。**不要带参数** |
| `args` | 否 | 参数数组，逐个字符串：`["status", "--short"]`。路径含空格也不用自己加引号 |
| `cwd` | 否 | 在哪个目录执行，绝对路径。跑 `git` / `npm` 时建议显式给 |
| `timeout` | 否 | 本次超时秒数，默认 30，上限 600 |

### 配置

```ini
[cli]
timeout = 30        # 单条命令跑多久算超时
max_chars = 4000    # stdout 和 stderr 各自的上限
```

### 出错时

这里最容易踩的不是"跑失败"，而是"根本没跑起来"。每种都有对应的话：

| 情况 | 模型收到的内容 |
|---|---|
| `command` 写成 `"git status"` | 指出 command 只能放程序名，并给出拆开的写法 |
| `args` 写成字符串 | 要求传数组，并给出正确例子 |
| 程序没装 / PATH 里没有 | 说明找不到，并建议用 `where`/`which` 先确认 |
| `dir` `copy` `type` 等 | 指出这是 **cmd 内建命令**，磁盘上没有 exe；改用文件工具 |
| `xxx.py` / `xxx.ps1` | 说明那是脚本不是程序，给出 `python xxx.py` 的写法 |
| 工作目录不存在 | 报出那个目录 |
| 退出码 ≠ 0 | 退出码标"失败"，stderr 原样带回，并提示"别用同样的参数重试" |
| 超时 | 说明被强制终止，附上已产生的输出，提示调大 `timeout` 或拆小任务 |

### 安全边界（重要，别跳过）

这个阶段**故意没有**白名单、黑名单，也没有逐条审批。原因只有一个：让学生清楚看见
「模型 → tool_call → subprocess → 结果回填 → 模型决定下一步」这条完整链路，
中间不插任何黑箱。

代价要说清楚：**命令以运行 xiaoA 的那个用户的权限执行**，它能干的事和这个人自己
在终端里手打一样多，不会更多，也不会更少。也就是说，模型理论上可以删你的文件。
在这个受控的教学环境里可以接受，但有三条底线仍然守着：

- 不走 shell，参数不参与解析（注入面直接消失）
- 有超时，跑飞的命令不会把 Agent 拖死
- 输出有上限，不会把上下文窗口一次吃光

真要上生产，两步就能收紧，而且不用动 `cli_ops.py` 的其余部分：

1. 把工具声明里的 `danger=DangerLevel.SAFE` 改成 `DESTRUCTIVE`——
   `session.py` 会自动在每条命令执行前弹确认口（就是删除文件用的那个）。
2. 在 `build_argv()` 前面加一层白名单，只放行 `git` `python` `npm` 这类。

## Skill 发现与注册

对齐 Agent Skills 规范的最小子集：xiaoA 初始化时，以**程序启动时的工作目录**为项目
根目录，扫描 `.agents\skills\` 下的各个技能子目录。每个子目录至少要有一个 `SKILL.md`，
开头用 YAML Frontmatter 声明必需的 `name` 和 `description` 两个字段：

```markdown
---
name: example-hello
description: 一句话说清这个技能是干什么的
---

（正文，初始化阶段一个字都不读）
```

两条刻意的限制：

- **初始化只读元信息。** frontmatter 解析只取文件前 64 KB、只留 name 和 description，
  正文有多长都不碰——技能清单是给"知道有什么"用的，不是给"全部背下来"用的。
  完整说明由模型在需要时用 `loadSkill` 取，见下文。
- **坏技能不炸程序。** 没有 SKILL.md、缺字段、frontmatter 没闭合的目录，落选原因
  写进 `SkillCatalog.problems`，其余技能照常注册；连 `.agents/skills` 目录本身
  不存在也只是得到空表。技能缺失是常态，不是故障。

frontmatter 解析是纯标准库的手写实现（只支持 `key: value` 这一层），不为它引入
pyyaml。`Session` 初始化时自动扫描并挂载；`main.py --check` 和 TUI 启动横幅会打印
`技能（N）：a、b` 和每条落选原因。示例技能在 `.agents/skills/example-hello/`，
看懂后删掉整个目录即可。

### 注入 System Prompt 与按需加载

发现技能后，`session.build_system_prompt()` 会在基础提示词后追加一段清单：

```
You have access to the following skills:
- "example-hello"：示例技能——演示 .agents/skills 的目录布局……
Use the "loadSkill" tool to load the full instructions of a skill when needed.
You can also load additional files referenced by a skill.
```

- **无技能时一个字都不加**：`loadSkill` 这些字样不会出现在提示词里，模型根本
  不知道有这回事，也就不会去调一个必然失败的工具。
- **`loadSkill`（core/tools/builtin/skill_ops.py）** 两个参数：
  - `name`（必需）：技能名。只传它 → 返回该技能 SKILL.md 全文；
  - `filename`（可选）：技能内的具体文件。传了就只读那个文件，不重复带 SKILL.md。
    解析顺序：技能目录内相对路径 → 项目相对路径 → 绝对路径（仅限落在技能目录内的）
    → 裸文件名（技能目录内递归搜索：唯一命中直接读，多个同名返回候选路径由模型
    决定，没有命中说明已搜过）。
  - 失败都不抛异常：缺参数、未知技能、空注册表、路径越界、二进制文件，各自返回
    带现状说明和下一步建议的错误文本。
- **安全边界：读文件的范围锁死在该技能目录内。** 所有路径先 realpath 解析再核对
  归属，`../` 穿越和指向目录外的绝对路径一律拒绝——技能文件再怎么翻也翻不出
  自己的目录。其他文件的读取走现成的 readFile（有自己的防线），二进制附件提示
  用 RunCli 执行而不是读成乱码。
- **动态加载原则**：SKILL.md 和附件的内容只有在模型调用 loadSkill 时才进入对话
  上下文；单次加载受 `max_read_chars` 约束，超长截断并标明总量。
  loadSkill 返回的脚本文本，模型交给 RunCli 执行、结果再用文件工具落盘——
  与现有工具就是普通的多轮 tool-calling 协作，工作流程没有任何特判。

### createSkill：让模型自己落盘一个技能

用户说"给我做个 XX 技能"时，模型调 `createSkill(name, skillcontext)`，工具在
`.agents/skills/<name>/` 下写好 `SKILL.md`。只做这一件事，附属文件交给
`createDir` / `createFile`——职责单一，出错好定位。

- **name 先规范化**：小写化，非 `[a-z0-9]` 的字符换成连字符，连续连字符合并，
  去首尾连字符，最长 64。`"Weekly Report"` → `weekly-report`。纯中文名压完是空串，
  直接拒绝并让模型给英文名——音译猜出来多半不是用户想要的。
- **frontmatter 由工具保证合法**：skillcontext 自带 `---` 开头就解析它（坏格式
  报错让模型重写，不替它猜）；没有就按 name 生成 `name`，`description` 取正文
  第一句有效文字（跳过标题、代码块、表格）。`name` 一律以规范化后的目录名为准，
  与目录名保持一致——这是 `discover_skills` 的注册前提。
- **已存在就不写**：SKILL.md 已存在 / 目录非空但没 SKILL.md / 同名文件占位，
  三种情况都拒绝并说明怎么改（`readFile` + `editFile`），绝不覆盖。
- **建完当场可用**：通过 `ToolContext.refresh_skills` 回调触发
  `Session.refresh_skills()`，重扫目录并重建系统提示词。不这么做的话，
  新技能要等下次启动才进清单，模型马上 `loadSkill` 只会得到"没有这个技能"。
  没挂回调时（自检里的裸 ctx）如实回"重启后生效"，不假装成功。
- **返回里写清下一步**：文件绝对路径、实际的 description、能否直接 loadSkill、
  加 `references/` `scripts/` 该用哪两个工具。模型不用猜。

## MCP Client：挂载外部 MCP Server

xiaoA 启动时读**启动目录**下的 `.mcp.json`（和技能扫描同一个"项目根"），
按 MCP Python SDK 的 Streamable HTTP 协议连接外部 Server，把它们的工具
动态注册进自己的工具集合。模型看到的就是普通 Function Calling 工具，
完全感知不到背后是 MCP。

### 配置（.mcp.json）

```json
{
  "mcpServers": {
    "PetHospital": { "url": "http://127.0.0.1:8090/mcp" }
  }
}
```

需要认证头时加 `headers`：`{"url": "...", "headers": {"Authorization": "Bearer xxx"}}`。
模板见 `.mcp.example.json`；超时和返回上限在 config.ini 的 `[mcp]` 段。

### 四步链路

1. **读配置** `parse_mcp_config`：纯函数，坏项只进 problems 不炸程序；
2. **连接+发现**：SDK 的 `streamable_http_client` + `ClientSession`，
   持久连接跑在专属后台线程的事件循环里（SDK 是 asyncio 的，core 是同步的，
   两个世界用 `run_coroutine_threadsafe` 对接）；
3. **注册**：`list_tools()` 的每个工具包成本地 `Tool`。暴露名规则——
   消毒后全集群唯一且不撞本地工具 → 裸名；否则加「服务器名__」前缀；
   中文名（连"消毒成下划线"都做不到的）回退 `server` 并靠冲突规则兜底。
4. **转发调用**：模型发起 tool_call 后按「暴露名 → (服务器, 原名)」映射转发；
   文本内容直接拼、`structuredContent` 转 JSON 附后、`isError` 转 ok=False。

### 失败哲学

连不上的 Server 只进 problems（横幅/`--check` 一行说明原因），其余 Server、
其余工具、整个对话完全不受影响。没装 `mcp` 库也一样：其他 17 个工具照常。

## 上下文压缩与历史检索（/compact + recallContext）

长任务迟早会把上下文窗口撑爆。爆了的症状不是变慢，是接口直接 400，整轮白跑。
这一节做的事就四件：**量清楚、存下来、压成摘要、还能查回去**。

### 用法

- 随时输入 `/compact`：归档当前历史 → LLM 写一份摘要 → 用摘要替换历史。
- 占用到 **70%** 时自动警告并**暂停接收新提问**，只认 `/compact`；压缩成功才解禁。
- 状态栏一直显示：`上下文 42.3k / 128k（33%）`。窗口没配置时后面跟「默认」，
  表示 128k 是兜底值不是模型真实值。

### 四步链路

1. **估量**（`compact.compute_usage`）：优先用服务端 `usage.prompt_tokens`（精确，
   含 system 与 tools）；拿不到就本地估算（中文≈1 字 1 token、英文≈4 字符 1 token）。
   取 `max(API 值, 估算值)`——宁可早警告，不要等 400。
2. **归档**（`archive_messages`）：写 `<启动目录>/.agents/context/YYYYmmdd-HHMMSS.jsonl`，
   第一行 meta（编号/时间/条数），之后每行一条消息，保留 role / content /
   tool_calls / tool_call_id 和**原始顺序**。写完 `flush + fsync` 并读回核对行数，
   **校验不过就中止压缩**——宁可没压缩，不能丢原文。
3. **摘要**（`build_compact_messages` + 专用 Prompt）：把历史拍平成纯文本
   （工具名、参数、结果都保留），system 段放压缩专用 Prompt，要求产出约 5,000 tokens
   的 Markdown 工作摘要。这一步是压缩期间唯一允许的请求，**且不带 tools**。
4. **替换**（`build_summary_message`）：历史换成一条 user 消息 = 摘要 + 归档说明
   （"此前上下文已于 … 压缩，原始上下文保存在编号为 … 的归档文件中…请使用
   recallContext 工具"）。System Prompt 和工具定义在每次请求时另拼，天然保留。

失败处理：归档失败、请求失败、摘要为空——三种都**不碰历史**，报错后可以重来 `/compact`。

### recallContext

摘要一定丢细节（哪个错误码、哪个路径、当初怎么定的）。模型发现缺东西时调：

```json
{"text": "BACKEND_API_ERROR"}                 // 所有归档里找
{"text": "8090", "id": "20261022-153000"}     // 只在指定归档里找
```

本地全文检索（子串、大小写不敏感），命中按次数排序，返回**归档编号 + 消息序号 +
角色 + 原文片段**，总长度有上限（默认 4,000 字符）——检索是为补细节，不是把归档再灌回去。

### 配置（config.ini 的 [context]）

```ini
[context]
window = 128000     # 模型窗口；不写按 128000 且状态栏标「默认」
warn_ratio = 0.70   # 告警线
```

### 取舍说明

- **不用向量库/Embedding**：关键词检索 60 行就能读懂、能调试；真要语义检索，
  换掉 `search_archives` 一个函数即可。
- **摘要不硬校验 5,000 tokens**：给模型的是要求，实际长度由它决定；测下来中文
  摘要通常 1k~3k tokens，已远小于原始历史。
- **压缩只压缩历史，不压 system/tools**：模型的基本能力和工具清单不能因为压缩而变。

## 加一个新工具

只写一个函数，不用改任何别的地方：

```python
# core/tools/builtin/net_ops.py
from ..base import tool, empty_params

@tool("getIp", "查询本机内网 IP", empty_params(), tags=["net"])
def get_ip(args, ctx) -> str:
    ...
```

然后在 `builtin/__init__.py` 里把它加进模块列表即可。注册表会自动算进
schema、算进分发——旧版那种"三处同步改、漏一处静默失效"的问题没有了。

## 四节课的知识点去哪了

| 旧版 | 现在的位置 |
|---|---|
| 流式打字机 | `core/llm/client.py`（逐行读 SSE）+ `tui/app.py::_advance_stream`（逐帧放字） |
| tool_calls 分片拼接 | `core/session.py::_accumulate_call` |
| 12 个工具的大 if/elif | `core/tools/`（装饰器 + 注册表） |
| `confirm()` 里的 `input()` | `core/ports.py::ConfirmPort`（core 不知道有键盘） |
| `print("出错…")` | `core/events.py::ErrorOccurred` |
| `sys.exit(1)` | `core/config.py::ConfigError`，由界面决定怎么显示 |

## 下一步：Web 版怎么接

core 一行都不用改，只要做到两件事：

1. 写一个 `WebSink`，实现 `emit(event)`：把事件 JSON 化后推进 WebSocket。
2. 写一个 `WebConfirm`，实现 `ask(title, msg) -> bool`：发一个请求、等前端回包。

然后用 `XiaoAApp` 的同一个 `Session`（`Session(config, sink=..., confirm=...)`）即可。
`tui/bridge.py` 里的 `QueueSink` 就是这套东西的终端版参考实现。
