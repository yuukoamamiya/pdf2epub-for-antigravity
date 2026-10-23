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
- 原生文本输入路径已删除：所有新 PDF 运行都使用视觉 OCR，不存在 `pdf_text.mode` 或
  其他页面来源开关。旧版本留下的 `source_kind: native_text` page-point layout sidecar
  只作为旧产物兼容读取，不再由新运行产生。
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
    # Conservative default; up to 6 independent hand-offs may be enabled when
    # the IDE/model quota permits it.
    max_concurrency: 3
    # Packed adjacent chapters may use this combined source-token budget;
    # default is twice max_source_tokens.
    max_packed_source_tokens: 24000
  # 可选：覆盖某个具体任务
  # task_models:
  #   refine: <configured task model>
```

默认规则是：正文、元数据、目录、小说和 TeX 翻译使用配置中的 `translation`；结构分析、OCR 润色和实体提取使用 `default`。每个生成的 `*_subagent_manifest.json` 和提示词都会明确写出推荐模型，供 Antigravity 中的 Subagent 选择。具体模型版本以当前配置文件为准，本文档不固定版本号。这里是任务合同，不是 Python 对模型 API 的调用或强制切换。

## 额度耗尽与断点续传

正文任务按文件拆分。使用 `--resume` 重新准备任务时，manifest 会根据目标目录写出 `completed_files` 和 `pending_files`；提示词要求 Subagent 只处理 `pending_files`。已经通过校验的输出不会被重新覆盖。新 manifest 还会按单元比较 `unit_context_sha256` 和精确 TOC 上下文，因此外部术语表变化只会使受影响单元重新进入 `pending`；没有单元哈希的旧 manifest 才采用整批重做的兼容策略。Subagent 输出遵守 `atomic_output_contract`：先写临时同级文件，再原子替换正式文件；校验器会清理遗留临时文件，并把无效目标移到可恢复的 scratch 区。恢复前建议先运行对应的 `*-validate`，这样可以先发现空文件、行数不一致或标签损坏。

PDF/EPUB 的 `translate` manifest 会在 `worker_handoffs/` 按 TOC 顺序为相邻短章节装箱，生成隔离的最小 manifest 和提示词；父级 manifest 保留完整审计索引，worker 只接收自己的文件、批次、上下文哈希和必要的章节元数据，不重复携带全书文件统计、章节映射或审计快照路径；
`polish` 使用独立的 `polish_worker_handoffs/`，避免不同阶段的任务被误认。每个 Subagent
只读取自己 handoff 的 `assigned_files`；超过 30,000 字节的单元自动独立成批，但不能与
其他章节合并。polish 的普通章节仍可适度打包，文件预算使用独立的
`subagent.batching.polish_max_files`（默认 `min(max_files, 4)`）；`index`、
`bibliography`、`notes`、`author_notes`、`toc`、`catalog`、`ad`、`copyright`、
`frontmatter`、`metadata` 和 `cover` 会标记为 isolated batch，并各自占用独立 worker，
不与普通章节或其他特殊角色混排。隔离 worker 的 prompt 只针对其 assigned files 高亮
index 条目 1:1、书目逐条、notes 标记和 CIP/版权页短行标识符规则。普通单章节 worker 的
术语上下文只注入一次；相邻章节合并 worker 使用按
文件分区的直接上下文；大章节拆分时使用 `shared_entries` 和当前分片的 `local_entries`。

polish worker 的 scoped prompt 会在 assigned task 前给出只读自检命令：
`uv run pdf2epub -c <config_path> polish-validate --file <assigned_file> --check-only`。
manifest 记录当前 hand-off 的 `config_path`（POSIX `/` 分隔）；若旧 manifest 没有该字段，
prompt 使用“本次 hand-off 使用的同一配置”措辞，不编造路径。`--check-only` 必须与
`--file` 同时使用，只运行单文件校验，不写 `polish_file_validation.json`，不创建或清理
`polished_markdown/validated/`，也不移动或删除已暂存文件。这样每个 worker 可以独立闭环，
又不会并发争写共享 checkpoint。worker 全部结束后，主 Agent 仍运行正式
`polish-validate --file`（或全量 `polish-validate`）写入权威 checkpoint；只有该权威记录
才可作为后续打包和翻译门禁。
Markdown worker 的提示词还明确禁止修改 `pdf2epub/`、`tests/`、`docs/`、`glossaries/` 和项目
配置。准备阶段会对这些受保护文件记录哈希；校验阶段发现 Worker 改动其中任何文件时，
本轮会进入 `safety_blocked`，不会生成可恢复的内容 checkpoint，必须先由主 Agent 处理代码变更。
`translate-toc` 是正文 worker 启动前的独立任务。Subagent 只写
`toc_translation_result.json`；`translate-toc-validate` 从 `toc_tree.json` 本地合并生成
`toc_tree_translated.json`，正文 worker 不写入这两个文件。

恢复重建时，当前校验 checkpoint 已覆盖全部 `assigned_files` 的旧 handoff 不会重新生成；
父级 manifest 在 `retired_worker_handoffs` 中保留 `completed` 审计记录。源哈希变化导致
assignment 失效的旧 handoff 标记为 `superseded`，只生成新的 pending assignment。

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
| `polish_max_files` | polish 普通章节的文件数量上限，默认 `min(max_files, 4)`，硬上限为 `8`；特殊角色始终独立 worker |
| `max_source_tokens` | 一个 worker 的源 token 预算 |
| `max_packed_source_tokens` | 相邻完整章节合并 worker 的源 token 总预算；默认是 `max_source_tokens` 的两倍 |
| `single_file_max_bytes` | 单个文件超过该值时必须独立成批，默认 `30000` |
| `max_concurrency` | 独立 hand-off 的并发提示上限；默认 `3`，硬上限为 `6`，仍受大文件降速规则约束 |

装箱具有以下硬边界：

1. 一个章节组如果自身需要多个批次，先结束当前短章节装箱，再按章节内部拆出的
   part 文件分别派发；这些 part 不得与其他章节组混合。
2. 一个章节组只要包含超过 `max_source_tokens` 或 `single_file_max_bytes` 的文件，
   即使运行时只生成一个 part，也视为 oversized，不能与相邻章节合并。
3. 普通单章节仍受 `max_source_tokens` 约束；多个相邻且各自未拆分的章节合并时，合计
   token 数可使用 `max_packed_source_tokens`，但文件数、单文件字节数和章节数即将超出
   对应预算时，先提交当前 worker，再从下一个章节组开始。装箱只允许连续相邻章节，不做
   跨章节重排或负载均衡重排。
4. `--resume` 时，已经完成而被跳过的章节组是相邻性屏障；不能把它前后的两个待处理章节
   重新拼成一个 worker。
5. 若当前批次和新增章节都属于 tiny，章节数可放宽到 `max_tiny_chapters_per_worker`；
   新增普通章节、超大章节或拆分章节时不适用该放宽。
6. polish 中 `index`、`bibliography`、`notes`、`author_notes`、`toc`、`catalog`、
   `ad`、`copyright`、`frontmatter`、`metadata` 和 `cover` 是 isolated batch；
   每个此类 batch 生成一个只含该特殊单元的 worker，普通 batch 不能与之合并。
   scoped prompt 只对当前 worker 命中的角色追加规则，不向其他 worker 广播。

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

TeX 使用独立的 `tex_units/` 和 `translated_tex_units/` 文件。`source/` 恢复时要求旁置身份记录、输入身份/内容指纹及完整源树 SHA-256 匹配；旧的无记录快照仅在本地输入可用且树摘要相同的情况下接受，其他情况失败并保留旧快照。校验时本地程序检查每个单元都存在，再从这些单元重建 `project/` 并编译，因此不会把初始原文工程误判为“已经翻完”。TeX 流程中的 XeLaTeX 只用于 `translate-arxiv-validate` 的项目编译门禁，不代表 EPUB 公式也会走 XeLaTeX。

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

PDF 不再有页面来源配置；所有新运行都使用视觉 OCR。`pdf_text_probe` 只把
`classification`/`detected_recommendation` 写入报告供审计，`recommendation` 始终是
`ocr_required`。probe schema 为 3，旧 schema 不会被复用。配置中残留 `pdf_text` 字段时，
配置加载和 `doctor` 会明确报错。旧版本生成的 `source_kind: native_text` sidecar 和
`use_text_layer` probe 只用于读取/恢复旧输出，新运行不再产生它们。

`translation_report.json` 会保留导航更新结果和打包阻断状态。导航格式缺失和导航更新异常
必须区分处理：前者在仅有 EPUB 2 或 EPUB 3 导航时属于正常记录，后者必须先人工检查再决定
是否使用 `--allow-navigation-warnings`。放行 warning 不会替代 `html-validate` 或
`epubcheck` 门禁。

项目测试还会构造一个固定 EPUB，经过真实的最终打包路径后交给 PyMuPDF 渲染，比较页数、
文本版心和像素签名，并验证重复渲染稳定。这是本地阅读器引擎级别的回归，不等同于所有
商业阅读器和硬件设备的兼容性认证。

### PDF 源类型分支

`ocr-pages` 先写 `pdf_text_probe.json`，然后始终进入视觉 OCR：

```text
ocr-pages
  → pdf_text_probe.json（只做分类审计；recommendation 始终为 ocr_required）
  → pages/*.md + OCR layout sidecars
```

即使 PDF 分类为 `native_text`，`detected_recommendation` 也只是审计信息；新运行不使用
原生文本输入。可搜索但实际内容来自整页图像的 PDF、隐藏 OCR 层和混合稿同样走视觉 OCR；
不能仅凭复制文字或目录中已有 Markdown 判断。

旧版本的原生运行会留下 `backend: native_text`、`source_kind: native_text`、
`coordinate_system: page_points` 的 sidecar，其中包含 `page_box`、`layout_mode`、
`reading_order`、`body_font_size` 和有序 `blocks[]`。这些文件只用于旧输出恢复；新运行绝不
生成它们。这里的坐标是 PDF page points，不能使用视觉 OCR 的 `0..1000` 坐标解释。

旧原生运行不运行视觉 OCR、`ocr-correct`、`ocr-correct-validate` 或
`ocr_consensus.json`。即使 `ocr.secondary.enabled: true` 残留在配置中，旧原生脚注仍走
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
# 旧原生 PDF 若生成 toc_fast_path_prompt.md：工作区 Subagent 先做选定页面视觉确认
# 否则/回退时：工作区 Subagent 阅读 refine_subagent_prompt.md 并写 toc_tree.json
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

### 旧原生 PDF 的 TOC 选择性视觉确认

旧原生文字运行且存在可用 PDF 书签时，`refine-prepare` 会先运行一个保守的快速路径预检。
本地只做证据筛选，不生成最终 `toc_tree.json`。预检没有书签页匹配异常时进入
`selective_review`，选择印刷目录页、每个书签起始页、起始页前一页以及封面/版权页；只有少量
匹配异常时进入 `targeted_review`，只选择印刷目录页、异常书签起始页及其前页和元数据页。异常
超过阈值、审阅页过多或缺少目录证据时才直接要求完整审查。选定页面及其哈希写入
`toc_fast_path_manifest.json`，视觉任务写入 `toc_fast_path_prompt.md`。

旧原生 PDF 还会生成 `toc_line_evidence.json`，其中只保存书签起始页的 block、bbox、字体、页首/页中
提示和原始 Markdown 行范围。它是给视觉任务定位证据的只读索引，不是本地结构判断，也不能替代
页面图。快速路径校验会同时检查该索引和它引用的原生 sidecar 是否过期。

快速路径 Subagent 必须先确认目录条目与书签的标题、层级、顺序和物理页码，再确认章节是否
从页面中部开始。`targeted_review` 只要求对异常条目进行直接视觉复核，其余条目仍必须留下决定
记录。中部起始必须在 `toc_fast_path_decisions.json` 中记录原始
`page_XXX.md` 的 1-based `start_line`；父标题和首个子标题同页时两者都要记录。只有全部确认
通过后才写入 `toc_tree.json`，且不得修改书签的标题、层级、页码或树结构。

如果目录有漏项、书签指向不匹配、页面视觉证据不足或页内边界无法确定，Subagent 必须写入
`status: fallback_required`，不要写快速路径的 `toc_tree.json`。此时运行：

```text
uv run pdf2epub -c config.yaml refine-prepare --full-review
# 工作区 Subagent 阅读完整 refine_subagent_prompt.md 并写 toc_tree.json
```

`refine-local` 会重新核对快速路径的书签哈希、PDF 哈希、审阅页哈希、原生证据哈希、树结构和边界行号；
任何过期、缺失或不一致都会阻断并要求完整审查。没有书签、没有可识别印刷目录页、不是旧
旧原生文字运行或预检标题不匹配时，快速路径自动失效，仍使用原有全页 TOC Subagent 流程。

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

如果 `candidate_pages` 为空，manifest 状态为 `no_review_required`，不需要启动 Subagent
或创建 `illustration_decisions.json`。对于当前 TOC 明确标记为 `COVER`（或等价的
`cover` 类型）、且第 1 页同时满足全页视觉块、几乎没有可见文字和图像证据一致的窄场景，
manifest 状态为 `auto_resolved`，本地会生成带候选报告哈希的
`illustration_decisions.json`，无需启动 Subagent；这条规则不会自动处理普通插图或混合候选页。
两种无需 Subagent 的路径仍应运行 `illustration-validate` 和
`illustration-apply`；两者会验证当前候选报告并生成空的 `illustration_bindings.json`，
或生成已验证的封面绑定，使后续 `refine-local` 能明确消费检查点。零候选路径不能用来绕过
存在候选时的哈希和决定校验。

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
第二套 OCR 关闭时直接使用主 OCR 的 `pages/`，不生成“已纠错”检查点；旧原生文字运行
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

`refine-prepare` 会在 `output/<title>/` 生成 `refine_subagent_prompt.md` 和 `refine_subagent_manifest.json`。视觉 OCR PDF 中，Subagent 阅读经过校验的 `ocr_corrected_pages/validated/page_*.md`；旧原生文字运行仍读取 `pages/page_*.md`。它只负责写入 `toc_tree.json`。随后 `refine-local`：

- 校验页码范围、层级、父子包含关系和兄弟节点重叠；
- 用本地 tokenizer 估算单元大小；
- 用 `PageMerger` 合并页面并生成 `ocr_markdown/`；
- 从证据目录 `pages/` 的 `page_NNN.html` 或 sidecar `raw_html` 恢复被视觉 OCR 误删的
  TOC 标题，把精确匹配结果在内存中注入页面合并，并写入 `toc_heading_recovery.json`；
  原始页面文件和当前正文来源（可能是 `ocr_corrected_pages/validated/`）保持不变；
- 从当前 `pages/` 生成 `pagination_map.json`；对于 TOC 标题末尾的阿拉伯数字，只有在
  节点物理起始页同时出现相同印刷页码和去掉该数字后的正文标题时才自动清洗。原始/规范化
  哈希、修改项、保留候选和证据写入 `toc_title_normalization.json`，证据不足不会改写标题；
- 处理页内章节边界：`toc_tree.json` 中的 `boundary_info.start_line` 使用对应
  `page_XXX.md` 的 1-based 行号，`end_line` 是不包含该行的结束位置。若新章节从
  下一页的中部开始，上一章节会自动拥有该页标题之前的前缀；若父章节标题和首个
  子章节同页，父标题/导语会保留在首个子章节单元中，不会被前一章节吞并或丢失；
- 对超过 15,000 tokens 的 Notes、Bibliography 和 Index 单元按完整条目/段落
  自动生成 `chapter_N.partM.md` 分片，默认目标为 12,000 tokens；
- 不创建 LLM client、不发送 PDF、不消耗 API 配额。

`toc_tree.json` 顶层允许可选的 `footnote_mode`（`footnotes` / `endnotes_only` / `auto`），
它与 `chapters` 的页码范围、层级和边界校验相互独立。`validate_toc_tree_data()` 会拒绝
`disabled` 或其他非法值。Subagent 应按全书证据给出 `footnotes` 或 `endnotes_only`，不要
用 `auto` 代替判断；旧 TOC 没有该字段时仍可通过校验，并按安全默认 `footnotes` 处理。

结构 Subagent 必须区分“换页”和“页内换章”：不能因为章节标题出现在某页中部，
就把上一章节的 `end_page` 提前到上一页。若父章节与首个子章节共享起始页，必须同时
提供父节点和首个子节点的 `boundary_info.start_line`；本地校验会拒绝缺少这两个锚点的
结构结果。这样 `polish` 收到的源稿仍保持完整句子和正确章节归属，polish 只负责换行、
段落和块级结构，不会通过删除半句来“修复”错误的章节切分。页面合并器只会在前后均为
普通正文、前页句子明显未结束且后页不是标题、列表、脚注、图片或表格时自动拼接跨页续句；
其他页面边界保留为空段落，交给 polish 复核。

### TOC 标题恢复报告

`toc_heading_recovery.json` 是 `refine-local` 的确定性报告，不是 Subagent 决定文件：

- `recovered` 只包含页眉/页脚合并文本与 TOC 标题精确匹配的条目，记录 `page`、`title`、
  `level`、`block_text`、`evidence`（HTML 文件和块索引）以及可用的 `start_line`；
- `skipped` 记录 `title_already_present`、`no_page_evidence`、`no_header_blocks`、
  `no_heading_match`、`native_sidecar`、`invalid_sidecar`、`ambiguity` 等原因；这些情况
  一律不猜测、不阻断；
- `status` 为 `validated`（至少恢复一项）或 `no_recovery`；
- `page_markdown_fingerprint` 绑定当前正文页目录，`evidence_pages_fingerprint` 绑定证据
  目录的 `page_*.md`、`page_*.ocr.json` 和 `page_*.html`；`toc_sha256` 绑定当前 TOC；
- 恢复映射的稳定哈希写入 `tree_progress.json` 的 `toc_heading_recovery_sha256`。旧
  checkpoint 缺少该键、恢复映射变化或证据目录变化时，`refine-local --resume` 必须重新
  生成单元。切换 `ocr_corrected_pages/validated/`、`pages/` 或其他页面来源后不得复用旧报告。

`refine-local` 完成物理切分后会运行边界注脚扫描器，将安全匹配的跨文件 Markdown 脚注引用/定义记录
到 `footnote_boundary_bindings.json`；EPUB 构建时由 `FootnoteManager` 消费。原始 PDF 书签
草稿还会对明显的“大跨度目录/附录包装节点”执行保守的层级解构，Subagent 只需复核
结果。

对于所有 PDF，在 `refine-local` 后、`polish` 前运行 `footnote-prepare`。扫描 PDF 使用
OCR layout sidecar；旧原生文字运行在旧版本的 `ocr-pages` 阶段生成带 PDF 坐标、文本块和字体
元数据的 native layout sidecar。两种来源在本地使用不同的候选筛选器，但都按页面内实际顺序
生成稀疏的 `footnote_contexts/`，并共享 Subagent 决策、校验和 `footnote-apply`。视觉 OCR 只
使用弱几何证据：block 的 `bbox[3]` 进入候选底部区域（默认召回下限
`bottom_ratio: 0.50`，相交比例 `bottom_intersection_ratio: 0.08`）。这两个值只是候选短名单的
召回地板，不能单独把 block 判为脚注；页面自身的内容尾部、正文流相对分离和独立的脚注语义证据
才允许本地接受。文本以短数字和分隔符开头时才形成数字候选，`Footnote` 标签不能单独绕过几何条件。
页眉、页脚、纯页码和引用标签会被排除。随后本地按页面内 block 分布、垂直间距、左右对齐、连续数字
和未编号邻块推断页面级脚注区域：证据不足、边界不清、续文、正文/脚注重叠、OCR 证据冲突、正文流
延伸到页尾和几何漏检都会形成区域级视觉审计任务。
`copyright`、`metadata`、`frontmatter`、`cover`、`author_notes` 等已由 TOC 标注的前置或附属单元，
会先按边界从脚注候选中排除；共享页面缺少可靠边界时采用整块保守排除。普通正文中紧跟句末/冒号的
编号列表若仍有明显页底余量，也会从视觉队列中排除，但真正贴近物理页尾的数字区仍保留原有复核路径。
连续数字开头块（例如 `18, 19, 20, 21`）默认会向上回溯相邻的同一区域；如果数字块中已经出现
明确的 `Footnote` 标签，则普通 `Text` 块默认不会再被机械回溯，只有同样带脚注标签的相邻块才可进入
该区域；但为覆盖“正文 → 上一脚注续文 → 新脚注”的跨页布局而保留的普通 `Text` 回溯块，若没有跨页证据
或只有低置信度且数字区已有 `Footnote` 标签，只写入 `suppressed_continuation_candidates` 作为上下文，
不触发视觉审核；明确标注的无编号脚注、中/高置信度连续性和真正的边界冲突仍会审核。明确标注的区域首块也不会仅因上方相邻正文而触发边界审核。回溯区间、`audit_candidates`
和 `footnote_visual_audit_manifest.json` 会交给 Antigravity 视觉 Subagent。准备阶段尽量附带
区域裁剪图，不能裁剪时附整页图。这个筛选器只把清晰区域留在本地，把异常压缩成区域任务。
`footnote_candidates.json` 同时记录跨页续文的置信度和分项证据（上一页最后一个物理脚注、当前页候选前的最近
正文块、bbox/字号/字体对齐、块间距以及句首句末连续性）。本地会在“延续上一页脚注”和“当前页正文”两个
假设之间做保守的词汇连续性比较；正文块中保留的 `<sup>…</sup>` 引用是强正文信号。只有普通 `Text` 块的
正文假设明显胜出时才写入 `suppressed_continuation_candidates` 作为上下文，不自动移动；脚注假设胜出或比较
不分明时仍交给视觉 Subagent。中/高置信度且最后一个物理脚注的文本未明显闭合时，本地可将该块归类为
`footnote_continuation`。最后一个脚注已闭合或证据不足时仍交给视觉 Subagent。上一页编号是否唯一不是判断依据：
单个脚注可能跨页，多个脚注时也只有物理上最后一个脚注有自然
续接位置。视觉审核任务要求
先打开整页 PNG 判断整体边界，再用区域裁剪图放大文字细节，不能只看裁剪图。
旧原生文字的
页底编号候选不会仅因底部位置自动判为脚注；默认只有同页对应上标引用、小字号双重匹配且区域
与页面相对内容尾部对齐的候选本地接受，其余候选交 Subagent 复核。扫描/OCR PDF 中只有明确标签、
数字开头、页面相对尾部证据和区域没有异常且启用 `auto_accept` 时才会本地接受；明确的无编号续文、连续区间中的
边界冲突、跨页中/高置信度续文或正文/脚注交错区域交给工作区
Subagent；双 OCR 模式下，主/次候选存在性或 layout 证据不同的页面会把主、次两套 sidecar
一起交给 Subagent；正文共识的行粒度差异不会单独禁用脚注本地规则。次 OCR 独有的连续物理
行会聚合成一个 `secondary_only_candidates` 窗口。Subagent 写
`footnote_decisions.json` 中的 `region_audits` 和每个 review block 的决定，随后用
`footnote-validate` 校验；兼容恢复仍接受 flat `decisions`。旧原生文字运行不运行视觉 OCR
共识，即使配置残留次 OCR 开关，也只使用原生版面证据。

脚注和整页插图的证据选择由同一套本地边界负责：`refine/pdf_evidence.py` 统一判断
`native_text`、`single_ocr` 和 `two_ocr`，`refine/layout_evidence.py` 统一读取 sidecar、
清理 block 文本并归一化坐标。两个阶段可以拥有不同的候选器和 Subagent 角色，但不得各自
实现一套 OCR 模式判断或 bbox 归一化；这样旧原生运行不会在某个阶段意外读入旧的双 OCR 报告。
脚注配置的默认值、校验和 CLI 覆盖统一由 `resolve_footnote_options()` 处理；`auto` 模式
再由 `resolve_effective_footnote_mode()` 从当前 TOC 的 `footnote_mode` 解析 effective
mode，命令 handler 不再复制这些默认值或自行读取 TOC。
这里必须区分脚注和引用：正文中的作者—年份/编号引用、引文来源以及
`bibliography`/`reference` 条目使用 `citation` 或 `bibliography` 角色，脚本不会搬移它们。
如果 TOC 单元属于集中尾注、作者说明、出版社目录或广告，分别使用
`type: "notes"`、`type: "author_notes"`、
`type: "toc"`、`type: "catalog"` 或 `type: "ad"`；版权页/CIP 页使用
`type: "copyright"`，其他明确的前置页使用 `type: "frontmatter"`，只有证据明确时才使用
`type: "cover"` 或 `type: "metadata"`。这些单元的页底数字不会被当作随页脚注；角色会经
`pdf_unit_roles.py` 投影到 translate/polish，版权页和前置页在 polish 中必须保持短行、
ISBN/CIP 等标识符的独立性，不得当作页边装饰删除。
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

### 旧原生文字 PDF 的脚注 handoff

旧原生候选器的职责是缩小复核范围，不是替 Subagent 读懂脚注。默认采用保守的本地接受规则，
只放过版面证据同时满足的候选；其余候选仍进入 Subagent。它按以下顺序工作：

1. 读取 `pages/page_NNN.ocr.json`，按 `page_box` 将 PDF page points 的 block bbox 归一化到
   `0..1` 页面比例；不能把 Letter/A4 的 point 数值误当成 OCR 的 `0..1000` 坐标。
2. 先用宽松的候选召回地板（未配置时 `bottom_ratio: 0.50`、相交比例 `0.08`）收集页面下部的
   数字开头块；纯数字块
   排除为印刷页码候选。若连续出现两个以上递增数字开头块，候选器默认会向上回溯相邻的无编号块，
   形成一个 review 区域并生成视觉审计任务；连续数字本身只是结构证据，没有一致的 `Footnote` 标签、
   同页上标引用或原生文字字号/标记等独立证据时，不得本地接受。数字组中已有 `Footnote` 标签时，普通 `Text` 块
   默认不再回溯，以免把正文末行误报为脚注续文。若紧邻上一页已有脚注候选，则恢复该回溯并交给
   视觉审核，以保留跨页续文召回；若回溯块同时有当前页正文上下文和上一页末脚注上下文，本地会比较两者的
   内容连续性。正文假设明显胜出，或块内有正文 `<sup>…</sup>` 引用时，普通 `Text` 块只保留为上下文；
   脚注假设胜出或无法区分时仍进入视觉审核。区域首块已有 `Footnote` 标签时，也不会仅因上方相邻正文触发
   边界不确定审核。
   旧原生 PDF 没有可靠的 OCR 语义标签，因此只有
   同页更早位置存在对应 `<sup>N</sup>` 引用、且字号比例不超过默认 `0.88` 的候选标为
   `confidence: high`；同时还必须通过页面相对内容尾部的区域门禁，其余保留下来的候选标为
   `review_required`。
3. 旧原生提取器依据 PDF 文本 block 内的行顺序，在新的页底数字开头处拆分 layout block，
   因而同一页的多条脚注各自拥有稳定的 `page`/`block` 地址。`body_font_size`、block
   `font_size`、`font_names` 和上标 flag/位置只作为复核证据；只有当前书稿反复显示稳定的
   正文/脚注字号阶梯时，本地才启用 `native_font_profile`，把与主导正文同大或更大的页底数字
   block 排除为正文。对于已经形成阶梯的原生稿，注脚字号但没有同页上标/脚注标签的出版年份，
   以及明确处在正文流中的数字块，会记录为 `suppressed_native_citations` 并从视觉队列排除；
   其他小字号数字仍保留复核。没有形成阶梯时，字号不能单独排除候选。
4. Subagent 只读取 manifest 列出的 `footnote_contexts/*.json`、区域 manifest 和 sidecar/图片窗口，
   逐个异常区域写入 `footnote_decisions.json`。本地已标记 `confidence: high` 的候选不需要重复决定；
   它必须区分 `footnote_start`、
   `footnote_continuation`、`footnote_definition`、`citation`、`bibliography`、`body` 和
    `review_required`；数字开头本身不是决定理由。跨页续文仍按视觉顺序处理，不能默认贴到
    下一页开头。如果 manifest 的 `endnote_ambiguity_count` 大于 0，还要读取
    `endnote_definition_ambiguities.json`，为每条歧义写
    `endnote_definition_decisions.json` 的 `definition`/`continuation` 决定；这一步独立于
    `footnotes.mode`，`endnotes_only` 下同样必须完成。

`footnote-prepare` 完成后维护者应按下表解释产物：

| 产物 | 作用 | 不能替代 |
|---|---|---|
| `footnote_candidates.json` | 候选窗口、来源类型、证据模式、sidecar 哈希和 review pages | Subagent 决定 |
| `footnote_visual_audit_manifest.json` | 异常区域、bbox、原因、页面图/裁剪图和 review block | 视觉区域审计 |
| `footnote_subagent_manifest.json` | 当前复核页、单位上下文、尾注歧义数量和 pending 状态 | 决定校验 |
| `footnote_decisions.json` | Subagent 对候选 block 的角色决定 | sidecar 或候选事实 |
| `endnote_definition_ambiguities.json` | 需要判断边界的 numbered punctuation 尾注定义行 | 本地自动删除或改写 |
| `endnote_definition_decisions.json` | 每条歧义的 `definition`/`continuation` 决定 | 候选事实 |
| `endnote_definition_validation.json` | 逐条覆盖、角色、文件和行号校验 | apply 结果 |
| `footnote_decision_validation.json` | 决定地址、角色、键、覆盖率和哈希门禁（含 `endnote_definitions`） | apply 结果 |
| `footnote_normalization.json` | apply 后的定位、归并和输出检查点 | 上游候选报告 |

### 集中尾注归一化

`type: notes` 是集中尾注的结构合同，不要求对应 Markdown 文件保留 `# Notes` 标题。
`footnote-apply` 会在写入 `footnote_normalized/` 时识别尾注定义，并把普通正文中匹配的
`<sup>N</sup>` 或 Unicode 数字上标转换为 `[^N]`；尾注定义转换为 `[^N]: text`。已有
Markdown 脚注语法会被保留，页脚脚注仍按上一节的页面候选和 Subagent 决定处理。

当同一打印编号在多个章重新开始时，`refine/endnote_scopes.py` 从 `toc_tree.json`、
`ocr_markdown/tree_progress.json` 和当前 Markdown 构建只读 scope 目录：

- 顶层 unit 使用 `u<unit_order>`，后代使用 `u4c1`、`u4c1c2`；scope 保存源标题和保守的
  `title_variants`（剥离 `Chapter N:`、`Part N:`、前导 `N.` 和冒号后的副标题）。
- 正文只按 `^#{1,6}\s` 标题行切分 span；第一个 part 从 unit scope 开始，后续 part 继承
  上一个 part 的最后一个 scope。匹配多个 scope 的标题只记录 ambiguity，匹配不到的普通
  小节标题不改变当前 scope。
- Notes 文件按同一套标题变体匹配回正文 scope。标题既可以是 Markdown 标题，也可以是
  保守识别的独立纯文本标题：strip 后 1..120 字符、前后为空行或文件边界、不以 Markdown/
  HTML 标记或 `N.`/`N)`/`N、` 定义开头、不以句末标点结尾。唯一匹配才切换；多个匹配只记录
  ambiguity 并保持当前 scope；`Notes to Chapter N`、`Abbreviations` 等未匹配标题也保持
  当前 scope（未匹配的 Markdown 标题仍写入报告）。跨 part 继承上一个 Notes 标题的 scope，
  未切换 scope 的定义保留原 scope（文件开头为 `scope_key=None`）。
- 正文 span 内收集 `<sup>N</sup>`、Unicode 上标和已有 `[^N]` 引用；同文件存在
  `[^N]:` 定义时该引用按页脚注处理，不进入尾注证据。

触发 `per_chapter` 模式的条件是同一数字 key 在至少两个 scope 有定义，或同一
`(scope, key)` 有多个定义。只有需要去重的 key 改写为 `fnc-<scope>-<number>`；同一 scope
的第 N≥2 个定义使用 `fnc-<scope>-<number>-oN`。定义和引用分别按文件/行顺序、阅读顺序
配对：第 1 个引用指向基础 key，第 2 个引用指向 `-o2`。非重复 key 保持原数字，未触发
scoping 的书籍仍走原有全局路径。

Notes 中可能出现“外层页计数器 + 内层真实注释号”（例如 `1. 16. ...`）。只有内层数字出现在
当前 scope 的正文引用集合中，且内层不小于外层时，才采用内层作为 key 并剥离内层编号；
`3. 2005 was...` 因 2005 不在引用集合中而保持 key `3`。该判定只在 scope 目录可用时启用。

以下情况必须 fail closed，并提示修复 `toc_tree.json`/章标题（可先检查
`toc_heading_recovery.json`）后重跑 `footnote-prepare → footnote-validate →
footnote-apply`：重复 key 落在 `scope_key=None`；定义所在 scope 没有正文 span；引用所在
scope 没有对应定义；某个 scope 的引用数超过定义数。定义数多于引用数时，多出的定义仍写入
Notes，并作为 `orphan_scoped_definitions` 报告。scope 目录不写入 `output/`，只进入
`footnote_normalization.json` 的 `endnotes` 报告字段（`scope_mode`、`scopes`、
`scoped_keys`、`double_numbered_definition_count`、`duplicate_definition_occurrences`、
`orphan_scoped_definitions`）。

OCR 把 Notes 定义识别成无序列表时，行首可选的 `-`/`*`/`+` 标记与 `N.`/`N)`/`N：`
等价；`footnote-apply` 的本地 normalization 会把 `- N. text`、`* N. text`、
`+ N. text` 直接改写为 `[^N]: text`，不交给 polish 或 translate Subagent。

集中尾注的定义边界采用混合识别。文件开头、前一行空行、前一行以句末标点结束，或前一行
是 Markdown 标题的 `N.`/`N)`/`N：`（含可选 `-`/`*`/`+` 标记）行由本地直接接受；
`N text` 日期续行继续使用
`previous_line_blank` 规则，Markdown/HTML 定义和 scope 识别不进入该队列。前一行非空且
不是句末标点（例如以 `:`、`,`、`;`、`-` 或普通单词结尾）的 numbered punctuation 行写入
`endnote_definition_ambiguities.json`，每项包含 `file`、`line`、`key`、
`previous_line`、`line_text`、`next_line` 和可用的 `scope_key`。只有这些行交给
Subagent；它必须为每条写入 `endnote_definition_decisions.json` 的一个
`definition`/`continuation` 决定。完整决定形状：

```json
{
  "schema_version": 1,
  "decisions": [
    {
      "file": "chapter_10.part1.md",
      "line": 774,
      "role": "continuation",
      "confidence": "high",
      "reason": "The number is a page locator; the sentence continues."
    }
  ]
}
```

`footnote_subagent_manifest.json` 记录 `endnote_ambiguity_count`、
`endnote_ambiguities`、`endnote_decision_file` 和 `endnote_definition_file`；页面复核或
尾注歧义任一存在时 `status` 为 `pending_review`。`footnote-validate` 要求每条歧义恰好
一个决定，缺失、多余、重复、未知角色或 `review_required` 都阻断，并把结果写入
`footnote_decision_validation.json` 的 `endnote_definitions` 字段和
`endnote_definition_validation.json`。`footnote-apply` 只有在该字段有效时才把 decisions
传给 `normalize_endnotes()`；歧义无决定时在错误中给出 `file:line` 并 fail closed。
存在歧义时，`footnote_normalization.json` 记录
`endnote_definition_ambiguities_sha256` 和 `endnote_definition_decisions_sha256`；
`footnote_status` 对已记录的文件做 freshness 检查，缺失或变化时 stale，并提示重跑
`footnote-prepare → footnote-validate → footnote-apply`。没有歧义时不要求这两个哈希。

EPUB 构建时，`pdf2epub/epub/footnotes/keys.py` 的 `display_footnote_key()` 负责把
`fnc-<scope>-<number>` 和 `...-oN` 显示为原始数字；链接、ID 和扫描器仍使用真实 key。
因此读者看到 `[1]`，而锚点和跨章往返使用内部唯一 key。

`footnotes.mode` 默认是 `auto`。refine TOC Subagent 必须在 `toc_tree.json` 顶层写入
`footnote_mode`：集中 Notes/Endnotes 且每章重新编号或按章组织时用 `endnotes_only`；
注释主要印在页底时用 `footnotes`；两者都有或证据不清时也用 `footnotes`，因为它同时覆盖
页面脚注与集中尾注路径。Subagent 不得用 `disabled` 规避判断；`auto` 只表示未作决定，
本地会回退到 `footnotes`。`resolve_effective_footnote_mode()` 先读
`ocr_markdown/tree_progress.json` 的 `book_metadata.footnote_mode`，再读
`toc_tree.json`；显式配置始终覆盖 TOC。`footnote_candidates.json` 记录 effective
`footnote_mode`、`configured_footnote_mode` 和 `footnote_mode_source`；validate/apply/
status 都按当前 effective mode 比较，TOC 决定变化使 effective mode 改变时 checkpoint
视为 stale，必须重跑 `footnote-prepare → footnote-validate → footnote-apply`。

effective mode 为 `endnotes_only` 时只关闭页面脚注候选，不关闭这条 typed Notes 归一化路径；
只有 effective mode 为 `disabled` 才会原样保留；该模式同时跳过这条 Polish 尾注归一化校验和
构建门禁。其他模式下，`polish-validate` 会按 `file_roles` 检查 Notes 文件及所有普通单元的
对应标记，EPUB 构建前还会再次阻断残留的原始定义或未链接上标。
无法唯一配对的重复尾注编号、结构角色变化或归一化报告版本过期时，必须重新运行
`footnote-prepare → footnote-validate → footnote-apply`，不能手工修改派生产物。

`footnote_candidates.json` 的 `bottom_detection` 还会报告页底 block、页底数字 block、已进入
候选的数字 block、页眉/页脚与纯数字页码排除数，以及 `suspected_missed_count`。达到审计条件的
漏检还会进入 `audit_candidates` 和视觉 manifest，不再只是静态警告。异常区域会复用 OCR 校正
阶段的 freshness manifest，并尽量附 `region_visual_file` 裁剪图；若没有可用源 PDF，则报告明确
标记视觉证据不可用。新版决定可用 `region_audits` 表示区域结论，旧版 flat `decisions` 仍可恢复。

旧原生脚注的典型失败包括 `No page layout sidecars found`、`source_kind` 或
`coordinate_system` 不一致、sidecar hash 过期、决定引用不存在/重复的 page/block，及脚注
块在章稿中无法唯一命中。它们都应回到源阶段恢复，而不是手工改决定 JSON。新版本不再生成
native sidecar；旧运行若缺少 sidecar，只能使用旧版本恢复，或把该页集视为没有原生版面
证据。恢复顺序为：

```text
refine-local --resume
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
  # 默认 auto：TOC Subagent 在 toc_tree.json 写 footnote_mode。
  # 也可显式固定 footnotes/endnotes_only；无任何脚注/尾注处理时使用 disabled。
  mode: auto
  auto_accept: true
  # 仅为候选召回地板，不是自动接受阈值。
  bottom_ratio: 0.50
  bottom_intersection_ratio: 0.08
  native_max_font_ratio: 0.88
```

默认的 `auto_accept: true` 只让没有异常的清晰区域走本地路径；边界不清、漏检或 OCR 冲突区域
会自动进入视觉审计。需要把所有候选都交给视觉 Subagent 时，设置 `footnotes.auto_accept: false`
或使用 `footnote-prepare --review-all` 临时覆盖。无论是否本地接受，候选报告都会保留区域证据、
`same_page_superscript`、`font_size_ratio` 和哈希信息，便于审计。

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
结果直接进入 polish；polish 仍只处理结构，不应静默改写 OCR 字符。对旧原生矢量文本运行，polish 用于识别视觉换行与真实
段落边界；旧原生文字稿也不得进行无依据的拼写或字形改写。`polish-validate` 还会对源稿和
润色稿做忽略换行、Markdown 外层标记及已确认页边装饰的内容保真比较；正文 token 或数字
标记大量丢失，或结构性空父标题下的子章节正文被重复复制时会阻断，而不是把不完整或
重复的润色稿交给后续翻译。普通正文段落内部不得为了视觉排版新增换行；EPUB 渲染默认把
单换行视为软换行，只有空行或显式 `<br />` 才形成段落/硬换行。父标题可以没有正文，但必须只
保留标题本身；子章节正文只出现一次并归属于最近的子标题。
PDF 翻译、实体提取和打包都必须以当前且通过 `polish-validate` 的
`polished_markdown/validated/` 为源稿；如果润色稿缺失、校验失败或与当前源稿不匹配，
本地门禁会拒绝继续。

polish handoff 会附带源语言 TOC 标签保护清单。唯一出现且匹配 TOC 的源行被删除时，
校验会生成 `polish_unique_toc_label_removed` 的 `review_required`；重复页眉只有在
版面和重复性证据充分时才可移除。已登记的 TOC Part/Chapter **Markdown 标题行**直接从
页边装饰候选中豁免，`# PART I` 等不会因尾部罗马数字被当成页码；TOC 带副标题时允许匹配
冒号前的短标签。纯文本 `PART I` 等重复页眉仍进入候选检测，不因匹配 TOC 标题而豁免。
`bilingual_warnings` 只在 translate 阶段运行，polish 不触发未翻译长英文段落警告。
续片只在文件开头删除其 TOC 所属顶层祖先标签时使用窄范围例外，并把例外记录在校验报告；
续片自己的标题以及其他分支的标题仍然阻断。
`bibliography`/`index` 单元中的短标题、页码和无标点条目只有在同类页眉信号重复出现时
才进入该页眉警告，避免把合法书目条目当作页边装饰。纯图片或无可见标题的单元使用
`binding_mode: container_only`，由 EPUB 构建阶段生成包装器标题，Markdown worker 不得
自行补写标题。
`toc` 与 `bibliography`、`index` 一样是受保护结构角色；目录中的点线引导页码不应被当作普通正文
页眉页脚。页眉候选还覆盖 `第一部分`、`第二章`、`第12卷` 等中文编号样式，以及通用的装饰符号页眉。
特殊角色的数字标记审计使用 `autojunk=False`，避免长书目或索引中的高频年份、页码被序列匹配器丢弃。

`notes`/尾注单元还保留一个窄范围的结构标题白名单：形如 `## Chapter 1`、`## 第2章`
或 `## Notes to Chapter 3` 的二至六级 Markdown 标题不会仅因末尾数字被识别为
`running_title_plus_page_label`。这只保护尾注的章节分组标题；普通的 `Notes 7`、重复短页眉
或其他页边装饰仍会进入 review gate，不能把整个 `notes` 单元视为自动豁免。

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
大章节拆分时，`toc_heading_contexts` 按物理 part 下发：每个 part 只包含其源文本中实际
可见的子标题，父标题也只在该 part 可见时标记 `visible`；子标题按阅读顺序第一次出现的
part 归属。源标题在所有 part 都缺失的子标题只挂到第一个 part 并标记 `container_only`，
对应 worker 不得新增该标题，校验也会跳过它。
当 `container_only` 或缺失的子标题在构建阶段找不到可见 Markdown 标题时，EPUB 构建器
保留 TOC 层级，但导航回退到父单元文件或首个含文件的后代，不生成指向不存在锚点的
fragment 链接。

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

`translate-toc` 会生成不可变源树的合同快照和扁平模板；Subagent 只读取
`toc_translation_source.json`/`toc_translation_template.json`，并写入
`toc_translation_result.json`。结果必须保持条目顺序、JSON 路径和 `original` 字段不变，
只填写译后书名和标题。`translate-toc-validate` 再从当前且哈希匹配的 `toc_tree.json`
合并生成 `toc_tree_translated.json`，因此 Subagent 不需要复制页码、层级、边界和 `type`
等非标题字段。`status`/`doctor` 只验证这份结果而不物化合并文件；显式的
`translate-toc-validate` 才会写入合并产物。旧版直接提交完整 `toc_tree_translated.json`
的 contract v1 仍只为恢复历史任务保留兼容校验，新任务必须使用扁平 result 合同。

翻译 Subagent 完成单个 PDF 单元后可以先做低成本闭环校验：

```text
translate-validate --file chapter_5.3.2.md
```

单文件模式会同时检查该文件绑定的可见 TOC 标题：结果写入
`report["toc_heading_bindings_local"]`，绑定失败时从 `valid_files` 移除该文件、删除
`translated/validated/` 中的副本并写入 `retry_required`。单文件结果会写入
`translate_file_validation.json`，供 `--resume` 使用；它不等同于全书校验。
未完工时运行不带 `--file` 的校验会输出 `[完成/总数]` 和 pending 清单，并暂缓 TOC heading
binding；所有目标文件存在后才运行该门禁。需要显式提前做不完整终审时使用
`translate-validate --final`，打包前仍须完成不带 `--file` 的最终校验。

参考文献和索引单元会额外进行离线数字标记校验，覆盖年份、版次、DOI/ISBN 片段、页码、页码范围和索引交叉引用；数字标记发生丢失、改写或重排时，校验会拒绝该单元。

Windows 下的批量替换、JSON 写入和正则处理应使用仓库已有的 UTF-8 脚本或可复用
脚本，不要拼接复杂的 PowerShell `python -c` 内联命令。若确需运行独立 Python 检查，
入口应设置 `PYTHONIOENCODING=utf-8`；仓库 CLI 会自动覆盖继承的 GBK 设置，并把该设置
传给子进程。

pipeline 的能力矩阵由 `pdf2epub/pipeline_policy.py` 维护。命令模块应使用该策略判断
实体、翻译 TOC、正文翻译和 polish 门禁，不要各自解析 `pipeline`/`mode`。

## 安全边界

仓库不硬编码内部项目 ID，不伪造 IDE 请求头，也不自动导出或冒用 ADC 凭证。任何需要账号授权的模型调用都应由用户在 Antigravity IDE 会话中完成。
