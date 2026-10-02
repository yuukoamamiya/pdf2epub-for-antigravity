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
  batching:
    # Optional PDF whole-book TOC orientation budget; the exact chapter TOC
    # contract remains separate and authoritative.
    global_toc_tokens: 1200
  # 可选：覆盖某个具体任务
  # task_models:
  #   refine: <configured task model>
```

默认规则是：正文、元数据、目录、小说和 TeX 翻译使用配置中的 `translation`；结构分析、OCR 润色和实体提取使用 `default`。每个生成的 `*_subagent_manifest.json` 和提示词都会明确写出推荐模型，供 Antigravity 中的 Subagent 选择。具体模型版本以当前配置文件为准，本文档不固定版本号。这里是任务合同，不是 Python 对模型 API 的调用或强制切换。

## 额度耗尽与断点续传

正文任务按文件拆分。使用 `--resume` 重新准备任务时，manifest 会根据目标目录写出 `completed_files` 和 `pending_files`；提示词要求 Subagent 只处理 `pending_files`。已经通过校验的输出不会被重新覆盖。新 manifest 还会按单元比较 `unit_context_sha256` 和精确 TOC 上下文，因此外部术语表变化只会使受影响单元重新进入 `pending`；没有单元哈希的旧 manifest 才采用整批重做的兼容策略。恢复前建议先运行对应的 `*-validate`，这样可以先发现空文件、行数不一致或标签损坏。

PDF 的 `translate` manifest 会在 `worker_handoffs/` 按顶层章节生成隔离的最小 manifest 和提示词；父级 manifest 保留完整审计索引，worker 只接收自己的文件、批次、上下文哈希和必要的章节元数据，不重复携带全书文件统计、章节映射或审计快照路径；
`polish` 使用独立的 `polish_worker_handoffs/`，避免不同阶段的任务被误认。每个 Subagent
只读取自己 handoff 的 `assigned_files`；超过 30,000 字节的单元自动独立成批，但不能与
其他章节合并。章节术语上下文在普通章节中只注入一次；大章节拆分时使用
`shared_entries` 和当前分片的 `local_entries`。`translate-toc` 是正文 worker 启动前的
独立任务，不由任何正文 worker 写入。

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
  跨语言资料应配置在 `translation.reference_glossaries`；它们只作为只读概念参考，
  不参与正式术语优先级，也不能覆盖 `translation.glossaries`。

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
ocr-pages →（若启用第二套 OCR：ocr-correct → ocr-correct-validate）→ refine-prepare → Subagent → refine-local → polish → polish-validate
```

OCR 完成不是“有几个 `page_*.md` 文件”就算通过。`ocr_progress.json` 会记录源 PDF
哈希、真实物理页数、已处理页、失败页、缺失页和空结果页；失败、缺页或未确认的空结果
会使 `ocr-pages` 返回非零，并阻断 `refine-prepare`、`refine-local` 和后续 readiness
检查。网络/服务失败可直接使用 `ocr-pages --resume` 重试；如果某页虽然已标记完成但
需要重新识别，可使用 `ocr-pages --resume --retry-pages 12,15`。确认某页确实是物理空白页
后，才可使用 `--allow-empty-pages` 显式放行，并保留该决定在 checkpoint 中。修复 OCR
后运行 `refine-local --resume`，页面指纹变化会使下游 polish/翻译 checkpoint 重新进入
待处理状态。

只有 `ocr.secondary.enabled: true` 时才执行 `ocr-correct`。本地程序会从原始 PDF 渲染一张
与每个物理页对应的审阅图，并生成 `ocr-correct_worker_handoffs/`；工作区 Subagent 只能处理
自己 manifest 的 `assigned_files`，对照同名页图修正有直接视觉证据的识别错误，结果写入
`ocr_corrected_pages/`，并为每页写入 `ocr_correction_reviews/page_NNN.json`。随后运行
`ocr-correct-validate`，它会检查 UTF-8、输出完整性、逐页审阅记录、行数不减少、Markdown
结构标记和原始页哈希，并将通过的结果暂存到 `ocr_corrected_pages/validated/`。原始 `pages/`
永不覆盖；纠错结果缺失、过期或未通过校验时，`refine-prepare` 和 `refine-local` 会阻断。
第二套 OCR 关闭时直接使用主 OCR 的 `pages/`，不生成“已纠错”检查点；高置信度原生文字 PDF
同样不适用该阶段。

