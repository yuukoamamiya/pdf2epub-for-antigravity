# pdf2epub Agent 工作规范

本文件是本仓库翻译工作的唯一执行规范源。`docs/antigravity-workflow.md` 仅作补充说明；
执行任务时以本文件为准。

仓库模块职责和依赖方向见 `docs/architecture.md`；该文件仅作架构说明，不改变本文件的
执行规范和优先级。

## 0.1 Agent 快速上手

每次接手仓库任务，按下面顺序做，不要先猜流程或直接处理书稿：

1. 先运行 `git status --short`，确认已有改动和未跟踪文件；不删除、不覆盖不属于本次任务的文件。
2. 判断输入和目标：
   - PDF → 第 2 节的 PDF 流程；只转换不翻译时使用 `pipeline: epub_conversion`；
   - EPUB/MOBI/AZW3 → 第 3 节的高保真 HTML 流程；
   - 轻小说 EPUB → 第 4 节的小说流程；
   - arXiv 或本地 TeX → 第 5 节的 TeX 流程。
3. 读取对应配置和 `ocr.secondary.enabled`。双 OCR 开启时必须安装并使用配置的次 OCR，
   单 OCR 时忽略遗留的共识报告；不要用文件是否存在来猜当前模式。
4. 所有本地命令使用 `uv run pdf2epub -c <配置> <命令>`。准备命令只会生成 prompt、manifest
   和本地中间结果，不代表 Subagent 工作已经完成。
5. 每个准备命令成功后立即检查其 prompt、manifest 和 `pending_files`/`pending_units`，
   打开 Antigravity 工作区 Subagent，让它直接写入指定目录；主 Agent 不在聊天中翻译或代写译文。
6. Subagent 完成后先跑对应的单文件校验，再跑全量校验；只有当前源哈希、checkpoint、
   结构校验和安全审计都通过，才可进入下一阶段或打包。
7. 中断或失败时保留已有产物，先校验，再使用原命令的 `--resume`；只重做 pending/invalid 项。

常用验收命令：

```text
uv run pytest -q
git diff --check
```

这三层文档的职责不同：本文件规定 Agent 必须怎么做；`README.md` 供使用者了解能力和快速
开始；`docs/` 记录模块边界、产物合同和维护细节。修改流程、命令、配置或产物时，三层文档
都要检查，但执行优先级始终是本文件最高。

## 0. 最高优先级：Subagent 总闸

用户提出以下任一任务时，必须使用 Antigravity IDE 的**工作区 Subagent**：翻译、润色、
OCR 结构判断、目录判断、术语提取、元数据翻译、参考文献或索引处理。

主 Agent 严禁：

- 直接阅读正文并在自己的上下文中翻译、润色或判断结构；
- 直接写入 `translated/`、`polished_markdown/`、`translated_compressed/` 等译文目录；
- 把译文贴在聊天中代替 Subagent 写入目标文件；
- 因 Subagent 暂时不可用而自行接管翻译。

主 Agent 只做：检查配置和文件名、运行本地准备/校验/合并/打包命令、生成任务合同、
调度和等待 Subagent、读取校验报告、安排重试并向用户汇报。

如果 IDE 中看不到或无法打开工作区 Subagent，必须暂停并告知用户，不得降级执行。
不要假设存在 `define_subagent`、`invoke_subagent` 等固定工具名，使用当前 IDE 实际
提供的 Subagent 入口。

Subagent 必须读取本地命令生成的 `*_subagent_prompt.md` 和 manifest，并直接读写工作区
文件。只有“目标文件已写入”且“本地校验通过”才算完成；目标文件非空、聊天确认或模型
返回译文片段都不是完成证据。

### 每次开工检查

在主 Agent 读取正文、`pages/`、`ocr_markdown/`、`polished_markdown/` 或
`compressed_units/` 内容之前，必须确认：

- [ ] 已确定使用 PDF、EPUB、轻小说或 TeX 流程；
- [ ] 若 `ocr.secondary.enabled: true`，已完成 `ocr-correct-validate`；若关闭第二套 OCR，已明确接受单 OCR 不做视觉纠错；或已确认是高置信度原生文字 PDF；
- [ ] 已运行本地准备命令并生成 manifest 和 Prompt；
- [ ] 已打开工作区 Subagent，并把对应 Prompt/manifest 交给它；
- [ ] 已明确本批次的 `pending_files` 或 `pending_units`；
- [ ] 已安排单文件校验和最终全量校验。

任何一项无法确认，都停在准备阶段。

## 1. 通用执行边界

- Python 只做拆分、压缩、校验、合并和打包，不调用翻译 API，不创建模型客户端。
- 只有 `ocr-pages` 可以按 OCR 配置调用 OCR 服务；OCR 服务不得用于翻译或结构判断。
- 外部术语表只读。先用 `glossary-candidates` 扫描并生成候选报告；程序只做格式和
  语言筛选，不凭文件名猜领域。唯一且明确匹配时才写入 `translation.glossaries`，
  多个候选或领域不清时先询问用户。跨语言但相关的表只能由用户明确写入
  `translation.reference_glossaries`，作为只读参考，不参与正式术语优先级。忽略
  `*.example.*`、README 和 `output/` 快照。
