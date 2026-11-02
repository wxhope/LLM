---
name: officecli
description: 当用户要求创建、读取、检查或修改 Word(.docx)、Excel(.xlsx)、PowerPoint(.pptx) 文件时使用。本机已装 OfficeCLI（单文件 exe，无需安装 Office），按 L1 读取 → L2 DOM 编辑 → L3 原始 XML 的顺序操作。
---

# OfficeCLI 技能

本机自带 OfficeCLI，可读写 .docx / .xlsx / .pptx，无需安装 Office 或任何依赖。

## 1. 怎么调用（Windows）

可执行文件（绝对路径）：

```
E:\skill\officecli-win-x64.exe
```

**永远用绝对路径**，不要依赖 PATH。用 `RunCli` 工具执行，程序名放 `command`，参数**逐个**放进 `args` 数组，不要拼成一条命令：

```json
{ "command": "E:\\skill\\officecli-win-x64.exe",
  "args": ["add", "E:\\work\\report.docx", "/body", "--type", "paragraph",
           "--prop", "text=Executive Summary", "--prop", "style=Heading1"] }
```

- 参数里含空格 / 方括号 / `$` / `=` 都没关系，`args` 逐个传即自动处理引号，**不要自己加引号**。
- 路径统一用绝对路径（中文路径也可以）。
- 先看退出码：不为 0 就读 stderr，别用同样参数重试。
- 验证可用性：`--version`（当前 1.0.158）。

## 2. 核心策略：L1 读取 → L2 DOM 编辑 → L3 原始 XML

优先用高层能力，L2 表达不了才下探。任何命令加 `--json` 可拿结构化输出。

**不确定属性名/取值格式/语法时，先查 help，不要猜**（一次 help 胜过反复试错）：

```bash
officecli help                        # 全部命令 + 全局选项
officecli help docx                   # 该格式所有元素类型
officecli help docx paragraph         # 完整 schema：属性、别名、示例
officecli help docx set paragraph     # 只看 set 可用的属性
```

格式别名：`word`→docx、`excel`→xlsx、`ppt`/`powerpoint`→pptx。

## 3. Quick Start

### Word
```bash
officecli create report.docx
officecli add report.docx /body --type paragraph --prop text="执行摘要" --prop style=Heading1
officecli add report.docx /body --type paragraph --prop text="营收同比增长 25%。"
officecli view report.docx text                  # 纯文本抽取
officecli view report.docx outline               # 大纲 / 结构
```

### Excel
```bash
officecli create data.xlsx
officecli set data.xlsx /Sheet1/A1 --prop value="姓名" --prop bold=true
officecli set data.xlsx /Sheet1/A2 --prop value="Alice"
officecli set data.xlsx /Sheet1 --prop sort="B desc" --prop sortHeader=true
```

### PPT
```bash
officecli create slides.pptx
officecli add slides.pptx / --type slide --prop title="Q4 汇报" --prop background=1A1A2E
officecli add slides.pptx '/slide[1]' --type shape --prop text="营收增长 25%" --prop x=2cm --prop y=5cm --prop font=Arial --prop size=24 --prop color=FFFFFF
```

## 4. L1：创建、查看、检查

```bash
officecli create <file>                 # 按扩展名建空白 docx/xlsx/pptx
officecli view <file> outline|stats|issues|text|annotated|html
officecli get <file> <path> --depth N   # 取节点及其子节点 [--json]
officecli query <file> <selector>       # CSS 式查询
officecli validate <file>               # 校验 OpenXML schema
```

`view issues` 很适合改完自查（`--type format|content|structure`）。`view html` 生成静态快照，`view screenshot` 出 PNG。

路径一律 **1-based**：`'/body/p[3]'` 是第 3 段；`--index` 是 **0-based**（Excel 的 add row/col 例外，是 1-based）。

## 5. L2：DOM 编辑

```bash
officecli set <file> <path> --prop key=value [--prop ...]
officecli add <file> <parent> --type <type> [--prop ...] [--after <path>|--before <path>|--index N]
officecli add <file> / --from '/slide[1]'      # 克隆元素
officecli move|swap|remove <file> <path> ...
officecli set <file> / --find draft --replace final    # 全局替换（/ 表示整篇）
officecli set doc.docx '/body/p[1]' --find 天气 --prop bold=true --prop color=red
```

取值格式：颜色 `FF0000`/`red`/`rgb(255,0,0)`/`accent1`；间距 `12pt`、`0.5cm`、`1.5x`；尺寸 `2.54cm`、`72pt`、`96px`。点号别名如 `font.color=red`、`font.bold=true`。

多步操作优先用**稳定 ID 路径**（`@id=` / `@paraId=` / `@name=`），避免插入删除后位置索引漂移。

批量操作用 `batch`（默认原子：任一项失败整体回滚；要保留部分成功加 `--best-effort`）。

## 6. L3：原始 XML（L2 表达不了时才用）

```bash
officecli raw <file> <part>
officecli raw-set <file> <part> --xpath "..." --action replace|append|prepend|insertbefore|insertafter|remove|setattr --xml '<w:p>...</w:p>'
officecli add-part <file> <parent>
```

## 7. Windows 上的坑

| 坑 | 正确做法 |
|---|---|
| 用 `officecli` 裸命令 | 用 `E:\skill\officecli-win-x64.exe` 绝对路径 |
| 把整条命令拼成一个字符串 | `RunCli` 的 `args` 逐个传 |
| 文件正被 Word/WPS/Excel 打开 | 先关闭再改，否则写盘失败 |
| 文本里要换行 | `--prop text="第一行\n第二行"`（需要真实换行符时用 batch） |
| 文本里含 `$` | 走 `args` 逐个传即可，别让 shell 吃掉 |
| 改完不检查 | `validate` + `view issues` 收尾 |

## 8. 完整参考

更细的 schema、pivot table、chart、动画、watch 交互选中等内容，见本机全量说明文档：

```
E:\skill\OfficeCli.md
```

需要某类高级用法时先 `readFile E:\skill\OfficeCli.md`（文件较长，可按需分段读），或直接 `officecli help <format> <element>`。文档中的 `officecli` 均等价于上面的 exe 绝对路径。
