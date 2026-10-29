# -*- coding: utf-8 -*-
"""Skill 发现与注册（本阶段只做这两件事）。

约定（对齐 Agent Skills 规范的最小子集）：
  - 以"程序启动时的工作目录"为项目根目录，找 .agents/skills/ 下的各个子目录；
  - 每个 Skill 子目录至少要有一个 SKILL.md；
  - SKILL.md 开头是 YAML Frontmatter，必需字段 name 和 description；
  - 初始化阶段只解析这两个字段，SKILL.md 正文一个字都不读。

为什么初始化不读正文：
  技能正文可能很长，全部塞进上下文等于让模型背着几百 KB 的说明书干活。
  正确的姿势是"先知道有什么，需要时再取"——这正是本模块只登记元信息的原因。

设计要点：
  - 纯标准库：frontmatter 只需要 "key: value" 这一层 YAML，不值得为它引入 pyyaml；
  - 发现失败绝不炸程序：坏的技能目录收集进 problems，剩下的照常注册；
  - 本模块不知道 Session、不知道工具，只管"扫目录、读元信息"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional

# 目录布局常量，改布局只动这里
SKILLS_DIRNAME = ".agents"
SKILLS_SUBDIR = "skills"
SKILL_FILENAME = "SKILL.md"

# "程序启动时的工作目录"。在 import 本模块的那一刻抓取——
# 那时进程还没换过目录，正是题目要求的"程序启动时的工作目录"。
# 后面任何地方 os.getcwd() 都可能变，不能拿来当项目根。
STARTUP_CWD = os.getcwd()


@dataclass(frozen=True)
class SkillMeta:
    """一个已注册技能的元信息。只有注册必需的字段，正文不在其中。"""

    name: str
    description: str
    path: str          # SKILL.md 的绝对路径——留给将来"按需加载正文"用
    dirname: str       # 技能所在的子目录名（规范要求与 name 一致，这里只是记录）


class SkillCatalog:
    """扫描结果：注册成功的技能 + 每个坏目录的落选原因。

    problems 不是异常，是"发现报告"——技能装了一半也能用，
    哪个没装上、为什么没装上，要能一句话说清楚。
    """

    def __init__(self) -> None:
        self.skills: List[SkillMeta] = []
        self.problems: List[str] = []
        self.root: str = ""              # 实际扫描的 .agents/skills 目录；空串=没找到

    def __len__(self) -> int:
        return len(self.skills)

    def __bool__(self) -> bool:
        return bool(self.skills)

    def names(self) -> List[str]:
        return [s.name for s in self.skills]

    def get(self, name: str) -> Optional[SkillMeta]:
        for s in self.skills:
            if s.name == name:
                return s
        return None

    def summary(self) -> str:
        """给 --check 和界面看的一句话摘要。"""
        if not self.skills:
            return "技能：未发现（%s）" % (self.root or "没有 .agents/skills 目录")
        return "技能（%d）：%s" % (len(self.skills), "、".join(self.names()))


def parse_frontmatter(text: str) -> Dict[str, str]:
    """解析 SKILL.md 开头的 YAML Frontmatter，返回扁平的 key -> value。

    只支持规范里需要的最小子集：
      ---                  ← 第一行（允许 BOM 和空行在前）
      name: xxx
      description: yyy     ← 值里可以带冒号；可以带单/双引号
      ---
    解析失败抛 ValueError，原因写得能直接指给人看。
    """
    # 去掉 UTF-8 BOM（\ufeff）；统一换行，CRLF 的文件也能按行切
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    # 跳过文件开头的空行，找到第一条 ---
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].strip() != "---":
        raise ValueError("文件没有以 --- 开头的 YAML Frontmatter")
    i += 1

    fields: Dict[str, str] = {}
    closed = False
    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            closed = True
            break
        stripped = line.strip()
        # 空行、注释、缩进行（别的键下面的列表项）都不参与
        if not stripped or stripped.startswith("#") or line[:1] in (" ", "\t"):
            i += 1
            continue
        if ":" not in stripped:
            raise ValueError("Frontmatter 第 %d 行不是「key: value」格式：%s" % (i + 1, stripped))
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        # 成对引号是 YAML 的合法写法，剥掉，避免 description 两边挂着引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key in fields:
            raise ValueError("Frontmatter 里 %s 出现了两次" % key)
        fields[key] = value
        i += 1

    if not closed:
        raise ValueError("Frontmatter 没有闭合（只有开头的 ---，没有结尾的 ---）")
    return fields


def _scan_one_skill(skill_dir: str, catalog: SkillCatalog) -> None:
    """检查单个技能子目录：有 SKILL.md、frontmatter 合法，才注册。"""
    dirname = os.path.basename(skill_dir)
    md_path = os.path.join(skill_dir, SKILL_FILENAME)

    if not os.path.isfile(md_path):
        catalog.problems.append(
            "%s：目录里没有 SKILL.md（每个技能子目录至少要有一个）" % dirname)
        return

    try:
        # 只读元信息需要的部分：先只取前 64 KB，frontmatter 超过这个尺寸的
        # 技能说明本身就是问题；正文再长也不多读一个字节。
        with open(md_path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(64 * 1024)
    except OSError as e:
        catalog.problems.append("%s：SKILL.md 读不了（%s）" % (dirname, e))
        return

    try:
        fields = parse_frontmatter(head)
    except ValueError as e:
        catalog.problems.append("%s：%s" % (dirname, e))
        return

    name = fields.get("name", "").strip()
    description = fields.get("description", "").strip()
    if not name:
        catalog.problems.append("%s：Frontmatter 缺少必需字段 name" % dirname)
        return
    if not description:
        catalog.problems.append("%s：Frontmatter 缺少必需字段 description" % dirname)
        return

    # 规范要求 name 与目录名一致。不一致不拦（有的仓库确实目录带前缀），
    # 但要留一条问题记录，别让作者蒙在鼓里。
    if name != dirname:
        catalog.problems.append(
            "%s：提醒——Frontmatter 的 name 是 %r，和目录名不一致（仍已注册，调用时用 name）"
            % (dirname, name))

    # 重名：先注册的生效（按目录名排序保证结果稳定），后来的记进 problems
    if catalog.get(name) is not None:
        catalog.problems.append("%s：技能名 %r 已被其他目录注册，本目录被跳过" % (dirname, name))
        return

    catalog.skills.append(SkillMeta(
        name=name,
        description=description,
        path=md_path,
        dirname=dirname,
    ))


def discover_skills(root_dir: Optional[str] = None) -> SkillCatalog:
    """扫描 <root>/.agents/skills/ 下的技能子目录，返回注册结果。

    root_dir 缺省用"程序启动时的工作目录"。任何一层找不到、打不开，
    都返回一个空 catalog（root 为空串），绝不抛异常——技能缺失是常态，
    不该让整个 Agent 起不来。
    """
    catalog = SkillCatalog()
    base = root_dir if root_dir is not None else STARTUP_CWD
    skills_root = os.path.join(base, SKILLS_DIRNAME, SKILLS_SUBDIR)

    if not os.path.isdir(skills_root):
        return catalog
    catalog.root = skills_root

    try:
        entries = sorted(os.listdir(skills_root))
    except OSError as e:
        catalog.problems.append("技能目录读不了（%s）" % e)
        return catalog

    for entry in entries:
        skill_dir = os.path.join(skills_root, entry)
        # 只认目录；散落在 skills/ 根下的文件不属于任何技能
        if os.path.isdir(skill_dir):
            _scan_one_skill(skill_dir, catalog)

    return catalog


__all__ = [
    "SKILL_FILENAME",
    "SKILLS_DIRNAME",
    "SKILLS_SUBDIR",
    "STARTUP_CWD",
    "SkillCatalog",
    "SkillMeta",
    "discover_skills",
    "parse_frontmatter",
]