- 术语优先级固定为：外部领域表 `fixed` > 外部领域表 `preferred` > 当前书实体表；
  短语优先于其组成部分。若出现无法按此规则解释的冲突，停止翻译并报告，不要临时造译法。
- 术语准备完成后，检查 `output/<title>/glossary_selection.json`：它记录本次是否明确
  选择外部表、配置路径、源文件哈希和工作区快照哈希。规范化只读快照位于
  `output/<title>/translation_glossaries/`；按源单元裁剪的上下文位于其下的
  `unit_contexts/`。PDF 和 EPUB 翻译都必须优先读取 worker handoff 为当前分配章节
  列出的稀疏上下文；普通单章节 worker 把该章节命中的条目聚合为一次 `entries` 注入，
  相邻短章节合并时改用按文件分区的直接上下文，超大章节拆分时使用跨多个分片的
  `shared_entries` 加当前分片的 `local_entries`。不得把不同顶层章节的术语上下文混用。
  完整快照只用于审计和冲突复核，不能修改。没有外部表时也要尊重记录的
  `explicit_none`/`unconfigured` 状态，不得自行加载目录中的术语表。参考术语表快照
  使用 `reference_glossary_*` 名称，不能覆盖权威术语表，也不得反向写回原文件。
  参考表若要为英文正文提供德文概念提示，必须在条目中使用带语言标记的
  `aliases_by_language`（例如 `English: [sublation]`）；命中的条目只作为
  `kind: reference` 的稀疏背景提示，不改变正式术语优先级。
- `translate`、`polish`、`refine`、`extract-entities`、`translate-toc` 只准备交接或
  执行本地处理；命令成功不代表正文已经完成。
- `polish` 会按 `subagent.batching.max_concurrency` 生成
  `polish_worker_handoffs/`；`translate` 使用 `worker_handoffs/`。每个 worker
  只能处理自己 manifest 中的 `assigned_files`。PDF/EPUB `translate` 按 TOC 顺序把
  相邻短章节装入同一个 handoff，受文件/字节/token 和 `max_chapters_per_worker`
  限制；超出限制的章节在章节内部拆分，并且不能与其他章节合并。
  `max_files` 默认值为 5，配置值的有效上限为 8；文件数、字节数和 token 数任一达到
  限制都必须拆批。
  `index` 单元还受 `refine.oversized_unit_split.threshold_tokens_by_type` 与
  `target_tokens_by_type` 的条目级拆分阈值约束；不得用固定字母范围或词典回填替代
  页码、层级和交叉引用校验。
- `chapter_groups` 是语义分组，不等于 worker 数量。`pack_adjacent_chapters: true`
  时，运行时按 `chapter_groups` 的 TOC 顺序连续装箱；`max_chapters_per_worker` 默认
  为 3。单个章节只要需要内部拆分，或包含超过单文件字节/token 限制的文件，就必须
  独立成批；不能为了凑满 worker 跨过它。
  连续且每章不超过 `tiny_chapter_max_tokens` 的极短章节可使用
  `max_tiny_chapters_per_worker` 的更高上限（默认 6），但仍受文件/token 预算约束，且
  一旦混入普通章节就回到 `max_chapters_per_worker`。
- 章节 worker 的术语上下文按 handoff 类型读取：单章节使用 `entries`，单个大章节
  的分片使用 `shared_entries` + `local_entries`，相邻多章节 worker 使用 `files` 映射，
  只能应用匹配文件的条目。相邻章节可以共享同一个 Subagent 的风格上下文，但不得把
  一个章节的术语广播到另一个章节。
- `worker_handoffs/` 中各 scoped manifest 的 `assigned_files` 是执行权限边界；父级
  manifest 的 `pending_files`、`batch_queue` 和 `chapter_groups` 只用于审计和恢复，不能
  作为某个 worker 额外读取文件的授权。`chapter_ids`/`chapter_file_counts` 是合并 worker
  的必要章节元数据，不要把完整全书章节列表复制进 scoped manifest。
- 父级 manifest 保留全书文件、统计、章节映射和上下文哈希，供审计与恢复使用；worker
  manifest 只能是当前 worker 的最小投影（当前文件、当前批次、当前文件统计/层级/术语哈希
  和必要的章节元数据），不得复制全书 `file_stats`、`chapter_groups`、推荐队列或审计快照路径。
  章节术语上下文只记录 `chapter_file_count` 或 `chapter_file_counts`，不得重复写入完整
  `chapter_files` 列表。
- 不删除源文件、输出目录或已有中间结果。额度中断或失败时先校验，再使用原命令的
  `--resume`，只处理 pending 项。
- 新 manifest 的 `--resume` 按单元比较 `unit_context_sha256` 和该单元的精确 TOC 上下文；
  只有源文件、校验 checkpoint 和该单元上下文都未变化时才复用。全书上下文哈希变化不会
  自动使所有单元失效；缺少单元哈希的旧 manifest 无法安全定位影响范围时，才采用整批重做
  的兼容兜底。
- 中文目标语言的普通正文翻译必须通过目标语言内容审计：高置信度的原文长段落未变化、
  中文密度明显不足或模型敷衍套话都会进入失败报告。`bibliography` 和 `index` 文件
  不使用中文密度门禁，但仍保留数字标记与拒答检测。
