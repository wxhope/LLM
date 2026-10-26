# -*- coding: utf-8 -*-
"""计算类工具：isLeapYear。

这里体现一个原则：能用确定的代码算准的事，就不让模型"回忆"。
模型背得出闰年规则，但换个年份就可能顺口说错；代码不会。
"""

from __future__ import annotations

from ..base import DangerLevel, ToolContext, integer, params, tool
from .fs_guard import get_int


def is_leap_year(year: int) -> bool:
    """公历闰年判定。

    顺序很重要：先判 400 再判 100。
    反过来写的话，2000 会被判成平年（它是闰年）。
    """
    if year % 400 == 0:
        return True
    if year % 100 == 0:
        return False
    return year % 4 == 0


def leap_reason(year: int) -> str:
    """给模型一句解释，方便它转述给用户。"""
    if year % 400 == 0:
        return "能被 400 整除"
    if year % 100 == 0:
        return "能被 100 整除，但不能被 400 整除"
    if year % 4 == 0:
        return "能被 4 整除，且不能被 100 整除"
    return "不能被 4 整除"


@tool(
    name="isLeapYear",
    description=(
        "判断指定年份是不是闰年。"
        "当用户问某一年是不是闰年、某年二月有几天、某年有没有 2 月 29 日时调用。"
        "用户说「今年」时，先用 getDate 拿到当前年份，再调本工具。"
    ),
    parameters=params({"year": integer("要判断的年份，四位数字，例如 2024")}, ["year"]),
    danger=DangerLevel.SAFE,
    tags=["math", "calendar"],
)
def is_leap_year_tool(args: dict, ctx: ToolContext) -> str:
    year = get_int(args, "year")
    if year is None:
        return '没拿到有效的年份参数，请提供 year，例如 {"year": 2024}。'
    if not 1 <= year <= 9999:
        return "%d 不是一个合理的公历年份，请给 1 到 9999 之间的整数。" % year

    if is_leap_year(year):
        return "%d 年是闰年（%s），2 月有 29 天，全年 366 天。" % (year, leap_reason(year))
    return "%d 年不是闰年（%s），2 月有 28 天，全年 365 天。" % (year, leap_reason(year))


ALL_TOOLS = [is_leap_year_tool]

__all__ = ["ALL_TOOLS", "is_leap_year_tool", "is_leap_year", "leap_reason"]
