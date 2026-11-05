# -*- coding: utf-8 -*-
"""配置：只负责"把 ini 读成对象"，不负责打印提示、不负责退出程序。

旧版是把提示和 sys.exit 写在里面的，那是 UI 的活儿。
这里出错只抛 ConfigError，让界面决定是弹窗、还是打一行红字。
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from typing import Optional

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"

# 联网搜索（百度千帆 · 智能搜索生成）的默认值。
# model 不填的话接口只回网页列表、不回摘要，所以这里给一个能出摘要的默认模型；
# 各家账号开通的模型不一样，填错了工具会把接口报错原样带回来，改 ini 即可。
DEFAULT_SEARCH_MODEL = "ernie-4.5-turbo-128k"
DEFAULT_SEARCH_TIMEOUT = 30.0

# 网页抓取（FetchWeb）的默认值
DEFAULT_FETCH_TIMEOUT = 30.0
DEFAULT_FETCH_MAX_CHARS = 12000

# 本地命令（RunCli）的默认值
DEFAULT_CLI_TIMEOUT = 30.0
DEFAULT_CLI_MAX_CHARS = 4000

# MCP 客户端的默认值（.mcp.json 里的 Streamable HTTP 服务器）
DEFAULT_MCP_CONNECT_TIMEOUT = 10.0
DEFAULT_MCP_CALL_TIMEOUT = 60.0
DEFAULT_MCP_MAX_CHARS = 8000

# 上下文窗口：拿不到模型真实值时用 128k（题目要求），并在界面上标「默认」
DEFAULT_CONTEXT_WINDOW = 128000
DEFAULT_CONTEXT_WARN_RATIO = 0.70

# AGENTS.md 一次注入的上限：再大的项目说明也不该把首屏上下文吃满
DEFAULT_AGENTS_MD_MAX_CHARS = 8000

# TUI 的默认节奏参数（[ui] 段，全部可选）
DEFAULT_POLL_INTERVAL = 0.04        # 事件队列轮询间隔，40ms ≈ 25fps
DEFAULT_FAST_FORWARD_CHARS = 40     # 积压超过这个字数就开始追帧
DEFAULT_FAST_FORWARD_DIVISOR = 5    # 追帧时每帧吃掉积压的 1/5
DEFAULT_DRAIN_MIN_STEP = 3          # 收尾阶段每帧至少放几个字
DEFAULT_DRAIN_DIVISOR = 8           # 收尾阶段每帧吃掉积压的 1/8

REQUIRED_SECTION = "deepseek"
SEARCH_SECTION = "search"
FETCH_SECTION = "fetch"
CLI_SECTION = "cli"
MCP_SECTION = "mcp"
CONTEXT_SECTION = "context"
UI_SECTION = "ui"

# 占位符：填这几个等于没填（"sk-xxxx" 开头是自动生成的 mock 配置里的占位 Key）
_PLACEHOLDERS = ("你的key", "你的百度千帆key", "your_api_key", "xxx")
_PLACEHOLDER_PREFIX = "sk-xxxx"


def _is_placeholder(value: str) -> bool:
    """占位 Key 判断：等值命中占位表，或以 mock 占位前缀开头。"""
    v = (value or "").strip()
    return v in _PLACEHOLDERS or v.startswith(_PLACEHOLDER_PREFIX)


class ConfigError(Exception):
    """配置缺失或不合法。UI 层捕获它并决定怎么告诉用户。"""


@dataclass(frozen=True)
class AppConfig:
    api_key: str
    base_url: str
    model: str
    max_history_chars: int = 40000
    max_tool_rounds: int = 8
    max_read_chars: int = 20000
    type_delay: float = 0.03
    config_path: str = ""
    # 搜索是可选的：没配不影响对话，只是 searchWe 这个工具会返回"没配置"
    search_api_key: str = ""
    search_model: str = DEFAULT_SEARCH_MODEL
    search_timeout: float = DEFAULT_SEARCH_TIMEOUT
    # 抓网页：超时 + 单页正文返回上限（上限直接决定这次抓取吃掉多少上下文）
    fetch_timeout: float = DEFAULT_FETCH_TIMEOUT
    fetch_max_chars: int = DEFAULT_FETCH_MAX_CHARS
    # 本地命令：超时 + stdout/stderr 各自的上限
    cli_timeout: float = DEFAULT_CLI_TIMEOUT
    cli_max_chars: int = DEFAULT_CLI_MAX_CHARS
    # MCP 远端工具：连接握手、单次调用、返回文本上限
    mcp_connect_timeout: float = DEFAULT_MCP_CONNECT_TIMEOUT
    mcp_call_timeout: float = DEFAULT_MCP_CALL_TIMEOUT
    mcp_max_chars: int = DEFAULT_MCP_MAX_CHARS
    mcp_desc_chars: int = 500          # 注册时给模型看的单条工具描述上限
    # 上下文窗口与告警线（窗口没配时用 128k，界面标「默认」）
    context_window: int = DEFAULT_CONTEXT_WINDOW
    context_window_is_default: bool = True
    context_warn_ratio: float = DEFAULT_CONTEXT_WARN_RATIO
    agents_md_max_chars: int = DEFAULT_AGENTS_MD_MAX_CHARS
    # TUI 节奏参数（打字机 type_delay 在上面，其余都在这里）
    ui_poll_interval: float = DEFAULT_POLL_INTERVAL
    ui_fast_forward_chars: int = DEFAULT_FAST_FORWARD_CHARS
    ui_fast_forward_divisor: int = DEFAULT_FAST_FORWARD_DIVISOR
    ui_drain_min_step: int = DEFAULT_DRAIN_MIN_STEP
    ui_drain_divisor: int = DEFAULT_DRAIN_DIVISOR

    def masked_key(self) -> str:
        """给界面展示用：只露头尾，别把 key 打在屏幕上。"""
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return self.api_key[:4] + "*" * (len(self.api_key) - 8) + self.api_key[-4:]

    def masked_search_key(self) -> str:
        """搜索 key 的脱敏版本，规则同上。"""
        if not self.search_api_key:
            return "（未配置）"
        if len(self.search_api_key) <= 12:
            return "*" * len(self.search_api_key)
        return self.search_api_key[:8] + "*" * (len(self.search_api_key) - 12) \
            + self.search_api_key[-4:]

    @property
    def search_ready(self) -> bool:
        return bool(self.search_api_key)



def default_config_path() -> str:
    """默认配置路径：<仓库根>/config.ini（core 在 <根>/xiaoA/core/ 下）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    return os.path.join(root, "config.ini")