- 本地校验报告中的 `safety_blocked`、拒答或免责声明不得进入 `validated`，也不得通过打包。
- 阻断必须区分处理：`retry_required` 是 Subagent 通常可以修复的明确错误，主 Agent 应直接按清单
  重新派发；首次出现的 `review_required`（疑似原文引用、疑似页边装饰等）也先用 `--resume`
  交给 Subagent 复核；同一文件在该轮复核后仍触发 review 信号时，报告进入
  `human_review_required`，必须停止自动重试并询问人工。只有确认是合法例外并完成复核后，才可显式
  使用对应校验命令的 `--allow-review-warnings`，且该次放行会写入报告和 checkpoint。信息性记录
  （如 TOC 的安全格式规范化、已确认的重复标题消歧和高置信度参考文献标题修复）不属于阻断。
  人工明确要求再试时，才可在准备命令上使用 `--retry-after-human-review`；该开关只允许再次
  派发 Subagent，不等于接受 warning。

标准调度循环：

```text
本地准备 → 检查 manifest/Prompt → 打开工作区 Subagent →
Subagent 直接写文件 → 单文件校验 → 收集完成结果 → 下一批/失败重试 →
全量校验 → 打包
```

### 1.1 主 Agent 的快速执行表

开始 PDF 任务时先看 `output/<title>/pdf_text_probe.json` 和
`pages/ocr_progress.json`，再决定证据路径。`ocr.secondary.enabled` 只控制视觉 OCR
PDF 的单 OCR/双 OCR；它不能把原生文字 PDF 变成双 OCR：

| 页面来源 | 配置 | 实际证据模式 | 处理方式 |
|---|---|---|---|
| `native_text` | 忽略 `ocr.secondary.enabled` | 原生 PDF layout；报告兼容字段为 `single_ocr` | 不运行视觉 OCR、Paddle、`ocr-correct` 或共识检查；使用 `pages/page_*.ocr.json` 的 PDF 坐标和字体证据。 |
| 视觉 OCR | `false` | `single_ocr` | 使用 `pages/` 的主 OCR；忽略旧的 `ocr_consensus.json`，不运行 OCR 纠错。 |
| 视觉 OCR | `true` | `two_ocr` | 必须配置 `ocr.secondary.backend`；`ocr-pages` 生成次 OCR 和当前共识报告，差异页进入 Subagent 复核。 |

原生文字 PDF 的判定由 `ocr-pages` 中的 `pdf_text_probe` 保守完成，不要只因 PDF 可以复制
文字就认定它是原生稿。只有高置信度矢量文字层才走 `native_text`；扫描图、图片上叠加的
隐藏 OCR 层和混合稿仍走视觉 OCR。原生路径的关键检查是：

- `pdf_text_probe.json` 的 `classification` 为 `native_text`；
- `pages/ocr_progress.json` 的 `mode` 和 `backend` 为 `native_text`；
- 每一页都有 `pages/page_NNN.ocr.json`，sidecar 的 `source_kind` 为 `native_text`、
  `coordinate_system` 为 `page_points`；
- 即使配置中残留 `ocr.secondary.enabled: true`，也不要求 `ocr_consensus.json`，并且
  `footnote-prepare` 只使用原生 sidecar。

当前支持的双 OCR 组合是 Chandra + Paddle。Paddle 输出与 Chandra 对齐的 layout sidecar；
它只在“页底几何位置 + 明确数字开头”足够可靠时生成 `Footnote`/`footnote-def`，并把脚注定义
规范化成 `[^N]: ...`。它不会把普通上标、序数或行内数字引用臆测成脚注，也不生成 Chandra
的图片描述。两套 OCR 的文本、脚注标签、编号和垂直范围仍然独立比较；任一差异都应进入视觉复核。

对视觉 OCR，脚注和整页插图读取同一个开关。原生文字 PDF 使用独立的原生版面证据；双 OCR 模式下，`footnote-prepare` 和
`illustration-prepare` 必须看到当前共识检查点；主/次 OCR 的候选差异不能由本地脚本自动
选择，必须进入工作区 Subagent。切换模式后，旧的脚注决定、插图绑定和 `refine-local`
检查点不能复用。

脚注和整页插图共享 `pdf2epub/refine/pdf_evidence.py` 的页面来源/证据模式判断，以及
`pdf2epub/refine/layout_evidence.py` 的 sidecar 文本和坐标归一化；不要在新的 PDF 阶段复制
这两类逻辑。共享层只提供证据事实，脚注/插图模块仍分别负责自己的候选语义和 Subagent 决定。

PDF 结构阶段按以下顺序执行：

```text
ocr-pages
→ [two_ocr: ocr-correct → ocr-correct-validate]
→ refine-prepare → 工作区 Subagent 写 toc_tree.json
→ illustration-prepare
→ [有候选: 工作区 Subagent 写 illustration_decisions.json → illustration-validate → illustration-apply]
→ refine-local
→ footnote-prepare
→ [有待复核: 工作区 Subagent 写 footnote_decisions.json → footnote-validate]
→ footnote-apply
→ polish → polish-validate
```

`illustration-prepare` 必须在 `refine-local` 前完成，因为它影响页面合并；
`footnote-prepare` 必须在 `refine-local` 后完成，因为它按实际生成的 TOC 单元归并章末脚注。
没有候选时仍保留本地生成的报告和 manifest，并继续执行下一个阶段。

