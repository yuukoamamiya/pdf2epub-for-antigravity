# Antigravity Subagent 工作流

本项目的推荐翻译路径不从 Python 调用 Google 或 Antigravity API。需要判断和翻译的工作交给 Antigravity IDE 中的 Subagent，Python 只做本地文件处理和确定性校验。

执行时以仓库根目录的 `AGENTS.md` 为唯一规范源；本文档只提供背景说明和示例，
不应被主 Agent 当作独立的简化 SOP。尤其是正文翻译、润色、结构判断和术语提取，
必须先打开工作区 Subagent；Subagent 入口不可用时应暂停，不得由主 Agent 代做。

## Subagent 模型配置

在 `config.yaml` 或 `config_epub.yaml` 中设置：

```yaml
subagent:
  models:
    translation: <configured translation model>
    default: <configured default model>
  # 可选：覆盖某个具体任务
  # task_models:
  #   refine: <configured task model>
```

默认规则是：正文、元数据、目录、小说和 TeX 翻译使用配置中的 `translation`；结构分析、OCR 润色和实体提取使用 `default`。每个生成的 `*_subagent_manifest.json` 和提示词都会明确写出推荐模型，供 Antigravity 中的 Subagent 选择。具体模型版本以当前配置文件为准，本文档不固定版本号。这里是任务合同，不是 Python 对模型 API 的调用或强制切换。

## 额度耗尽与断点续传

正文任务按文件拆分。使用 `--resume` 重新准备任务时，manifest 会根据目标目录写出 `completed_files` 和 `pending_files`；提示词要求 Subagent 只处理 `pending_files`。已经通过校验的输出不会被重新覆盖。恢复前建议先运行对应的 `*-validate`，这样可以先发现空文件、行数不一致或标签损坏。

PDF 翻译 manifest 还会在 `batch_handoffs/` 生成按批次隔离的 manifest 和提示词。并发时每个 Subagent 只读取自己批次的 `assigned_files`；超过 30,000 字节的单元自动独立成批。只有第一个批次负责写入 `toc_tree_translated.json`。

元数据是单个 `translated_metadata.json`，必须整体是合法 JSON；如果额度中断留下半个文件，校验会拒绝它，下一次 Subagent 会完整重写。

TeX 使用独立的 `tex_units/` 和 `translated_tex_units/` 文件。校验时本地程序检查每个单元都存在，再从这些单元重建 `project/` 并编译，因此不会把初始原文工程误判为“已经翻完”。

## EPUB 高保真翻译

```text
html-prepare → Subagent(entity extraction) → html-prepare → Subagent(book_translator) → html-validate → build-html-epub
```

首次执行 `html-prepare` 后，输出目录会包含：

- `compressed_units/*.md`：正文翻译单元，每行对应一个结构单元；
- `metadata_translation_source.json`：书名、目录、简介、版权说明等元数据输入；
- `metadata_translation_prompt.md`：元数据翻译说明。
- 默认还会生成 `entity_subagent_manifest.json` 和 `entity_subagent_prompt.md`，
  用于从整本 EPUB 提取 `translation_entities.json`，以便全书统一人名、专名和术语。
- 配置中的 `translation.glossaries` 可以按书选择零个、一个或多个外部 YAML/JSON
  领域术语表；程序会把只读规范化快照放到
  `output/<title>/translation_glossaries/`，并在翻译 manifest 中锁定 SHA-256。

Subagent 需要：

1. 将每个正文单元写入 `translated_compressed/<同名>.md`；
2. 保持正文行数、HTML 标签、属性、实体和容器不变；源行没有 `<div>` 时不得添加，`<i>` 必须保持原有数量和嵌套关系；
3. 阅读 `metadata_translation_prompt.md`，在输出目录写入 `translated_metadata.json`。

首次运行 `html-prepare` 后，先执行 manifest 中的实体提取任务，再次运行
`html-prepare`。第二次生成的正文和元数据提示词会同时挂载当前书实体表和所选
外部领域术语表，并生成 `translate-html_subagent_manifest.json`。确实不需要当前
书实体表时，才使用
`html-prepare --skip-entities`；外部领域术语表仍会继续生效。

写完单个文件后，可以立即运行：

```text
html-validate --file <同名文件>.md
```

单文件模式只检查该文件，不检查元数据和全书完整性；最终打包前仍须运行不带
`--file` 的全量校验。

