# -*- coding: utf-8 -*-
"""内置工具集合。

按领域拆成小文件，避免出现一个 800 行的巨型模块：
    fs_guard.py   路径解析与安全防线（其余工具共用）
    datetime_ops  getDate
    math_ops      isLeapYear
    fs_ops.py     10 个文件操作
    cli_ops       RunCli（调用本机任意命令行程序）
    web_search    searchWe（联网搜索，走百度千帆）
    fetch_web     FetchWeb（抓网页正文，转 Markdown，走本机 curl）
    skill_ops     loadSkill（按需加载技能的 SKILL.md 全文）
新增一批工具（比如数据库）就再加一个文件 + 在这里 import。
"""

from . import cli_ops, datetime_ops, fetch_web, fs_ops, math_ops, skill_ops, web_search

# 每个模块导出的 ALL_TOOLS 会被逐个收集
_MODULES = (datetime_ops, math_ops, fs_ops, cli_ops, web_search, fetch_web, skill_ops)

ALL_TOOLS = [t for module in _MODULES for t in module.ALL_TOOLS]

__all__ = ["ALL_TOOLS"]