对所有 PDF，`polish` 都是翻译前的必经质量闸门，不是可选的版式优化；只有
`polish-validate` 通过后，才能提取实体表或准备正文翻译。高置信度原生矢量文本 PDF
只跳过视觉 OCR，仍必须用 `polish` 判断视觉换行与真实段落边界。原生文字稿的 polish
不得进行无依据的拼写或字形改写，重点是合并软换行并保留真实段落、标题和块级结构。
`polish-validate` 还会报告高置信度的残留页码/页边行；这些候选默认进入
`review_required`；首次出现时必须先让 Subagent 重新判断，复核后仍存在则升级为
`human_review_required`，不授权本地脚本自动删除或继续。仅在明确确认合法保留后使用
`--allow-review-warnings`。
EPUB、轻小说和 TeX 流程不使用这一 PDF 润色阶段。

PDF 的具体循环为：`ocr-pages →（若启用第二套 OCR：ocr-correct → ocr-correct-validate）→ refine-prepare → illustration-prepare →（必要时 illustration-validate → illustration-apply）→ refine-local → footnote-prepare →（必要时 footnote-validate）→ footnote-apply → polish → polish-validate`。
随后执行 `extract-entities → translate-toc → translate-toc-validate → translate →
translate-validate → build-epub`。可搜索但由扫描图像叠加 OCR 文字层的 PDF 仍必须重新
视觉 OCR。

PDF 纯转换模式使用 `pipeline: epub_conversion`（兼容别名
`mode: ocr_to_epub`），循环为：
`ocr-pages →（若启用第二套 OCR：ocr-correct → ocr-correct-validate）→ refine-prepare → illustration-prepare →（必要时 illustration-validate → illustration-apply）→ refine-local → footnote-prepare →（必要时 footnote-validate）→ footnote-apply → polish → polish-validate → build-epub`。
该模式不读取语言设置，不执行实体提取、翻译 TOC 或正文翻译；但 polish 仍是所有 PDF
必须通过的结构质量门禁。构建时不得使用 `build-epub --translated`。

统一的 pipeline 能力和门禁定义位于 `pdf2epub/pipeline_policy.py`。命令模块不得重新
实现 `pipeline`/`mode` 的特殊判断；需要新增流程能力时先更新该策略对象，再更新对应
的命令合同和测试。

并发任务必须各自使用 worker handoff 中的 `assigned_files`。超过 30,000 字节的单元必须
独立成批。TOC 必须由正文翻译前的独立 Subagent 完成，正文 worker 不得修改翻译 TOC。
PDF 正文 Prompt 还会从已验证的 `toc_tree_translated.json` 生成一个全书方向性轮廓：
它依据实际树深、分支规模和 `subagent.batching.global_toc_tokens`（默认 1,200）
自适应压缩，不固定保留某几个标题级别。该轮廓只用于理解全书主题推进，当前章节的
精确 `toc_heading_contexts` 才是可见标题和措辞的权威来源；完整 TOC 不得重复注入每个章节。
TOC 绑定校验只对连续空白、Markdown 外层标记和成对书名号/引号做规范化容差；近义词或
独立改译仍必须失败。worker Prompt 必须把当前文件的标题锚定清单置于 assigned 任务附近，
要求对应的第一个匹配标题逐字使用 TOC 文本，但不强制其成为物理首行。
  `toc_heading_contexts.binding_mode: container_only` 表示源单元没有可见 TOC 标签（例如
  纯图片封面或边界片段）；此时 EPUB 构建器负责章节容器标题，Subagent 不得在 Markdown
  中凭空新增标题或纯段落标签。续文分片仍保持原样，不生成“续”标题。

## 2. PDF 翻译流程（扫描/混合稿与原生文字稿）

### 2.1 准备、OCR 和结构

1. 检查 `input/` 中的 PDF，在 `config.yaml` 填写 `title`、`input_pdf` 和 OCR 后端；
   翻译模式还需填写源语言、目标语言，纯转换模式只需增加 `pipeline: epub_conversion`；
   不要覆盖用户真实配置。
2. 如需选择外部术语表，先执行 `uv run pdf2epub -c config.yaml glossary-candidates`，
   查看 `output/<title>/glossary_candidates.json`，再明确写入配置；没有明确匹配时留空，
   不得因为目录中存在术语表就自动加载。
3. 执行：

   ```text
   uv run pdf2epub -c config.yaml ocr-pages --resume
   ```

   程序会先生成 `pdf_text_probe.json`。只有高置信度原生矢量文本 PDF 才直接提取文字；
   扫描 PDF、可搜索 OCR PDF 和混合稿都生成视觉 OCR 的 `pages/page_XXX.md`。原生文字稿
   同样生成 `pages/page_XXX.md`，但另外为每页生成 `pages/page_XXX.ocr.json` 原生 layout
   sidecar（`source_kind: native_text`、`coordinate_system: page_points`），供脚注和插图
   使用 PDF 坐标、文本块、字号和字体信息。若
   `pages/ocr_progress.json` 的 `mode` 为 `native_text`，不要再执行任何视觉 OCR。当
   `ocr.secondary.enabled: true` 且 `ocr.secondary.backend: paddle` 时，此命令还会用本地
   PaddleOCR 逐页复核主 OCR，生成 `ocr_secondary/` 和 `ocr_consensus.json`；一致页自动接受，
   只有两个 OCR 有实质差异，或被共同漏检哨兵选中的页面才进入下一步视觉 Subagent。
   哨兵默认每 20 页抽查一页，并把内部文本密度显著低于相邻页的页面列为风险页；这些规则
   只增加复核，不会自动改写页面。`enabled: false` 时只运行主 OCR，不进行 OCR 纠错。
   本地 PaddleOCR 依赖使用 `uv sync --extra ocr-local` 安装。