可选的双 OCR 配置可以在 `ocr-pages` 阶段启用本地 PaddleOCR；开关同时决定是否进入视觉
Subagent 纠错阶段：

```yaml
ocr:
  backend: chandra
  secondary:
    enabled: true
    backend: paddle
  backends:
    paddle:
      lang: en

ocr_correction:
  review_dpi: 150
```

本地依赖可用 `uv sync --extra ocr-local` 安装；PaddleOCR 的语言模型必须与原书语言匹配。
项目将 `albumentations` 固定在 1.4.x（`<2.0.0`），因为 Windows 下 2.x 会在导入阶段
主动加载 PyTorch，可能触发与 PaddlePaddle 冲突的 DLL 加载错误。不要通过预加载 `torch`
来绕过该问题；若本地依赖安装不完整，应重新运行上述同步命令。

安装本地引擎后，`ocr-pages` 会把主 OCR 和 PaddleOCR 的结果按页做规范化比较，忽略纯粹的
Markdown 换行/标记差异，但保留字符、数字、标点和缺行差异。报告写入 `ocr_consensus.json`；
没有实质差异的页面直接自动接受，只有 `action: visual_review` 的页面才进入
`ocr-correct_worker_handoffs/`。双 OCR 只是筛查，不把任一 OCR 结果当作绝对真值；两个引擎
共同犯错由两层机制补充拦截：默认每 20 页抽查一页，并把内部文本密度显著低于相邻页的页面
标记为风险页。这些信号只扩大视觉复核范围，不自动修改文本；它们仍不能证明两个 OCR 没有
共同犯错。`ocr.secondary.enabled: false` 时不运行第二套 OCR，也不运行
视觉 OCR 纠错，主 OCR 结果直接进入后续结构整理。

翻译模式在 polish 之后继续：

```text
extract-entities → extract-entities-validate → translate-toc → translate-toc-validate
  → translate（worker_handoffs/）→ translate-validate → build-epub --translated
```

纯转换模式使用 `pipeline: epub_conversion`（兼容 `mode: ocr_to_epub`），跳过所有
翻译专属步骤：

```text
check-ready --stage package → build-epub
```

它不要求语言配置，不生成实体表或翻译 TOC，但仍必须完成 polish 和
`polish-validate`。转换配置最小形式是：

```yaml
title: "Your Book Title"
input_pdf: "input/your_book.pdf"
pipeline: epub_conversion
```

`refine-prepare` 会在 `output/<title>/` 生成 `refine_subagent_prompt.md` 和 `refine_subagent_manifest.json`。视觉 OCR PDF 中，Subagent 阅读经过校验的 `ocr_corrected_pages/validated/page_*.md`；原生文字 PDF 仍读取 `pages/page_*.md`。它只负责写入 `toc_tree.json`。随后 `refine-local`：

- 校验页码范围、层级、父子包含关系和兄弟节点重叠；
- 用本地 tokenizer 估算单元大小；
- 用 `PageMerger` 合并页面并生成 `ocr_markdown/`；
- 处理页内章节边界：`toc_tree.json` 中的 `boundary_info.start_line` 使用对应
  `page_XXX.md` 的 1-based 行号，`end_line` 是不包含该行的结束位置。若新章节从
  下一页的中部开始，上一章节会自动拥有该页标题之前的前缀；若父章节标题和首个
  子章节同页，父标题/导语会保留在首个子章节单元中，不会被前一章节吞并或丢失；
- 对超过 15,000 tokens 的 Notes、Bibliography 和 Index 单元按完整条目/段落
  自动生成 `chapter_N.partM.md` 分片，默认目标为 12,000 tokens；
- 不创建 LLM client、不发送 PDF、不消耗 API 配额。

结构 Subagent 必须区分“换页”和“页内换章”：不能因为章节标题出现在某页中部，
就把上一章节的 `end_page` 提前到上一页。若父章节与首个子章节共享起始页，必须同时
提供父节点和首个子节点的 `boundary_info.start_line`；本地校验会拒绝缺少这两个锚点的
结构结果。这样 `polish` 收到的源稿仍保持完整句子和正确章节归属，polish 只负责换行、
段落和块级结构，不会通过删除半句来“修复”错误的章节切分。

