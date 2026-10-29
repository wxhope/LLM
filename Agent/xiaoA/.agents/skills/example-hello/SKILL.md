---
name: example-hello
description: 示例技能——演示 .agents/skills 的目录布局与 SKILL.md 写法，确认能被 xiaoA 正常发现后即可删除。
---

# 示例技能

这个目录演示一个技能的最小结构：

```
.agents/
  skills/
    example-hello/     ← 目录名建议与 frontmatter 的 name 一致
      SKILL.md         ← 必需；开头是 YAML Frontmatter
```

Frontmatter 只有 name 和 description 两个必需字段：

- name：技能名，模型按这个名字引用它
- description：一句话说清这个技能是干什么的——这是 xiaoA 初始化时唯一会读的正文之外的内容

初始化阶段 xiaoA 只登记这两个字段，下面的正文不读。
把本目录删掉，就恢复"没有技能"的状态。