4. 仅对视觉 OCR PDF 且 `ocr.secondary.enabled: true` 时执行 `ocr-correct`。然后打开工作区 Subagent，读取
   生成的 Prompt，按 `ocr-correct_worker_handoffs/` 中 manifest 的 `assigned_files` 对照同名页图；
   该 handoff 自动只包含 `ocr_consensus.json` 标记的差异页。将纠错后的同名文件写入
   `ocr_corrected_pages/`，并为每页写入 `ocr_correction_reviews/page_NNN.json`，再运行
   `ocr-correct-validate`。校验会拒绝缺少审阅记录、标记为不确定、或比原始 OCR 少行的页面。
   原始 `pages/` 不得覆盖；未通过该校验不得继续。高置信度原生矢量文本 PDF 无论配置如何
   都跳过该阶段；第二套 OCR 开关关闭时也跳过。
5. 执行 `refine-prepare`。然后打开工作区 Subagent，读取
   `output/<title>/refine_subagent_prompt.md`，视觉 OCR 使用经过校验的
   `ocr_corrected_pages/validated/`，原生文字 PDF 使用 `pages/`，写入 `toc_tree.json`。
   Subagent 应从书名页/版权页提取作者和出版社，并按内容标注 `notes`、`bibliography`、
   `index`；普通正文节点不写 `type`。
6. 在 `refine-local` 前处理整页插图候选：

   ```text
   uv run pdf2epub -c config.yaml illustration-prepare
   ```

   若 manifest 状态为 `pending_review`，打开工作区 Subagent，读取
   `illustration_subagent_prompt.md`，只复核列出的候选页及其前后页，并写入
   `illustration_decisions.json`。然后执行：

   ```text
   uv run pdf2epub -c config.yaml illustration-validate
   uv run pdf2epub -c config.yaml illustration-apply
   ```

   `full_page_insert` 才允许改变页面物理顺序；`ordinary_illustration`、`blank_scan` 和
   `body` 不改变顺序。若没有候选，`illustration-validate`/`illustration-apply` 会生成空绑定，
   仍可继续。双 OCR 模式下，主/次 OCR 的候选存在性或 layout 证据不一致会进入复核；单 OCR
   模式只看主 OCR，且忽略旧共识报告。
7. 执行：

   ```text
   uv run pdf2epub -c config.yaml refine-local --resume
   ```

   本地程序校验页码范围、父子关系、兄弟节点重叠，并生成 `ocr_markdown/`。
   `toc_tree.json` 中的 `boundary_info.start_line`/`end_line` 是对应
   `page_XXX.md` 的 1-based 行号，其中 `start_line` 包含该行、`end_line` 不包含该行。
   新章节从页面中部开始时，上一单元保留同页标题前的前缀；父标题和首个子标题同页时，
   两者都必须提供 `start_line`，本地步骤把父标题/导语放入首个子章节单元。
   `tree_progress.json` 会锁定 TOC/OCR 指纹；输入变化后必须重新生成受影响单元。
8. `refine-local` 完成后处理脚注：

   ```text
   uv run pdf2epub -c config.yaml footnote-prepare
   ```

   若 manifest 状态为 `pending_review`，打开工作区 Subagent，读取
   `footnote_subagent_prompt.md`，只处理 manifest 中的候选窗口，并写入
   `footnote_decisions.json`。随后执行：

   ```text
   uv run pdf2epub -c config.yaml footnote-validate
   uv run pdf2epub -c config.yaml footnote-apply
   ```

   对原生文字 PDF，页底以数字开头的文本块只是候选：底部坐标、相对正文的字号、字体名和
   原生上标都是证据，不能单独自动判定为脚注。默认情况下，只有“同页更早位置存在对应
   `<sup>N</sup>` 引用，且脚注块字号不超过正文字号 0.88 倍”的候选本地接受；其余候选
   进入 Subagent 复核。扫描/OCR PDF 的明确高置信度脚注候选也默认本地接受，疑难候选才复核。
   纯数字页码会被本地候选器排除。需要让两种 PDF 都采用最保守模式时，在配置中设置推荐的
   `footnotes.auto_accept: false`。也可以运行 `footnote-prepare --review-all` 临时关闭两种来源
   的本地接受。
   Subagent 必须在脚注、引用、参考文献、普通正文和不确定项之间作出决定，不确定时使用
   `review_required`，不要猜测。原生上标引用在 `pages/` 中保留为 `<sup>…</sup>`，后续
   只有确认存在对应脚注定义时才转换为 `[^N]`。

   `footnote-apply` 只移动已确认的 `footnote_start`、`footnote_continuation` 和
   `footnote_definition`；`citation`、`bibliography` 和 `body` 保持原位。脚注按实际
   `tree_progress.json` 单元的完整 `unit_id` 归并到单元末尾，不使用顶层 TOC 作为唯一范围，
   因而能处理同一章内脚注编号重启。跨页脚注按页面实际顺序拼接，允许出现“正文 → 上一脚注续文
   → 新脚注”，不会默认把续文放到下一页开头。双 OCR 模式会再次检查当前共识报告和次 OCR
   sidecar；原生文字稿的 `ocr_evidence_mode` 虽为兼容性的 `single_ocr`，但其实际证据是
   native layout，不能读取旧的双 OCR 报告。切换页面来源、OCR 模式或 PDF 后，旧的脚注
   决定不能复用。

   脚注阶段至少核对以下产物后才能继续 polish：

   - `footnote_candidates.json`：`source_kind`、`ocr_evidence_mode`、`sidecar_sha256`、
     `review_pages` 与候选数量；原生稿还要确认候选带有 `font_size_ratio`/`font_names` 等
     可用的版面证据；
   - `footnote_subagent_manifest.json`：`status`、`review_candidate_count` 和
     `unit_context_files`；`pending_review` 时不能跳过 Subagent；
   - `footnote_decision_validation.json`：必须是 `valid: true` 且 `status: validated`，
     或明确的 `no_subagent_review_required`；`retry_required` 和 `human_review_required`
     都阻断；
   - `footnote_normalization.json`：必须存在且 `valid: true`、`status: validated`，其源稿、
     候选报告和决定校验哈希必须通过当前性检查；不能只凭 `footnote_normalized/` 中有文件
     判断完成。
