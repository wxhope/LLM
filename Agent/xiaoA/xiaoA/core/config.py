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

REQUIRED_SECTION = "deepseek"
SEARCH_SECTION = "search"
FETCH_SECTION = "fetch"
CLI_SECTION = "cli"

# 占位符：填这几个等于没填
_PLACEHOLDERS = ("你的key", "你的百度千帆key", "your_api_key", "xxx")


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
    if not api_key or api_key in _PLACEHOLDERS:
        raise ConfigError("配置里的 api_key 还没填：%s" % path)

    # ---- 搜索是可选功能：这里只负责"读到什么就是什么"，不因为缺失而报错。
    # "没配 key" 这句话更适合由 searchWe 工具在执行时告诉模型和用户。
    search_key = search_get("baidu_qianfan_api_key")
    if not search_key and cfg.has_section(SEARCH_SECTION):
        # [search] 段里叫 api_key 也认（但绝不从 [deepseek] 段借 api_key，
        # 那是对话模型的 key，拿它去搜百度只会 401）
        search_key = (cfg.get(SEARCH_SECTION, "api_key", fallback="") or "").strip()
    if search_key in _PLACEHOLDERS:
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
    )
