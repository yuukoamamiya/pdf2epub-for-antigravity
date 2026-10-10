# Antigravity Subagent 工作流

本项目的推荐翻译路径不从 Python 调用 Google 或 Antigravity API。需要判断和翻译的工作交给 Antigravity IDE 中的 Subagent，Python 只做本地文件处理和确定性校验。

执行时以仓库根目录的 `AGENTS.md` 为唯一规范源；本文档只提供背景说明和示例，
不应被主 Agent 当作独立的简化 SOP。尤其是正文翻译、润色、结构判断和术语提取，
必须先打开工作区 Subagent；Subagent 入口不可用时应暂停，不得由主 Agent 代做。

本文是维护者用的阶段合同文档：它解释每个命令生成什么、Subagent 读取什么、校验器如何
判断 freshness，以及下一阶段消费哪个目录。使用者快速了解功能和流程请看
[`README.md`](../README.md)；需要直接执行任务时只看 [`AGENTS.md`](../AGENTS.md)。

## 当前实现基线

- 默认 PDF 主 OCR 是 Chandra；可选第二套 OCR 使用配置的其他已注册后端。
- `ocr.secondary.enabled` 是单 OCR/双 OCR 的唯一开关，同时约束 OCR 纠错、脚注候选和整页插图候选。
- 高置信度矢量文字 PDF 使用 `native_text` 页面源和 PDF page-point layout sidecar；它跳过视觉
  OCR/共识，但仍必须经过 `refine-local`、脚注门禁和 polish。
- Chandra 写入完整的 layout sidecar；次 OCR 只写入独立的逐页文本和 layout sidecar，不静默
  覆盖主 OCR。
- EPUB 公式使用 Unicode 优先、MathML 回退方案；当前没有 SVG 公式渲染器，也不要求
  `dvisvgm`、XeLaTeX 或 TeX Live 参与普通 EPUB 构建。
- 任何“存在文件即可继续”的旧流程都不再成立：必须检查当前源哈希、manifest、validation
  report 和证据模式。

### OCR 后端的维护边界

当前 OCR 后端通过延迟注册保持可选依赖隔离：

| 后端 | 运行位置 | 进入主流程的接口 | 本地运行时是否随项目发布 |
|---|---|---|---|
| Chandra | 主项目 Python 进程 | `ocr/backends/chandra.py` 的 page result | 否；只发布接口和配置模板 |

兼容性页面后端 VLLM、Azure Document Intelligence 和 Google Cloud Vision 仍可作为显式主
后端使用，但不属于默认双 OCR 路径；它们的 SDK 通过可选依赖安装。配置中出现未注册或退役
的 backend 时，`doctor` 会直接失败，不能把它当作可选依赖缺失来处理。

本地凭据只能来自被 Git 忽略的凭据目录；发布源码时只包含凭据读取接口和配置模板，不包含
`.secrets/` 或 `config.yaml`。

## Subagent 模型配置

在开始长任务或恢复中断任务前，推荐先运行只读诊断：

```text
uv run pdf2epub -c config.yaml status
uv run pdf2epub -c config.yaml status --json
uv run pdf2epub -c config.yaml doctor
```

`status` 不创建或更新 checkpoint；它只读取当前 manifest、哈希和 validation report。`doctor`
还会检查配置中的 OCR backend、已选择的可选 SDK 和输入文件。

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
    pack_adjacent_chapters: true
    max_chapters_per_worker: 3
  # 可选：覆盖某个具体任务
  # task_models:
  #   refine: <configured task model>