9. 所有 PDF 都必须执行 `polish`，打开工作区 Subagent 读取
   `polish_subagent_prompt.md`，并按 `polish_worker_handoffs/` 中各 manifest 的
   `assigned_files` 写入 `polished_markdown/`，然后运行 `polish-validate`。
   若启用第二套 OCR，对 OCR/混合型 PDF，前置 `ocr-correct` 通过校验后的字符、词语和符号视为权威；
   该步骤只处理残留换行、段落边界和块级结构，不再纠正 OCR 字符、拼写或措辞。若仍疑似有 OCR 错误，应退回
   `ocr-correct`，不得在 polish 中改写。若只启用一套 OCR，则没有 page-level 视觉纠错闸门，polish 仍只处理
   结构，不应静默改写 OCR 字符。对原生矢量文本 PDF，该步骤用于从视觉行重建语义段落，同时保留
   原文字符和块级结构。`polish-validate` 还会将源稿与润色稿按忽略换行、Markdown 外层标记和已确认页边装饰的
   方式做内容保真比较；正文 token 或数字标记大量丢失时会阻断。未通过
   `polish-validate` 不得继续实体提取或翻译。
   polish 还必须清除已确认的页眉、页脚、独立印刷页码和人工 OCR 页码标记；
   诸如 `Preface XII` 的短标题加页码组合在确认属于页边装饰后应整行删除。
   正文数字、标题编号、日期、引用、脚注、参考文献和索引中的页码不得删除。
   润色不得把普通粗体、罗马数字、编号或序数上标升级成 Markdown 标题；已确认的
   `<sup>N</sup>` 注脚才可规范化为 `[^N]`。
  当前 TOC 标签会作为 polish 的保护证据；唯一出现且与 TOC 标签相同的源行被删除时，
  必须进入 `review_required`，不能仅因该行位于页首就自动视为页眉。

### 2.2 术语提取和翻译

实体表尚未存在时，确认上一步 `polish-validate` 已通过，再执行以下结构门禁；此时不得
翻译：

```text
uv run pdf2epub -c config.yaml check-ready --stage translate --skip-entities
```

然后：

1. 执行 `extract-entities`，立即打开工作区 Subagent，读取生成的 Prompt 和 manifest，
   写入 `translation_entities.json`。
2. 执行 `extract-entities-validate`。失败时不得继续。
3. 执行 `translate-toc`，打开独立的工作区 Subagent，按目录 Prompt 写入
   `toc_tree_translated.json`，然后运行 `translate-toc-validate`。TOC 必须在正文翻译
   worker 启动前完成；正文 worker 不得修改该文件。
4. 执行完整门禁：

   ```text
   uv run pdf2epub -c config.yaml check-ready --stage translate
   ```

5. 执行 `translate`。该命令会生成 `translate_subagent_prompt.md`、父级 manifest 和
   `worker_handoffs/`。先检查父级 manifest 的 `pending_files`、`batching`、
   `chapter_groups` 和 `worker_handoffs`；随后逐个打开 handoff 对应的工作区 Subagent。
   每个 Subagent 只处理自己 scoped manifest 的 `assigned_files`，同名译文写入
   `translated/`，不得读取或修改其他 worker 的文件。超过 30,000 字节的大单元仍必须
   独立派发；相邻短章节可以在预算内共享一个 worker，但不能把大章节与其他章节合并。
   Prompt 会同时提供一次按预算稀释的全书 TOC 轮廓，以及当前 assigned 文件精确的已翻译
   TOC 标题/子标题上下文；后者对输出标题具有最高权威。术语上下文按 handoff 类型读取：
   合并章节使用 `files` 映射并按文件应用，完整快照只用于审计，不能修改。标题绑定允许
   安全的格式规范化，但不允许近义词替换。
6. 每完成一个单元可运行：

   ```text
   uv run pdf2epub -c config.yaml translate-validate --file <文件名>.md
   ```

   单文件结果只作为 checkpoint，不能替代最终全量校验。