元数据规则：书名、目录、简介和版权说明可以翻译；作者名和出版社必须原样复制。`html-validate` 会检查元数据结构、目录顺序、链接锚点以及作者/出版社是否被修改。校验不通过时，`build-html-epub` 默认拒绝打包。

## PDF 结构精修

```text
ocr-pages → refine-prepare → Subagent → refine-local → polish → polish-validate
  → extract-entities → translate → translate-validate → build-epub
```

`refine-prepare` 会在 `output/<title>/` 生成 `refine_subagent_prompt.md` 和 `refine_subagent_manifest.json`。Subagent 阅读 `pages/page_*.md` 后，只负责写入 `toc_tree.json`。随后 `refine-local`：

- 校验页码范围、层级、父子包含关系和兄弟节点重叠；
- 用本地 tokenizer 估算单元大小；
- 用 `PageMerger` 合并页面并生成 `ocr_markdown/`；
- 对超过 15,000 tokens 的 Notes、Bibliography 和 Index 单元按完整条目/段落
  自动生成 `chapter_N.partM.md` 分片，默认目标为 12,000 tokens；
- 不创建 LLM client、不发送 PDF、不消耗 API 配额。

`refine` 是 `refine-prepare` 的别名，不再存在 provider/API 实现。

## PDF 翻译的术语、注脚和目录

润色前后的 Markdown 标题标记是结构合同：Subagent 不得把普通粗体、罗马数字
或编号文字升级成 `#` 标题，只能删除确认重复的 running header。润色校验还会
安全检查 OCR 中 Notes/注释章节的 `<sup>N</sup>` 注脚迁移为 `[^N]` 和
`[^N]: ...`；数学、表格和序数上标不会按注脚处理。

PDF 正文翻译前，必须按以下顺序运行（实体表尚未存在时使用第一条的
`--skip-entities`；实体表完成后再次运行不带该选项的门禁）：

```text
polish → polish-validate → check-ready --skip-entities → extract-entities →
extract-entities-validate → check-ready → translate → translate-validate
```

`polish` 用于修复 OCR 换行和明显 OCR 错字。PDF 翻译、实体提取和打包都必须以当前
且通过 `polish-validate` 的 `polished_markdown/validated/` 为源稿；如果润色稿缺失、
校验失败或与当前 OCR 源稿不匹配，本地门禁会拒绝继续。

`extract-entities` 读取 `translation.source_stage` 实际选中的源稿，并生成
`translation_entities.template.json` 和 `translation_entities.json` 的交接契约。
默认情况下 `translate` 要求后者存在且合法，
随后把它作为只读上下文挂载到翻译 manifest，并记录 SHA-256；词表变化后校验会
拒绝继续打包。确实不需要术语表时，使用 `translate --skip-entities`，该选择
会记录在 manifest 中并由校验器识别。

外部领域术语表通过同一配置中的 `translation.glossaries` 按书选择。它们与当前书
实体表分开管理，可以不配置，也可以同时配置多个；可先运行 `glossary-candidates`
生成候选报告，程序不会仅凭文件名自动选择。每次明确选择会记录在
`glossary_selection.json` 中；外部表中的 `fixed` 译法优先，不同外部表对同一源词
产生冲突时，准备阶段会拒绝继续。翻译任务还会为每个单元生成精简术语上下文，
完整快照保留作审计。

目录翻译保持独立的 JSON 合同，可使用：

```text
translate-toc → translate-toc-validate
```

这会保持目录树、顺序、页码、层级和元数据不变，只替换书名与章节标题。

翻译 Subagent 完成单个 PDF 单元后可以先做低成本闭环校验：

```text
translate-validate --file chapter_5.3.2.md
```

单文件结果会写入 `translate_file_validation.json`，供 `--resume` 使用；它不等同于全书校验，打包前仍须运行不带 `--file` 的完整校验。

参考文献和索引单元会额外进行离线数字标记校验，覆盖年份、版次、DOI/ISBN 片段、页码、页码范围和索引交叉引用；数字标记发生丢失、改写或重排时，校验会拒绝该单元。

Windows 下的批量替换、JSON 写入和正则处理应使用仓库已有的 UTF-8 脚本或可复用
脚本，不要拼接复杂的 PowerShell `python -c` 内联命令。

## 安全边界

仓库不硬编码内部项目 ID，不伪造 IDE 请求头，也不自动导出或冒用 ADC 凭证。任何需要账号授权的模型调用都应由用户在 Antigravity IDE 会话中完成。