def load_config(path: Optional[str] = None) -> AppConfig:
    """读配置。成功返回 AppConfig，失败抛 ConfigError。"""
    path = path or default_config_path()

    if not os.path.exists(path):
        raise ConfigError(
            "找不到配置文件：%s\n"
            "请把 config.example.ini 复制成 config.ini，并填入自己的 API Key。" % path
        )

    cfg = configparser.ConfigParser()
    # 保留选项名大小写，避免 api_key 被转成小写后读不到
    cfg.optionxform = str
    try:
        cfg.read(path, encoding="utf-8")
    except configparser.Error as e:
        raise ConfigError("配置文件格式有误：%s" % e) from e

    if not cfg.has_section(REQUIRED_SECTION):
        raise ConfigError(
            "配置文件里缺少 [%s] 段：%s" % (REQUIRED_SECTION, path)
        )

    def get(key: str, fallback: str = "") -> str:
        return (cfg.get(REQUIRED_SECTION, key, fallback=fallback) or "").strip()

    def search_get(key: str, fallback: str = "") -> str:
        """先看 [search]，再看 [deepseek]。

        为什么带这个回退：老配置里有人把 baidu_qianfan_api_key 直接写进了
        [deepseek] 段。段名对不对不该让整个功能失效，这里两种写法都认。
        """
        for section in (SEARCH_SECTION, REQUIRED_SECTION):
            if not cfg.has_section(section) or not cfg.has_option(section, key):
                continue
            value = (cfg.get(section, key, fallback="") or "").strip()
            if value:
                return value
        return fallback

    try:
        raw_keys = int(get("max_history_chars") or 40000)
        raw_rounds = int(get("max_tool_rounds") or 8)
        raw_read = int(get("max_read_chars") or 20000)
        raw_delay = float(get("type_delay") or 0.03)
    except ValueError as e:
        raise ConfigError("配置里的数值项格式不对：%s" % e)

    api_key = get("api_key")
    if not api_key or _is_placeholder(api_key):
        raise ConfigError("配置里的 api_key 还没填：%s" % path)

    # ---- 搜索是可选功能：这里只负责"读到什么就是什么"，不因为缺失而报错。
    # "没配 key" 这句话更适合由 searchWe 工具在执行时告诉模型和用户。
    search_key = search_get("baidu_qianfan_api_key")
    if not search_key and cfg.has_section(SEARCH_SECTION):
        # [search] 段里叫 api_key 也认（但绝不从 [deepseek] 段借 api_key，
        # 那是对话模型的 key，拿它去搜百度只会 401）
        search_key = (cfg.get(SEARCH_SECTION, "api_key", fallback="") or "").strip()
    if _is_placeholder(search_key):
        search_key = ""

    # 搜索模型只从 [search] 读：绝不能回退到 [deepseek] 的 model，
    # 那是个 deepseek 聊天模型名，拿去调百度搜索会被判 invalid_model。
    search_model = ""
    if cfg.has_section(SEARCH_SECTION):
        search_model = (cfg.get(SEARCH_SECTION, "model", fallback="") or "").strip()
    search_model = search_model or DEFAULT_SEARCH_MODEL

    search_timeout = DEFAULT_SEARCH_TIMEOUT
    raw_timeout = search_get("timeout")
    if raw_timeout:
        try:
            search_timeout = float(raw_timeout)
        except ValueError:
            raise ConfigError("配置里的 [%s] timeout 不是数字：%s" % (SEARCH_SECTION, raw_timeout))

    # ---- 抓网页同样可选，不填就走默认值
    fetch_timeout = DEFAULT_FETCH_TIMEOUT
    fetch_max_chars = DEFAULT_FETCH_MAX_CHARS
    if cfg.has_section(FETCH_SECTION):
        raw = (cfg.get(FETCH_SECTION, "timeout", fallback="") or "").strip()
        if raw:
            try:
                fetch_timeout = float(raw)
            except ValueError:
                raise ConfigError("配置里的 [%s] timeout 不是数字：%s" % (FETCH_SECTION, raw))
        raw = (cfg.get(FETCH_SECTION, "max_chars", fallback="") or "").strip()
        if raw:
            try:
                fetch_max_chars = max(1000, int(raw))
            except ValueError:
                raise ConfigError("配置里的 [%s] max_chars 不是整数：%s" % (FETCH_SECTION, raw))

    base_url = get("base_url") or DEFAULT_BASE_URL
    model = get("model") or DEFAULT_MODEL

    # ---- 本地命令同样可选，不填走默认值
    cli_timeout = DEFAULT_CLI_TIMEOUT
    cli_max_chars = DEFAULT_CLI_MAX_CHARS
    if cfg.has_section(CLI_SECTION):
        raw = (cfg.get(CLI_SECTION, "timeout", fallback="") or "").strip()
        if raw:
            try:
                cli_timeout = float(raw)
            except ValueError:
                raise ConfigError("配置里的 [%s] timeout 不是数字：%s" % (CLI_SECTION, raw))
        raw = (cfg.get(CLI_SECTION, "max_chars", fallback="") or "").strip()
        if raw:
            try:
                cli_max_chars = max(200, int(raw))
            except ValueError:
                raise ConfigError("配置里的 [%s] max_chars 不是整数：%s" % (CLI_SECTION, raw))

    # ---- MCP 同样可选：[mcp] 段缺失时全部走默认值
    mcp_connect_timeout = DEFAULT_MCP_CONNECT_TIMEOUT
    mcp_call_timeout = DEFAULT_MCP_CALL_TIMEOUT
    mcp_max_chars = DEFAULT_MCP_MAX_CHARS
    if cfg.has_section(MCP_SECTION):
        raw = (cfg.get(MCP_SECTION, "connect_timeout", fallback="") or "").strip()
        if raw:
            try:
                mcp_connect_timeout = float(raw)
            except ValueError:
                raise ConfigError("配置里的 [%s] connect_timeout 不是数字：%s" % (MCP_SECTION, raw))
        raw = (cfg.get(MCP_SECTION, "call_timeout", fallback="") or "").strip()
        if raw:
            try:
                mcp_call_timeout = float(raw)
            except ValueError:
                raise ConfigError("配置里的 [%s] call_timeout 不是数字：%s" % (MCP_SECTION, raw))
        raw = (cfg.get(MCP_SECTION, "max_chars", fallback="") or "").strip()
        if raw:
            try:
                mcp_max_chars = max(1000, int(raw))
            except ValueError:
                raise ConfigError("配置里的 [%s] max_chars 不是整数：%s" % (MCP_SECTION, raw))
    mcp_desc_chars = 500
    if cfg.has_section(MCP_SECTION):
        raw = (cfg.get(MCP_SECTION, "desc_chars", fallback="") or "").strip()
        if raw:
            try:
                mcp_desc_chars = max(50, int(raw))
            except ValueError:
                raise ConfigError("配置里的 [%s] desc_chars 不是整数：%s" % (MCP_SECTION, raw))

    # ---- 上下文窗口：可选。没写就用 128k，并记住"这是默认值"以便界面标注
    context_window = DEFAULT_CONTEXT_WINDOW
    context_window_is_default = True
    context_warn_ratio = DEFAULT_CONTEXT_WARN_RATIO
    agents_md_max_chars = DEFAULT_AGENTS_MD_MAX_CHARS
    if cfg.has_section(CONTEXT_SECTION):
        raw = (cfg.get(CONTEXT_SECTION, "window", fallback="") or "").strip()
        if raw:
            try:
                context_window = max(1000, int(raw))
                context_window_is_default = False
            except ValueError:
                raise ConfigError("配置里的 [%s] window 不是整数：%s" % (CONTEXT_SECTION, raw))
        raw = (cfg.get(CONTEXT_SECTION, "warn_ratio", fallback="") or "").strip()
        if raw:
            try:
                context_warn_ratio = float(raw)
            except ValueError:
                raise ConfigError("配置里的 [%s] warn_ratio 不是数字：%s" % (CONTEXT_SECTION, raw))
            if not 0.1 <= context_warn_ratio <= 0.95:
                raise ConfigError("配置里的 [%s] warn_ratio 应在 0.1~0.95 之间：%s"
                                  % (CONTEXT_SECTION, raw))
        raw = (cfg.get(CONTEXT_SECTION, "agents_md_max_chars", fallback="") or "").strip()
        if raw:
            try:
                agents_md_max_chars = max(500, int(raw))
            except ValueError:
                raise ConfigError("配置里的 [%s] agents_md_max_chars 不是整数：%s"
                                  % (CONTEXT_SECTION, raw))

    # ---- TUI 节奏参数：全部可选，缺失走默认值
    ui_poll_interval = DEFAULT_POLL_INTERVAL
    ui_fast_forward_chars = DEFAULT_FAST_FORWARD_CHARS
    ui_fast_forward_divisor = DEFAULT_FAST_FORWARD_DIVISOR
    ui_drain_min_step = DEFAULT_DRAIN_MIN_STEP
    ui_drain_divisor = DEFAULT_DRAIN_DIVISOR
    if cfg.has_section(UI_SECTION):
        def ui_float(key, fallback):
            raw = (cfg.get(UI_SECTION, key, fallback="") or "").strip()
            if not raw:
                return fallback
            try:
                return float(raw)
            except ValueError:
                raise ConfigError("配置里的 [%s] %s 不是数字：%s" % (UI_SECTION, key, raw))

        def ui_int(key, fallback, minimum=1):
            raw = (cfg.get(UI_SECTION, key, fallback="") or "").strip()
            if not raw:
                return fallback
            try:
                return max(minimum, int(raw))
            except ValueError:
                raise ConfigError("配置里的 [%s] %s 不是整数：%s" % (UI_SECTION, key, raw))

        ui_poll_interval = min(1.0, max(0.01, ui_float("poll_interval", ui_poll_interval)))
        ui_fast_forward_chars = ui_int("fast_forward_chars", ui_fast_forward_chars)
        ui_fast_forward_divisor = ui_int("fast_forward_divisor", ui_fast_forward_divisor)
        ui_drain_min_step = ui_int("drain_min_step", ui_drain_min_step)
        ui_drain_divisor = ui_int("drain_divisor", ui_drain_divisor)

    return AppConfig(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
        max_history_chars=raw_keys,
        max_tool_rounds=raw_rounds,
        max_read_chars=raw_read,
        type_delay=raw_delay,
        config_path=path,
        search_api_key=search_key,
        search_model=search_model,
        search_timeout=search_timeout,
        fetch_timeout=fetch_timeout,
        fetch_max_chars=fetch_max_chars,
        cli_timeout=cli_timeout,
        cli_max_chars=cli_max_chars,
        mcp_connect_timeout=mcp_connect_timeout,
        mcp_call_timeout=mcp_call_timeout,
        mcp_max_chars=mcp_max_chars,
        context_window=context_window,
        context_window_is_default=context_window_is_default,
        context_warn_ratio=context_warn_ratio,
        agents_md_max_chars=agents_md_max_chars,
        mcp_desc_chars=mcp_desc_chars,
        ui_poll_interval=ui_poll_interval,
        ui_fast_forward_chars=ui_fast_forward_chars,
        ui_fast_forward_divisor=ui_fast_forward_divisor,
        ui_drain_min_step=ui_drain_min_step,
        ui_drain_divisor=ui_drain_divisor,
    )