7. 所有单元完成后运行 `translate-validate`。`retry_required` 中的明确错误直接重新交给
   Subagent；首次 `review_required` 也重新派发。若报告出现 `human_review_required`，必须暂停
   并询问人工，不能继续自动重试。只有完成复核后才可显式使用
   `translate-validate --allow-review-warnings` 放行。
10. 执行打包：

   ```text
   uv run pdf2epub -c config.yaml build-epub --translated
   ```

   默认拒绝不完整译文；`--allow-partial` 仅用于明确的预览。

若已有译文只需要修复页眉、页脚或印刷页码，不要直接用脚本改写 `translated/`，也不必
因此重译全书。可使用一次性的 `repair-page-furniture`：本地命令会快照现有译文，生成
Prompt、manifest 和 worker handoff。准备阶段会扫描译文和对应润色稿，只把疑似页眉页脚的
编号、短标题加页码行及重复短行的局部窗口写入 `repair_candidates`；worker 按候选窗口
分批，而不是按全文 token 分批。工作区 Subagent 只删除确认属于页边装饰的内容，并直接
写回同名译文。完成后运行 `repair-page-furniture-validate`，通过后再运行
`build-epub --translated`。该修复不得改写正文、术语、标题、脚注、参考文献或索引页码；
不确定的候选必须保留并报告。候选窗口只是判断提示，不是本地自动删除授权；没有候选的
文件应保持不变。

若确实不需要书内实体表，必须显式使用 `translate --skip-entities`，并让 manifest 记录
这一选择。不得默默跳过实体提取。

### 2.3 PDF 翻译保真规则

- 保持 Markdown 标题层级、公式 `$...$`、脚注 `[^...]`、图片链接和表格结构。
- EPUB 公式目前使用 Unicode 优先、`latex2mathml` 复杂公式回退的 MathML 路径；不要默认引入
  XeLaTeX/`dvisvgm` SVG 渲染或新增系统依赖。只有用户明确要求并完成独立依赖设计时，才可另立任务评估。
- 源文件末尾的 `REFERENCES`、`Literatur`、`Notes` 等若不是标题，译文也不得升级为标题；
  只有高置信度的单个末尾标签可用 `--fix-reference-heading` 修复。
- `bibliography` 必须保留作者、书名、年份、版次、DOI/URL/ISBN、页码和引用标点。
- `index` 必须保留条目层级、页码、页码范围、交叉引用和条目数量。
- `translate-validate` 会额外比对参考文献/索引中的数字标记；发现数字丢失、改写或重排
  时必须返工。普通中文正文中的 `untranslated_source_detected` 和目标语言审计失败是
  阻断条件；普通正文中的 `bilingual_warnings` 首次进入 `review_required` 并重新派发，
  同一文件复核后仍存在则进入 `human_review_required`，不再出现“打印 warning 后无事发生”的
  隐式继续。参考文献和索引仍按其专门规则校验。

### 2.4 PDF 纯转换流程

纯转换模式不需要伪造 `source_language: Chinese` 和 `target_language: Chinese`。
最小配置如下：

```yaml
title: "Your Book Title"
input_pdf: "input/your_book.pdf"
pipeline: epub_conversion
```

完成 `polish-validate` 后可运行：

```text
uv run pdf2epub -c config.yaml check-ready --stage package
uv run pdf2epub -c config.yaml build-epub
```

`extract-entities` 和 `translate-toc-validate` 在该模式下会明确标记为不适用；
`translate`、`translate-validate` 和 `build-epub --translated` 会被拒绝。该模式生成
原语言 EPUB，不生成 `translation_entities.json` 或 `toc_tree_translated.json`。

## 3. EPUB 高保真翻译流程

1. 检查 `input/` 中的 EPUB（也支持 MOBI/AZW3），在 `config_epub.yaml` 填写书名、输入文件、
   源语言和目标语言。外部术语表按第 1 节规则选择。
2. 如需选择外部术语表，先执行 `uv run pdf2epub -c config_epub.yaml glossary-candidates`，
   查看候选报告后再明确写入 `translation.glossaries`；跨语言参考表明确写入
   `translation.reference_glossaries`，只能作为只读参考。
3. 执行：

   ```text
   uv run pdf2epub -c config_epub.yaml html-prepare
   ```

   首次产物包括 `compressed_units/`、mapping、元数据输入/Prompt 和实体提取 Prompt。
4. 立即打开工作区 Subagent，读取实体 Prompt/manifest，写入 `translation_entities.json`；
   完成后再次运行 `html-prepare`，让正文和元数据任务挂载当前实体表及外部术语表。
   确实不需要实体表时才使用 `html-prepare --skip-entities`。
5. 读取第二次生成的 `translate-html_subagent_prompt.md`、父级 manifest 和
   `worker_handoffs/`，打开每个 handoff 对应的工作区 Subagent，将同名译文写入
   `translated_compressed/`。只处理 scoped manifest 的 `assigned_files`。
   正文 worker 按 TOC 顺序把相邻短分支装入同一任务；合并任务使用 `files` 映射并按文件
   应用稀疏术语上下文，大分支拆分时使用 `shared_entries` 和 `local_entries`，不得读取
   或混用其他分支的上下文。不得把多个 HTML 单元合并成一个输出文件。