```

默认规则是：正文、元数据、目录、小说和 TeX 翻译使用配置中的 `translation`；结构分析、OCR 润色和实体提取使用 `default`。每个生成的 `*_subagent_manifest.json` 和提示词都会明确写出推荐模型，供 Antigravity 中的 Subagent 选择。具体模型版本以当前配置文件为准，本文档不固定版本号。这里是任务合同，不是 Python 对模型 API 的调用或强制切换。

## 额度耗尽与断点续传

正文任务按文件拆分。使用 `--resume` 重新准备任务时，manifest 会根据目标目录写出 `completed_files` 和 `pending_files`；提示词要求 Subagent 只处理 `pending_files`。已经通过校验的输出不会被重新覆盖。新 manifest 还会按单元比较 `unit_context_sha256` 和精确 TOC 上下文，因此外部术语表变化只会使受影响单元重新进入 `pending`；没有单元哈希的旧 manifest 才采用整批重做的兼容策略。恢复前建议先运行对应的 `*-validate`，这样可以先发现空文件、行数不一致或标签损坏。

PDF/EPUB 的 `translate` manifest 会在 `worker_handoffs/` 按 TOC 顺序为相邻短章节装箱，生成隔离的最小 manifest 和提示词；父级 manifest 保留完整审计索引，worker 只接收自己的文件、批次、上下文哈希和必要的章节元数据，不重复携带全书文件统计、章节映射或审计快照路径；
`polish` 使用独立的 `polish_worker_handoffs/`，避免不同阶段的任务被误认。每个 Subagent
只读取自己 handoff 的 `assigned_files`；超过 30,000 字节的单元自动独立成批，但不能与
其他章节合并。普通单章节 worker 的术语上下文只注入一次；相邻章节合并 worker 使用按
文件分区的直接上下文；大章节拆分时使用 `shared_entries` 和当前分片的 `local_entries`。
`translate-toc` 是正文 worker 启动前的
独立任务，不由任何正文 worker 写入。

每个 worker handoff 还记录由任务、worker、批次和当前源哈希计算的
`assignment_sha256`，并把 `assigned_files` 作为显式授权字段。`--resume` 重建完全相同的
assignment 时只保留旧的 `assigned`/`running` lease 元数据；它是文件合同中的恢复提示，
不等于 IDE 已确认存在对应会话。实际派发前仍必须检查当前工作区 Subagent 列表，确认没有
相同 `assigned_files` 的活动任务；不同源哈希或不同分配会生成新的 assignment，不能复用旧 lease。
调度器可通过 `subagent_dispatch.py` 的 `claim_dispatch_lease()`、
`renew_dispatch_lease()` 和 `release_dispatch_lease()` 完成原子认领、续租和释放；发现
`already_active` 或 `conflict` 时必须停止派发，而不是覆盖旧 handoff。

元数据是单个 `translated_metadata.json`，必须整体是合法 JSON；如果额度中断留下半个文件，校验会拒绝它，下一次 Subagent 会完整重写。

## 章节组与 worker 装箱合同

PDF 和 EPUB 的正文翻译有两个不能混淆的范围：`chapter_groups` 是语义范围，
`worker_handoffs` 是执行范围。一个章节组可以包含一个章节的多个 Markdown/HTML 单元；
一个 worker handoff 可以包含多个相邻的短章节组，但不能因此改变源文件、TOC 或输出文件
的边界。

### 装箱规则

运行时按 `chapter_groups` 的 TOC 顺序从前往后装箱，默认受以下限制约束：

| 设置 | 作用 |
| --- | --- |
| `pack_adjacent_chapters` | 是否允许相邻顶层章节组进入同一 worker，默认 `true` |
| `max_chapters_per_worker` | 一个 worker 最多容纳的章节组数量，默认 `3` |
| `max_tiny_chapters_per_worker` | 连续 tiny 章节的更高上限，默认 `6`；只在批次全由 tiny 章节组成时生效 |
| `tiny_chapter_max_tokens` | 判定 tiny 章节的单章 token 上限，默认 `2000` |
| `max_files` | 一个 worker 的源文件数量上限，硬上限为 `8` |
| `max_source_tokens` | 一个 worker 的源 token 预算 |
| `single_file_max_bytes` | 单个文件超过该值时必须独立成批，默认 `30000` |

装箱具有以下硬边界：

1. 一个章节组如果自身需要多个批次，先结束当前短章节装箱，再按章节内部拆出的
   part 文件分别派发；这些 part 不得与其他章节组混合。
2. 一个章节组只要包含超过 `max_source_tokens` 或 `single_file_max_bytes` 的文件，
   即使运行时只生成一个 part，也视为 oversized，不能与相邻章节合并。
3. 文件数、token 数或章节数即将超出预算时，先提交当前 worker，再从下一个章节组开始
   新 worker。装箱只允许连续相邻章节，不做跨章节重排或负载均衡重排。
4. `--resume` 时，已经完成而被跳过的章节组是相邻性屏障；不能把它前后的两个待处理章节
   重新拼成一个 worker。
5. 若当前批次和新增章节都属于 tiny，章节数可放宽到 `max_tiny_chapters_per_worker`；
   新增普通章节、超大章节或拆分章节时不适用该放宽。

父级 `translate_subagent_manifest.json` 保留完整的 `chapter_groups`、`pending_files`、
`batch_queue` 和审计信息。真正交给 Subagent 的 scoped manifest 只保留当前 worker 的
`assigned_files`、文件统计、上下文哈希和必要章节元数据。单章节 worker 保留兼容字段
`chapter_id`/`chapter_file_count`；合并 worker 使用：

```json
{
  "chapter_ids": ["toc_001", "toc_002"],
  "chapter_count": 2,
  "chapter_file_counts": {"toc_001": 1, "toc_002": 2},
  "assigned_files": ["chapter_1.md", "chapter_2.md", "chapter_3.md"]
}
```

`assigned_files` 是 worker 的唯一执行授权。父级 manifest 中其他文件的存在，不代表当前
Subagent 可以读取、修改或翻译它们。

### 术语上下文格式

worker handoff 生成的 `worker_contexts/` 依据任务范围选择格式：

| 场景 | `selection` | Subagent 的使用方式 |
| --- | --- | --- |
| 普通单章节 | `chapter_sparse_direct_context` | 使用一次 `entries`，作用于该章节的 assigned 文件 |
| 单个大章节的分片 | `chapter_shared_local_direct_context` | 所有分片一致使用 `shared_entries`；只对匹配分片使用 `local_entries` |
| 相邻多章节合并 | `adjacent_chapter_direct_file_contexts` | 从 `files` 映射读取当前文件的条目，不能把条目广播到其他文件 |
| 没有章节分组的普通 Markdown 任务 | `worker_sparse_direct_file_contexts` | 从 `files` 映射读取每个文件的直接条目 |

合并章节时，同一个 Subagent 可以通过同一任务上下文保持措辞和风格连续，但术语作用域仍
按文件隔离。完整领域术语表、实体表和审计快照不是 worker 的日常输入，不得用它们替换
handoff 指定的稀疏上下文。

### 维护时的验证重点

- 调整装箱逻辑时，同时覆盖“短章合并”“预算溢出封箱”“大章内部拆分”“resume 跳过章节
  作为屏障”和“术语不串章”五类测试。
- 不要把多个源文件物理合并后再交给 Subagent；输出仍必须与源文件保持一一对应，才能让
  `translate-validate --file` 和断点恢复按单元工作。
- 如果新增 worker context schema，必须更新 `AGENTS.md` 的读取规则、此处的 schema 表和
  `tests/workflow_optimizations_test.py` 的合同测试。

TeX 使用独立的 `tex_units/` 和 `translated_tex_units/` 文件。校验时本地程序检查每个单元都存在，再从这些单元重建 `project/` 并编译，因此不会把初始原文工程误判为“已经翻完”。TeX 流程中的 XeLaTeX 只用于 `translate-arxiv-validate` 的项目编译门禁，不代表 EPUB 公式也会走 XeLaTeX。

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
打包阶段如果已有 NCX 或 nav 导航文档更新失败，也会默认拒绝生成 EPUB，并把导航文件、错误和阻断状态写入
`translation_report.json`；仅在人工检查后，才可显式使用
`build-html-epub --allow-navigation-warnings` 放行。EPUB 只提供其中一种导航格式时，另一种记录为
`not_found` 属于正常情况。最终构建测试还会通过 PyMuPDF 打开成品并检查页数、文本版心位置和像素稳定性。

### HTML handoff 的 freshness 和构建报告

实体表不是只看 manifest 中已有文件是否匹配。校验会同时比较 manifest 的源文件哈希和当前
`compressed_units/` 或其他源目录的完整文件集合；新增、删除或修改源文件都会使旧实体表
失效。正文 HTML 校验同样锁定源单元集合、源哈希和准备阶段的 `input.epub` 快照。

第二套 OCR 的配置在配置加载阶段校验：`ocr.secondary.enabled: true` 时必须有
`ocr.secondary.backend`。配置关闭第二套 OCR 时，流程直接跳过 `ocr-correct` 和
`ocr-correct-validate`，CLI help 也会明确这一前置条件。

`translation_report.json` 会保留导航更新结果和打包阻断状态。导航格式缺失和导航更新异常
必须区分处理：前者在仅有 EPUB 2 或 EPUB 3 导航时属于正常记录，后者必须先人工检查再决定
是否使用 `--allow-navigation-warnings`。放行 warning 不会替代 `html-validate` 或
`epubcheck` 门禁。

项目测试还会构造一个固定 EPUB，经过真实的最终打包路径后交给 PyMuPDF 渲染，比较页数、
文本版心和像素签名，并验证重复渲染稳定。这是本地阅读器引擎级别的回归，不等同于所有
商业阅读器和硬件设备的兼容性认证。

### PDF 源类型分支

`ocr-pages` 先写 `pdf_text_probe.json`，再按保守分类选择页面源：

```text
ocr-pages
  → pdf_text_probe.json
  → native_text: pages/*.md + pages/*.ocr.json + pages/ocr_progress.json(mode=native_text)
  → visual OCR: pages/*.md + OCR layout sidecars
```

高置信度原生 PDF 都走原生分支，包括稳定双栏或多栏版面。多栏页由提取器根据 PDF 文本
span 的横向坐标拆分，并以栏为单位生成 `column_major` 阅读顺序；报告保留
`classification: native_text`、`recommendation: use_text_layer`，并记录
`layout_mode: multi_column`。可搜索但实际内容来自整页图像的 PDF、隐藏 OCR 层和混合稿仍必须
走视觉 OCR；不能仅凭复制文字或目录中已有 Markdown 判断。

原生分支的 sidecar 是版面证据，不是 OCR 结果。单栏和多栏原生 PDF 都生成这类 sidecar；每页 sidecar 应包含
`backend: native_text`、`source_kind: native_text`、`coordinate_system: page_points`、
`page_box`、`layout_mode`、`reading_order`、`body_font_size` 和有序 `blocks[]`。block 至少保留 `bbox`、`text`、
`font_size`、`font_names` 和 `source_block`。这里的坐标是 PDF page points，不能使用视觉
OCR 的 `0..1000` 坐标解释。

原生分支不运行视觉 OCR、`ocr-correct`、`ocr-correct-validate` 或
`ocr_consensus.json`。即使 `ocr.secondary.enabled: true` 残留在配置中，原生脚注仍走
native layout；候选报告中的 `ocr_evidence_mode: single_ocr` 只是下游兼容字段，不代表
调用了 OCR 或可以读取旧的双 OCR 检查点。

## PDF 结构精修

```text
ocr-pages
→（two_ocr：ocr-correct → ocr-correct-validate）
→ refine-prepare → Subagent 写 toc_tree.json
→ illustration-prepare →（pending_review：Subagent 写 illustration_decisions.json）
→ illustration-validate → illustration-apply
→ refine-local
→ footnote-prepare →（pending_review：Subagent 写 footnote_decisions.json）
→ footnote-validate → footnote-apply
→ polish → polish-validate
```

结构阶段的本地命令通常是：

```text
uv run pdf2epub -c config.yaml refine-prepare
# 工作区 Subagent 写 toc_tree.json
uv run pdf2epub -c config.yaml illustration-prepare
# 如果 manifest 为 pending_review：工作区 Subagent 写 illustration_decisions.json
uv run pdf2epub -c config.yaml illustration-validate
uv run pdf2epub -c config.yaml illustration-apply
uv run pdf2epub -c config.yaml refine-local --resume
uv run pdf2epub -c config.yaml footnote-prepare
# 如果 manifest 为 pending_review：工作区 Subagent 写 footnote_decisions.json
uv run pdf2epub -c config.yaml footnote-validate
uv run pdf2epub -c config.yaml footnote-apply
```

`illustration-prepare` 必须先于 `refine-local`，因为已确认的整页插图会影响页面合并；
`footnote-prepare` 必须后于 `refine-local`，因为脚注归并范围取自实际生成的
`tree_progress.json` 单元，而不是抽象的顶层 TOC。

OCR 完成不是“有几个 `page_*.md` 文件”就算通过。`ocr_progress.json` 会记录源 PDF
哈希、真实物理页数、已处理页、失败页、缺失页和空结果页；失败、缺页或未确认的空结果
会使 `ocr-pages` 返回非零，并阻断 `refine-prepare`、`refine-local` 和后续 readiness
检查。网络/服务失败可直接使用 `ocr-pages --resume` 重试；如果某页虽然已标记完成但
需要重新识别，可使用 `ocr-pages --resume --retry-pages 12,15`。确认某页确实是物理空白页
后，才可使用 `--allow-empty-pages` 显式放行，并保留该决定在 checkpoint 中。修复 OCR
后运行 `refine-local --resume`，页面指纹变化会使下游 polish/翻译 checkpoint 重新进入
待处理状态。

在 `refine-local` 之前运行 `illustration-prepare`。本地程序只用 layout sidecar、图片引用和
页面文字量筛选疑似整页彩页/插页，并只为候选页及其前后页生成审阅图；它不会自行断定普通图表
是整页插页。工作区 Subagent 只写 `illustration_decisions.json`，把候选页区分为
`full_page_insert`、`ordinary_illustration`、`blank_scan` 或 `body`。随后运行
`illustration-validate` 和 `illustration-apply`，生成带源页哈希及候选报告哈希的
`illustration_bindings.json`。`PageMerger` 只对其中的 `full_page_insert` 页面尝试把“前页未完句 →
整页插页 → 后页续句”恢复成一个连续段落，并把图片保留在恢复后的段落之后；普通插图和证据不足的
页面不会改变物理顺序。候选报告存在但未完成 apply 时，`refine-local` 会阻断，避免漏掉这道结构判断。

插图候选报告会记录 `ocr_evidence_mode`。`single_ocr` 只依据主 OCR；`two_ocr` 必须有当前
`ocr_consensus.json`，并比较主/次 OCR 是否都发现视觉候选。只有一套发现候选、候选标签或
几何证据不一致时，候选页才会带上差异原因交给 Subagent；本地程序不会把次 OCR 直接覆盖到
主 OCR Markdown。

只有 `ocr.secondary.enabled: true` 时才执行 `ocr-correct`。本地程序会从原始 PDF 渲染一张
与每个物理页对应的审阅图，并生成 `ocr-correct_worker_handoffs/`；工作区 Subagent 只能处理
自己 manifest 的 `assigned_files`，对照同名页图修正有直接视觉证据的识别错误，结果写入
`ocr_corrected_pages/`，并为每页写入 `ocr_correction_reviews/page_NNN.json`。随后运行
`ocr-correct-validate`，它会检查 UTF-8、输出完整性、逐页审阅记录、行数不减少、Markdown
结构标记和原始页哈希，并将通过的结果暂存到 `ocr_corrected_pages/validated/`。原始 `pages/`
永不覆盖；纠错结果缺失、过期或未通过校验时，`refine-prepare` 和 `refine-local` 会阻断。
第二套 OCR 关闭时直接使用主 OCR 的 `pages/`，不生成“已纠错”检查点；高置信度原生文字 PDF
同样不适用该阶段。

可选的双 OCR 配置可以在 `ocr-pages` 阶段启用一个不同于主 OCR 的已注册后端；同一个开关
同时决定 OCR 纠错、脚注候选和整页插图候选使用单 OCR 还是双 OCR 证据：

```yaml
ocr:
  backend: chandra
  secondary:
    enabled: true
    performance:
      max_estimated_seconds: 3600

ocr_correction:
  review_dpi: 150
```

此外，`ocr-pages` 会在主 OCR worker 启动前写入 `ocr_secondary_preflight.json`，按页数、worker
数和配置的保守页速估算次 OCR 总耗时。超过 `max_estimated_seconds` 时先暂停，只有用户明确
使用 `ocr-pages --allow-slow-secondary` 才继续。需要退回单 OCR 时，必须明确关闭
`ocr.secondary.enabled` 后重新运行；旧的双 OCR 共识不得复用，也不会自动降级。

启用双 OCR 后，`ocr-pages` 会把主 OCR 和次 OCR 的结果按页做规范化比较，忽略纯粹的
Markdown 外层标记差异。行数、全局数字序列和脚注垂直范围差异只写入诊断/版面证据；实质字符差异、
空结果和共同漏检哨兵仍会进入复核。报告写入 `ocr_consensus.json`；
没有实质差异的页面直接自动接受，只有 `action: visual_review` 的页面才进入
`ocr-correct_worker_handoffs/`。双 OCR 只是筛查，不把任一 OCR 结果当作绝对真值；两个引擎
共同犯错由两层机制补充拦截：默认每 20 页抽查一页，并把内部文本密度显著低于相邻页的页面
标记为风险页。这些信号只扩大视觉复核范围，不自动修改文本；它们仍不能证明两个 OCR 没有
共同犯错。`ocr.secondary.enabled: false` 时不运行第二套 OCR，也不运行
视觉 OCR 纠错，主 OCR 结果直接进入后续结构整理。

次 OCR 提供与主 OCR 形状兼容的 layout sidecar；脚注候选存在性或 layout 证据不一致会进入
脚注 Subagent，不能仅凭次 OCR 文本或启发式标签自动改变页面结构。两套 OCR 一致仍只是通过
筛查，不是正确性证明。已有两套 OCR 产物而只需重算规则时，
可运行 `uv run pdf2epub -c config.yaml ocr-consensus-rebuild`；该命令离线复用文本和 sidecar，
不重新调用任何 OCR 后端。

### 已有双 OCR 产物的恢复路径

这是修改共识规则后的标准续跑方式：

1. 确认 `pages/ocr_progress.json` 的源 PDF 哈希、页数和主 backend 当前，且
   `ocr_secondary/` 中每个 `page_NNN.md` 与 `page_NNN.ocr.json` 都存在。配置必须仍为
   `ocr.secondary.enabled: true`，并指定 `secondary.backend`。
2. 运行 `ocr-consensus-rebuild`。维护者检查 `ocr_consensus.json` 的
   `schema_version: 4`、`complete: true`、`failed_pages: []`，再按 record 的 `action` 读取
   `auto_accept`/`visual_review` 数量。`comparison.diagnostics` 中的行数/数字差异和
   `layout_review` 不是自动 OCR 纠错页；只有正文 `action: visual_review` 才进入下一步。
3. 运行 `ocr-correct` 生成页图、`ocr-correct_subagent_prompt.md`、总 manifest 和
   `ocr-correct_worker_handoffs/`。Subagent 只能读取自己 scoped manifest 的 `assigned_files`，
   对照同名审阅图，把结果写入 `ocr_corrected_pages/page_NNN.md` 和
   `ocr_correction_reviews/page_NNN.json`；不能改 `pages/` 或把次 OCR Markdown 当作正文答案。
4. 运行 `ocr-correct-validate`。它通过后才把 `ocr_corrected_pages/validated/` 作为视觉 PDF
   的结构阶段输入；失败时只用原命令 `--resume` 重派 pending/invalid 页。
5. 继续 `refine-prepare` → 目录 Subagent 写 `toc_tree.json` → `illustration-*` →
   `refine-local --resume`。没有 `toc_tree.json` 或 `tree_progress.json` 时不要运行
   `footnote-prepare`，因为脚注归并作用域尚未确定。

`ocr-consensus-rebuild` 的输出只替换共识检查点，不会生成 `ocr_corrected_pages/`；它不能被
当作已经完成视觉纠错的证明。
脚注和整页插图阶段还会检查证据模式与 sidecar 哈希：单 OCR 模式忽略遗留的
`ocr_consensus.json`；双 OCR 模式若共识报告、主/次 sidecar 或配置已变化，会阻断旧的
候选决定，必须重新准备。

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

`refine-local` 完成物理切分后会运行边界注脚扫描器，将安全匹配的跨文件 Markdown 脚注引用/定义记录
到 `footnote_boundary_bindings.json`；EPUB 构建时由 `FootnoteManager` 消费。原始 PDF 书签
草稿还会对明显的“大跨度目录/附录包装节点”执行保守的层级解构，Subagent 只需复核
结果。

对于所有 PDF，在 `refine-local` 后、`polish` 前运行 `footnote-prepare`。扫描 PDF 使用
OCR layout sidecar；高置信度原生文字 PDF 在 `ocr-pages` 阶段生成带 PDF 坐标、文本块和字体
元数据的 native layout sidecar。两种来源在本地使用不同的候选筛选器，但都按页面内实际顺序
生成稀疏的 `footnote_contexts/`，并共享 Subagent 决策、校验和 `footnote-apply`。视觉 OCR 只
使用弱几何证据：block 的 `bbox[3]` 进入页面底部区域（默认 `bottom_ratio: 0.64`），且 block
高度与该区域的相交比例达到默认 `bottom_intersection_ratio: 0.25`；文本以短数字和分隔符
开头时才形成数字候选，`Footnote` 标签不能单独绕过几何条件。页眉、页脚、纯页码和引用标签
会被排除。连续数字开头块（例如 `18, 19, 20, 21`）会向上回溯相邻的同一区域，但回溯结果
始终只进入 Subagent review，不直接移动。这个筛选器只缩小复核范围，不是脚注判定器。
原生文字的
页底编号候选不会仅因底部位置自动判为脚注；默认只有同页对应上标引用和小字号双重匹配的
候选本地接受，其余候选交 Subagent 复核。扫描/OCR PDF 中只有明确标签、数字开头、几何条件
同时满足且启用 `auto_accept` 时才会形成高置信度候选；无编号续文、连续区间回溯块、跨页续文
或正文/脚注交错的候选窗口交给工作区
Subagent；双 OCR 模式下，主/次候选存在性或 layout 证据不同的页面会把主、次两套 sidecar
一起交给 Subagent；正文共识的行粒度差异不会单独禁用脚注本地规则。次 OCR 独有的连续物理
行会聚合成一个 `secondary_only_candidates` 窗口。Subagent 只写
`footnote_decisions.json`，随后用 `footnote-validate` 校验；原生文字 PDF 不运行视觉 OCR
共识，即使配置残留次 OCR 开关，也只使用原生版面证据。

脚注和整页插图的证据选择由同一套本地边界负责：`refine/pdf_evidence.py` 统一判断
`native_text`、`single_ocr` 和 `two_ocr`，`refine/layout_evidence.py` 统一读取 sidecar、
清理 block 文本并归一化坐标。两个阶段可以拥有不同的候选器和 Subagent 角色，但不得各自
实现一套 OCR 模式判断或 bbox 归一化；这样原生 PDF 不会在某个阶段意外读入旧的双 OCR 报告。
脚注配置的默认值、校验和 CLI 覆盖统一由 `resolve_footnote_options()` 处理，命令 handler
不再复制这些默认值。
这里必须区分脚注和引用：正文中的作者—年份/编号引用、引文来源以及
`bibliography`/`reference` 条目使用 `citation` 或 `bibliography` 角色，脚本不会搬移它们。
脚注候选必须保留“正文 → 上一脚注续文 → 新脚注”的页内顺序，不能把跨页续文默认移动到
下一页开头。校验通过后运行 `footnote-apply`：本地脚本会把确认的脚注块从页面流中移除，
合并跨页续文，并按实际生成的 TOC 单元（完整 `unit_id`；其 `.partM` 分片共享作用域）追加为 `[^N]: ...` 章末注，生成独立的
`footnote_normalized/`；原始 `ocr_markdown/` 不会被覆盖。如果 `ocr-correct` 已经修正了块内字符，
脚本会用原始页与已验证页的字符对齐生成匹配变体，但最终仍要求在章稿中唯一命中；无法唯一定位时保持阻断并要求复核。
之后 `polish` 优先读取这个归一化源稿。
验证通过的单元上下文会挂载到后续 `polish` handoff，避免每个 worker 读取整本脚注报告。

脚注和整页插图共用 `ocr.secondary.enabled` 作为证据模式开关：关闭时只使用主 OCR，且会
忽略旧的 `ocr_consensus.json`；开启时必须存在与当前主 OCR、次 OCR 配置和次 OCR 文件匹配的
最新共识检查点，主/次 OCR 对候选的差异会进入工作区 Subagent 复核。`footnote-validate`、
`footnote-apply`、`illustration-validate`、`illustration-apply` 和 `refine-local` 都会再次
检查这个模式及其哈希，因此切换开关后不能复用旧的脚注或插图绑定。

若 Subagent 选择次 OCR 独有脚注，决定必须包含最终可见的 `text`、目标 `source_file` 和
`primary_disposition`。`absent` 表示主 OCR 确实没有该脚注；`remove` 表示主 OCR 有重复块，
并必须列出 `primary_block`/`primary_blocks`。同一脚注编号有多个正文引用时，还必须提供唯一
`marker_context`；`footnote-apply` 对缺失或不唯一的定位直接阻断，不能追加重复脚注或选择最后
一个编号。

典型的 secondary-only 决定如下；`block` 是聚合窗口的首行，不是让 Subagent 逐行重复决定：

```json
{
  "page": 126,
  "source": "secondary",
  "block": 21,
  "role": "footnote_start",
  "key": "37",
  "source_file": "chapter_1.md",
  "text": "The final visible footnote text.",
  "primary_disposition": "absent",
  "confidence": "high"
}
```

如果主 OCR 实际包含一段重复脚注，使用 `"primary_disposition": "remove"` 并填写
`"primary_blocks": [5]`；如果该编号在正文中有多个 marker，再加上一个能唯一命中的
`marker_context`。`footnote-validate` 会检查字段形式，`footnote-apply` 还会检查 sidecar、
refinement unit 和正文定位；任何一层失败都必须修正决定后重跑，不得手工改写归一化文件。

### 原生文字 PDF 的脚注 handoff

原生候选器的职责是缩小复核范围，不是替 Subagent 读懂脚注。默认采用保守的本地接受规则，
只放过版面证据同时满足的候选；其余候选仍进入 Subagent。它按以下顺序工作：

1. 读取 `pages/page_NNN.ocr.json`，按 `page_box` 将 PDF page points 的 block bbox 归一化到
   `0..1` 页面比例；不能把 Letter/A4 的 point 数值误当成 OCR 的 `0..1000` 坐标。
2. 只关注页面下部（当前默认 `bottom_ratio: 0.64`，且 block 与页底带相交比例至少为
   `0.25`）、以一至三位数字和空格/标点开头的文本块；纯数字块排除为印刷页码候选。若
   连续出现两个以上递增数字开头块，候选器会向上回溯相邻的无编号块，形成一个 review 区域。
   原生 PDF 没有可靠的 OCR 语义标签，因此只有
   同页更早位置存在对应 `<sup>N</sup>` 引用、且字号比例不超过默认 `0.88` 的候选标为
   `confidence: high`，其余保留下来的候选标为 `review_required`。
3. 原生提取器依据 PDF 文本 block 内的行顺序，在新的页底数字开头处拆分 layout block，
   因而同一页的多条脚注各自拥有稳定的 `page`/`block` 地址。`body_font_size`、block
   `font_size`、`font_names` 和上标 flag/位置只作为复核证据。
4. Subagent 只读取 manifest 列出的 `footnote_contexts/*.json` 和 sidecar 窗口，逐个候选
   写入 `footnote_decisions.json`。本地已标记 `confidence: high` 的候选不需要重复决定；
   它必须区分 `footnote_start`、
   `footnote_continuation`、`footnote_definition`、`citation`、`bibliography`、`body` 和
   `review_required`；数字开头本身不是决定理由。跨页续文仍按视觉顺序处理，不能默认贴到
   下一页开头。

`footnote-prepare` 完成后维护者应按下表解释产物：

| 产物 | 作用 | 不能替代 |
|---|---|---|
| `footnote_candidates.json` | 候选窗口、来源类型、证据模式、sidecar 哈希和 review pages | Subagent 决定 |
| `footnote_subagent_manifest.json` | 当前复核页、单位上下文和 pending 状态 | 决定校验 |
| `footnote_decisions.json` | Subagent 对候选 block 的角色决定 | sidecar 或候选事实 |
| `footnote_decision_validation.json` | 决定地址、角色、键、覆盖率和哈希门禁 | apply 结果 |
| `footnote_normalization.json` | apply 后的定位、归并和输出检查点 | 上游候选报告 |

`footnote_candidates.json` 的 `bottom_detection` 还会报告页底 block、页底数字 block、已进入
候选的数字 block、页眉/页脚与纯数字页码排除数，以及 `suspected_missed_count`。它是召回风险
提示，不是自动决定；有风险的页必须结合 Prompt 中的 `visual_file` 页面 PNG 复核。页面图会
复用 OCR 校正阶段的 freshness manifest；若没有可用源 PDF，则报告明确标记视觉证据不可用。

原生脚注的典型失败包括 `No page layout sidecars found`、`source_kind` 或
`coordinate_system` 不一致、sidecar hash 过期、决定引用不存在/重复的 page/block，及脚注
块在章稿中无法唯一命中。它们都应回到源阶段恢复，而不是手工改决定 JSON。旧运行没有
sidecar 时，恢复顺序为：

```text
ocr-pages --resume
→ refine-local --resume
→ footnote-prepare
→ [Subagent 写 footnote_decisions.json]
→ footnote-validate
→ footnote-apply
```

如果源 PDF、TOC、页合并或插图绑定改变，必须重新生成受影响的 `tree_progress.json` 单元
和脚注上下文；只有当前 `sidecar_sha256`、候选报告和单元作用域都匹配时才可恢复旧的
Subagent 输出。

脚注本地接受规则可通过配置统一控制两种 PDF 来源：

```yaml
footnotes:
  auto_accept: true
  bottom_ratio: 0.64
  bottom_intersection_ratio: 0.25
  native_max_font_ratio: 0.88
```

需要扫描/OCR PDF 和原生文字 PDF 都逐候选复核时，设置推荐写法
`footnotes.auto_accept: false`。命令行 `footnote-prepare --review-all` 是同等的临时覆盖。
无论是否本地接受，候选报告都会保留 `same_page_superscript`、`font_size_ratio` 和哈希证据，
便于审计。

### 公式和 EPUB 阅读器兼容性

`preprocess_markdown()` 处理公式时先保护代码和已有公式片段，再执行 Markdown 处理：

```text
简单行内 LaTeX → unicodeitplus → Unicode 文本
复杂行内/块级 LaTeX → latex2mathml → MathML
原始 <math>...</math> → latex2mathml → MathML
转换失败 → 可读的 LaTeX 退路并记录日志
```

MathML 是当前默认策略，因为它不增加系统级 TeX 依赖，已满足当前成品需求。维护者不要
为了“更像浏览器”直接把公式改成 SVG；若未来必须支持某个不支持 MathML 的阅读器，应先
做独立的可选渲染器设计，并把工具探测、SVG 白名单清洗、缓存、超时和跨平台测试纳入同一变更。

`refine` 是 `refine-prepare` 的别名，不再存在 provider/API 实现。

超大单元拆分支持按内容类型覆盖阈值。默认 `index` 使用 6,000 token 的拆分阈值和
4,500 token 的目标大小，仍按完整索引条目/子条目边界切分；可在
`refine.oversized_unit_split.threshold_tokens_by_type` 和
`target_tokens_by_type` 中调整。不要用固定字母范围或词典回填替代条目级校验。

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

polish handoff 会附带源语言 TOC 标签保护清单。唯一出现且匹配 TOC 的源行被删除时，
校验会生成 `polish_unique_toc_label_removed` 的 `review_required`；重复页眉只有在
版面和重复性证据充分时才可移除。续片只在文件开头删除其 TOC 所属顶层祖先标签时
使用窄范围例外，并把例外记录在校验报告；续片自己的标题以及其他分支的标题仍然阻断。
`bibliography`/`index` 单元中的短标题、页码和无标点条目只有在同类页眉信号重复出现时
才进入该页眉警告，避免把合法书目条目当作页边装饰。纯图片或无可见标题的单元使用
`binding_mode: container_only`，由 EPUB 构建阶段生成包装器标题，Markdown worker 不得
自行补写标题。

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
worker handoff 按 TOC 顺序装箱；普通单章节使用章节级 `entries`，相邻短章节使用按文件
分区的直接上下文，大章节拆分使用 `shared_entries`/`local_entries`，完整快照只保留作审计。
PDF 正文 Prompt 还会读取已验证的
`toc_tree_translated.json`，生成一个方向性全书 TOC 轮廓。轮廓按实际 TOC 的深度、分支规模
和 `global_toc_tokens` 预算自适应压缩，不固定规定保留几级标题；它只帮助理解全书结构，
当前章节的精确 TOC heading contract 才能决定正文中的标题文字。

跨语言的学派资料（例如德文术语表用于英文思想史书籍）必须单独配置在
`translation.reference_glossaries`。候选报告会把它们标记为
`reference_eligible`，但不会自动选择；用户明确选择后，Subagent 只能读取其
`reference_glossary_*` 快照，用于概念对照和既有目标语定名参考，不能覆盖正式术语表，
也不能修改原始 YAML、快照或生成自动修订版。
需要从英文正文命中德文概念时，在条目中声明
`aliases_by_language: {English: [sublation]}`。这些命中只进入当前单元的
`kind: reference` 稀疏上下文，不参与 fixed/preferred/entity 优先级。

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
脚本，不要拼接复杂的 PowerShell `python -c` 内联命令。若确需运行独立 Python 检查，
入口应设置 `PYTHONIOENCODING=utf-8`；仓库 CLI 会自动覆盖继承的 GBK 设置，并把该设置
传给子进程。

pipeline 的能力矩阵由 `pdf2epub/pipeline_policy.py` 维护。命令模块应使用该策略判断
实体、翻译 TOC、正文翻译和 polish 门禁，不要各自解析 `pipeline`/`mode`。

## 安全边界

仓库不硬编码内部项目 ID，不伪造 IDE 请求头，也不自动导出或冒用 ADC 凭证。任何需要账号授权的模型调用都应由用户在 Antigravity IDE 会话中完成。