# --------------------------------------------------------------------- 配置发现

def resolve_config_path(explicit: Optional[str] = None) -> Optional[str]:
    """按优先级找一个已存在的配置文件，找不到返回 None。

    顺序：命令行 -c 指定的路径 → 启动目录下的 config.ini → 仓库根的 config.ini。
    仓库根那一级是给开发者的兜底：从别的目录（比如技能工作区）启动时，
    项目自己的 config.ini 仍然生效，不需要每台机器拷来拷去。
    """
    if explicit:
        return explicit
    cwd_candidate = os.path.join(os.getcwd(), "config.ini")
    if os.path.exists(cwd_candidate):
        return cwd_candidate
    fallback = default_config_path()
    if os.path.exists(fallback):
        return fallback
    return None


MOCK_CONFIG_TEMPLATE = """\
# xiaoA 配置文件（自动生成的初始模板）
# ---------------------------------------------------------------
# 现在里面的都是占位信息，程序还不能正常工作。
# 把 api_key 换成你自己的 Key，然后重启 xiaoA 即可。
# 各项参数的含义见 config.example.ini 或 README。
# ---------------------------------------------------------------

[deepseek]
# 对话模型的 API Key（必填）。DeepSeek 开放平台：https://platform.deepseek.com
api_key = sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# 接口地址（OpenAI 兼容即可），换别家模型时改这里
base_url = https://api.deepseek.com
model = deepseek-chat

# 一次提问最多连续调几轮工具（防止模型死循环）
max_tool_rounds = 8

# 单次 readFile 最多返回多少字符
max_read_chars = 20000

# 打字机速度：每个字之间隔多少秒。0.03 ≈ 33 字/秒；0 = 关掉打字机
type_delay = 0.03


# ---- 联网搜索（searchWe 工具，可选）--------------------------------
[search]
# 百度千帆 API Key，形态类似 bce-v3/ALTAK-xxx/yyy。不填则 searchWe 不可用
baidu_qianfan_api_key =
model = ernie-4.5-turbo-128k
timeout = 30


# ---- 抓网页（FetchWeb 工具，可选）----------------------------------
[fetch]
timeout = 30
max_chars = 12000


# ---- 跑本地命令（RunCli 工具，可选）--------------------------------
[cli]
timeout = 30
max_chars = 4000


# ---- MCP 远端工具（配合启动目录的 .mcp.json，可选）------------------
[mcp]
connect_timeout = 10
call_timeout = 60
max_chars = 8000
desc_chars = 500


# ---- 上下文（/compact 与占用显示，可选）----------------------------
[context]
# 模型上下文窗口（tokens）。不确定就保持 128000，状态栏会标「默认」
window = 128000
# 占用到这个比例就警告并暂停提问，直到 /compact 完成
warn_ratio = 0.70
# 项目 AGENTS.md 注入会话时的字符上限
agents_md_max_chars = 8000


# ---- 界面节奏（可选，一般不用动）------------------------------------
[ui]
# 事件队列轮询间隔（秒），0.04 ≈ 25fps
poll_interval = 0.04
# 打字机追帧参数：模型吐字积压超过多少字开始加速
fast_forward_chars = 40
# 追帧时每帧吃掉积压的几分之一
fast_forward_divisor = 5
# 收尾阶段每帧至少放几个字
drain_min_step = 3
# 收尾阶段每帧吃掉积压的几分之一
drain_divisor = 8
"""


def write_mock_config(path: str) -> str:
    """在 path 写一份占位配置。返回写入的路径；失败抛 OSError 由界面接住。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(MOCK_CONFIG_TEMPLATE)
    return path