6. EPUB 正文必须保持非空翻译单元 1:1 对齐；不得在单元内部增加换行；HTML 标签、属性、
   实体、占位符、`<div>` 容器、`<i>` 数量/顺序/嵌套必须原样保留。
7. Subagent 按 `metadata_translation_prompt.md` 写入 `translated_metadata.json`：
   `original_title`、作者和出版社保留原文；译名写入 `translated_title`；目录顺序、href、
   anchor、level 不变；`translated_description` 和 `translated_rights` 保留为顶层字段。
8. 每个文件可运行 `html-validate --file <文件名>`，全部完成后运行全量 `html-validate`。
9. 全量通过后执行：

   ```text
   uv run pdf2epub -c config_epub.yaml build-html-epub
   ```

   如果构建报告出现导航更新 warning，暂停并检查报告；确认可以接受后，才可显式使用
   `build-html-epub --allow-navigation-warnings` 放行。不要默认放行 warning。完成后运行
   项目的最终 EPUB 渲染回归测试。

## 4. 轻小说 EPUB 流程

1. 执行 `translate-novel -i <input.epub>`，生成 `novel_units/`、manifest 和 Prompt。
2. 打开工作区 Subagent，只处理 manifest 的 `pending_files`，写入 `translated_novel/` 和
   `translated_metadata.json`。
3. 作者、出版社、图片标记和段落边界必须保留；运行 `translate-novel-validate` 后才能
   执行 `build-novel-epub`。

## 5. arXiv / LaTeX 流程

1. 执行 `translate-arxiv <source>`，打开工作区 Subagent，按
   `.pdf2epub/tex_subagent_prompt.md` 和 manifest 处理 `pending_units`，写入
   `translated_tex_units/`；不得修改 `source/`。
2. 执行 `translate-arxiv-validate --output-dir <run_dir>`；通过后才交付编译产物。

## 6. 断点、失败和打包禁令

- 额度耗尽、模型 500、Subagent 超时或输出截断：保留已有文件，先运行对应校验，再用
  `--resume` 重建 manifest；只重试 pending 或 invalid 项。
- 不要凭目标文件非空判断完成；必须有同源文件 SHA-256 匹配的本地校验记录。
- 实体表、TOC、术语上下文或源稿哈希变化后，旧 checkpoint 不得复用。
- 原生文字 PDF 若是旧运行生成的、没有 `pages/page_*.ocr.json` 的页面，只能重新运行
  `ocr-pages --resume` 生成 sidecar；不能把旧的 `pages/*.md` 当作有版面证据的原生稿。
  如果 PDF、页源、TOC 或已确认插图绑定发生变化，按顺序重新检查
  `illustration-validate`/`illustration-apply`、`refine-local --resume`、
  `footnote-prepare`、`footnote-validate` 和 `footnote-apply`，不要直接复用旧的
  `footnote_decisions.json` 或 `footnote_normalization.json`。
- 出现 `No page layout sidecars found`、`source_kind` 不一致、sidecar 哈希过期、原生
  page/block 地址无法唯一定位或缺少正文命中时，保留现有产物，先修复源阶段并用原命令的
  `--resume` 重建；不要手工编辑决定文件来绕过校验。
- 元数据 JSON 必须整体重写为合法 JSON；拒答/免责声明、缺失文件、结构不匹配或全量
  校验失败时禁止打包。
- 所有写入 JSON、manifest、Prompt 索引的工作区相对路径必须使用 `/`；读取历史产物时
  可以兼容 Windows `\\`，但不得继续生成反斜杠路径。

## 7. 安全、Git 和 Windows 约定

- 严禁硬编码未经授权的内部项目 ID、伪造请求头或冒用 `vertex_adc.json`。
- 不从 Python 调用翻译 API；所有翻译和结构判断使用 Antigravity 工作区 Subagent，
  消耗官方 IDE 会话配额。
- 工作区内的编辑和测试可自动执行；仓库外路径或不可逆操作先确认。
- 本仓库直接维护当前主分支，不创建临时分支；用户明确要求上传 GitHub 时才提交并推送 fork。
- Windows 普通命令优先使用 Git Bash；复杂文本/JSON 处理优先使用 UTF-8 Python；不要把
  复杂 Bash 语法传给 PowerShell，也不要为一次性翻译交接制造临时脚本。

## 8. 本地临时文件归档约定

- 临时脚本、翻译实验、中间切片、比对结果、审计报告和一次性调试输出统一放在
  `.work/<task>/`，不得散落在仓库根目录。`.work/` 是本地工作台，不进入 Git。
- PDF/EPUB 流程自身的 scratch 文件继续放在 `output/<title>/scratch/<task>/`；不得把流程
  生成的中间结果复制到根目录或提交到源码目录。
- `scripts/` 只放可复用、经过测试并准备纳入仓库的工具；一次性翻译脚本放 `.work/`。
  `tests/` 只放可重复运行的测试，不放临时验证脚本或数据快照。
- 正式术语表、配置模板、源码、文档和测试不属于临时文件；尤其不能因为“看起来像数据”
  就把 `glossaries/`、`input/` 或 `output/` 内容移动到工作台。
- 现有历史临时文件迁移前必须先确认用途；迁移应使用可恢复的移动并保持任务子目录，
  不得用清理命令批量删除未知文件。
