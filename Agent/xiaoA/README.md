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
    │   ├── skills.py           Skill 发现与注册（只读 name/description）
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
    │           ├── skill_ops.py  loadSkill（按需加载 SKILL.md 全文或技能内文件）
    │           ├── web_search.py  searchWe（联网搜索，百度千帆）
    │           ├── fetch_web.py   FetchWeb（抓网页正文 → Markdown，走本机 curl）
    │           └── fs_ops.py      10 个文件操作
    └── tui/                    ★ 终端界面
        ├── bridge.py           QueueSink / TextualConfirm（线程握手）
        ├── app.py              App 主体
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

## 界面操作

| 操作 | 说明 |
|---|---|
| Enter | 发送 |
| Shift+Enter | 换行（输入多行） |
| `/help` `/clear` `/quit` `/model` `/tools` | 命令 |
| Ctrl+Q / Ctrl+L / Ctrl+T / F1 | 快捷键 |
| y / n | 删除确认时回答（默认 n） |

删除类工具会先把文件路径打在屏幕上，等你按 y 才动手——**模型没有删除权限，你有**。

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

## 工具清单（16 个）

时间 `getDate`（调本机 date 命令）｜计算 `isLeapYear`｜
联网 `searchWe`（搜索）`FetchWeb`（读网页正文）｜
执行 `RunCli`（调任意本机命令行程序）｜技能 `loadSkill`（按需取技能全文/附件）｜
文件 `getCwd` `listDir` `getFileType` `readFile` `createFile` `createDir`
`editFile` `renamePath` `deleteDir`⚠ `deleteFile`⚠

`searchWe` 找 URL，`FetchWeb` 读全文，两步配合才能既知道"有什么"又看清"具体写了什么"。
`RunCli` 管"程序世界"：版本、构建、git、脚本；文件读写仍然走专用文件工具——
后者有路径校验和删除确认，`RunCli` 没有。

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
