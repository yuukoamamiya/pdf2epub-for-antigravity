# pdf2epub terminology

## Document references

- **脚注（footnote）**：印在页面底部、由正文中的上标或编号标记指向的说明文字。跨页时，续文仍属于同一个脚注。
- **引用（citation）**：正文中的作者—年份、方括号编号、括号编号、引文来源或其他学术指涉。引用是正文语义的一部分，不因位于页面底部就变成脚注。
- **参考文献（bibliography）**：书末或章节末的文献表条目。它保留在原有文献表结构中，不搬入脚注流。
- **章末注（chapter-end footnote）**：工作流中的输出形态：脚注正文从页面流中抽离，按实际 TOC 单元的顺序集中到该单元最后，并由 Markdown 脚注引用连接；不同 TOC 单元可以重新使用相同的脚注编号。

## Structural rule

页面位置只能产生脚注候选，不能单独证明一个编号块是脚注。只有明确判定为 `footnote_start`、`footnote_continuation` 或 `footnote_definition` 的块才能被归一化为章末注；`citation`、`bibliography`、`body` 和未解决的块保持原位。

## Translation handoff

- **章节组（chapter group）**：按顶层 TOC 组织的语义范围，包含该章节及其拆分单元；术语上下文默认以章节组为边界。
- **worker 批次（worker batch）**：交给同一个工作区 Subagent 的相邻章节组集合。它是执行范围，不改变章节组之间的目录和术语边界。