`refine-local` 完成物理切分后会运行边界注脚扫描器，将安全匹配的跨文件引用/定义记录
到 `footnote_boundary_bindings.json`；EPUB 构建时由 `FootnoteManager` 消费。原始 PDF 书签
草稿还会对明显的“大跨度目录/附录包装节点”执行保守的层级解构，Subagent 只需复核
结果。

`refine` 是 `refine-prepare` 的别名，不再存在 provider/API 实现。

## PDF 翻译的术语、注脚和目录

润色前后的 Markdown 标题标记是结构合同：Subagent 不得把普通粗体、罗马数字
或编号文字升级成 `#` 标题，只能删除确认重复的 running header。润色校验还会
安全检查 OCR 中 Notes/注释章节的 `<sup>N</sup>` 注脚迁移为 `[^N]` 和
`[^N]: ...`；数学、表格和序数上标不会按注脚处理。

启用第二套 OCR 时，PDF 正文翻译前必须按以下顺序运行（实体表尚未存在时使用第一条的
`--skip-entities`；实体表完成后再次运行不带该选项的门禁）：

```text
ocr-pages → ocr-correct → ocr-correct-validate → polish → polish-validate → check-ready --skip-entities → extract-entities →
extract-entities-validate → check-ready → translate → translate-validate
```

`polish` 在启用第二套 OCR 时只处理前置 OCR 纠错之后的换行、段落边界和块级结构；已通过
`ocr-correct-validate` 的字符、词语和符号视为权威，不再进行 OCR 字符、拼写或措辞改写。
若仍疑似有 OCR 错误，应退回 `ocr-correct`。关闭第二套 OCR 时跳过 `ocr-correct`，主 OCR
结果直接进入 polish；polish 仍只处理结构，不应静默改写 OCR 字符。对高置信度原生矢量文本 PDF，polish 用于识别视觉换行与真实
段落边界；原生文字稿也不得进行无依据的拼写或字形改写。`polish-validate` 还会对源稿和
润色稿做忽略换行、Markdown 外层标记及已确认页边装饰的内容保真比较；正文 token 或数字
标记大量丢失，或结构性空父标题下的子章节正文被重复复制时会阻断，而不是把不完整或
重复的润色稿交给后续翻译。父标题可以没有正文，但必须只保留标题本身；子章节正文只
出现一次并归属于最近的子标题。
PDF 翻译、实体提取和打包都必须以当前且通过 `polish-validate` 的
`polished_markdown/validated/` 为源稿；如果润色稿缺失、校验失败或与当前源稿不匹配，
本地门禁会拒绝继续。

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
产生冲突时，准备阶段会拒绝继续。翻译任务会先为每个源单元生成精简术语上下文，再由
worker handoff 按顶层章节聚合；普通章节使用章节级 `entries`，大章节拆分使用
`shared_entries`/`local_entries`，完整快照只保留作审计。PDF 正文 Prompt 还会读取已验证的
`toc_tree_translated.json`，生成一个方向性全书 TOC 轮廓。轮廓按实际 TOC 的深度、分支规模
和 `global_toc_tokens` 预算自适应压缩，不固定规定保留几级标题；它只帮助理解全书结构，
当前章节的精确 TOC heading contract 才能决定正文中的标题文字。

跨语言的学派资料（例如德文术语表用于英文思想史书籍）必须单独配置在
`translation.reference_glossaries`。候选报告会把它们标记为
`reference_eligible`，但不会自动选择；用户明确选择后，Subagent 只能读取其
`reference_glossary_*` 快照，用于概念对照和既有目标语定名参考，不能覆盖正式术语表，
也不能修改原始 YAML、快照或生成自动修订版。

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

pipeline 的能力矩阵由 `pdf2epub/pipeline_policy.py` 维护。命令模块应使用该策略判断
实体、翻译 TOC、正文翻译和 polish 门禁，不要各自解析 `pipeline`/`mode`。

## 安全边界

仓库不硬编码内部项目 ID，不伪造 IDE 请求头，也不自动导出或冒用 ADC 凭证。任何需要账号授权的模型调用都应由用户在 Antigravity IDE 会话中完成。
