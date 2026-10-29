# pdf2epub Agent 操作手册

本文件面向实际操作仓库的主 Agent 和工作区 Subagent，是**书稿流程任务**的执行规范源。
Agent 应先判断任务类型，再按对应文档执行；不要把 README 或维护文档中的示例当成更高
优先级的操作规则。仓库维护任务的边界见第 0.0 节和 `docs/maintenance.md`。

`README.md` 面向使用者，只介绍能力、安装和快速开始；`docs/architecture.md` 与
`docs/antigravity-workflow.md` 面向维护者，记录模块边界、产物合同和实现细节。修改流程、
配置、产物或后端时，三层文档都要同步检查，但本文件的执行规则优先。

## 0.0 书稿流程任务与仓库维护任务的分界

本文件同时服务两类任务，执行前必须先判断类型：

- **书稿流程任务**：翻译、润色、OCR 结构判断、TOC 判断、术语提取、元数据翻译、脚注/
  插图决定，以及写入 `translated/`、`polished_markdown/`、`translated_compressed/` 等
  译文目录。这类任务必须遵守本文件全部规则，特别是第 0 节的 Subagent 总闸。
- **仓库维护任务**：修改源码、测试、文档、CI、工具、配置模板，或修复 bug、重构模块。
  这类任务**不受 Subagent 总闸约束**，编码 Agent 可以直接读取和修改 `pdf2epub/`、
  `tests/`、`docs/` 等文件；不需要为了改代码打开 Antigravity 工作区 Subagent，也不要
  因为 Codex/OpenCode 的默认习惯创建 `codex/*` 或临时分支。完整规则见
  `docs/maintenance.md`，短入口见根目录 `opencode.md` / `codex.md`。

- **经用户明确授权的现有产物格式修复**：只修已有 EPUB、旧译稿或基于旧译文重建 EPUB 的
  显示/格式问题，不做新翻译、语义润色、OCR 纠错或新生产中的全书结构判断时，按 Codex 仓库维护任务处理，
  不要求使用 Antigravity 工作区 Subagent。Codex 可读取故障定位与验收所需的相关正文/产物，
  并可依据页图、TOC 和上下文证据核对页眉、段落边界、脚注归属等既有 EPUB 的局部显示问题；
  使用 Python、CLI 或底层构建器在 `.work/` staging 或独立文件中修复。保留原稿和原产物，
  不伪造 checkpoint，也不静默绕过门禁。该例外不适用于新书稿生产或未经授权的产物修改。

书稿流程任务不得由主 Agent 直接读取、翻译或润色 `output/` 中的正文；维护任务及上述明确
授权的现有产物格式修复可按其范围读取必要内容。两类任务均不修改 `config*.yaml`、`input/`、
`output/`、`.secrets/`（除非用户明确要求）；Git 只维护 `main`，不自动建分支。新书稿流程不自动
提交或推送。Codex 仓库维护按 `codex.md` 在主 Agent 独立验收后自动提交本次任务。push 在任何
流程中都须人类另行明确授权。无法判断任务类型时，先做只读检查并向用户确认。

Codex 仓库维护任务和经授权的现有产物格式修复不受后续第 0 节 Subagent 总闸及其“每次开工
检查”清单限制；它们可按上文说明直接使用 Codex 工具完成。新生产中的 OCR/TOC 结构判断、
新翻译、语义润色，以及脚注/插图的内容或语义决定，仍须遵守本手册并使用 Antigravity 工作区
Subagent。既有 EPUB 故障定位所需的局部版面核对按授权维护范围处理。

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

开始新任务或恢复中断任务前，可先运行只读诊断：

```text
uv run pdf2epub -c config.yaml status
uv run pdf2epub -c config.yaml status --json
uv run pdf2epub -c config.yaml doctor
```

`status` 只读取当前阶段、哈希和验证报告，不创建恢复产物；`doctor` 检查配置、输入文件、
选中的 OCR 依赖和配置兼容性。页面 OCR 只能使用注册表中当前存在的后端；未知、退役或遗留
的后端配置会在 `doctor` 阶段阻断，不能靠关闭开关绕过。

## 0.2 依赖和 OCR 后端快速配置

主项目环境只安装 `pyproject.toml` 中的依赖：

```text
uv sync
```

当前推荐的 PDF OCR 后端是 Chandra。可按需配置其他已注册的 OCR 后端；本地 OCR 不再额外
启动独立版面检测或 GPU worker。

配置至少应包含：

```yaml
ocr:
  backend: chandra
  secondary:
    enabled: false
footnotes:
  mode: auto
```

新建或复制配置时应显式保留 `footnotes.mode: auto`。`auto` 根据 TOC 结构判断处理方式；只有确认该书是集中尾注而非页底脚注时，才可考虑 `disabled`，其他情况或判断不确定时均使用 `auto`。确认存在尾注本身不代表应禁用：通常让 `auto` 将其判为 `endnotes_only`。

所有 PDF 新运行一律使用视觉 OCR，不存在 `pdf_text.mode` 或其他页面来源开关。`pdf_text`
配置已删除；配置中残留该字段时，配置加载和 `doctor` 必须明确报错，提示该字段已删除，
不得静默忽略。`pdf_text_probe.json` 只做分类审计：`classification`/
`detected_recommendation` 记录文字层特征，`recommendation` 始终是 `ocr_required`，
probe schema 为 3，旧 schema 不复用。旧版本生成的 `source_kind: native_text`
sidecar 和 `use_text_layer` probe 仅用于读取/恢复旧输出，新运行不再产生它们。

需要双 OCR 时配置一个不同于主 OCR 的已注册后端，并将 `ocr.secondary.enabled` 改为 `true`。
次 OCR 的文本和 sidecar 只作为复核证据，不覆盖主 OCR。关闭第二套 OCR 后，旧的双 OCR 共识
不得复用。

启用双 OCR 后，`ocr-pages` 必须在主 OCR 页面 worker 启动前生成
`ocr_secondary_preflight.json`，按页数和配置的 `ocr.secondary.performance` 页速估算次 OCR
耗时。超过 `max_estimated_seconds` 时必须先暂停；只有用户明确使用
`ocr-pages --allow-slow-secondary` 才能继续。

共识报告、脚注 sidecar 和当前性合同见 `docs/antigravity-workflow.md` 与
`docs/architecture.md`。

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
- [ ] 若双 OCR 开启，已完成 `ocr-correct-validate`；单 OCR 发现解说异常时，已对异常页完成相同的校验；
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
  `max_files` 默认值为 5，配置值的有效上限为 8；普通单章节的文件数、字节数和 token
  数任一达到限制都必须拆批。相邻章节合并 worker 可单独使用
  `max_packed_source_tokens`，但不会放宽单文件字节上限或单章节拆分规则。
  `polish` 的普通章节仍可适度打包，但使用独立的
  `subagent.batching.polish_max_files`（默认 `min(max_files, 4)`，有效上限为 8）。
  `index`、`bibliography`、`notes`、`author_notes`、`toc`、`catalog`、`ad`、
  `copyright`、`frontmatter`、`metadata` 和 `cover` 等特殊角色会强制隔离为独立
  batch 和 worker，不与普通章节或其他特殊角色混排；对应 worker 的 prompt 会高亮
  条目 1:1、短行和标识符保留规则。
  `max_nonempty_lines` 默认值为 350；EPUB 的密集单元超过该值时在准备阶段生成有序的
  `.partN.md`，每个 part 单独交给 worker，最终构建前再按原 mapping 合并。`index` 和
  `bibliography` 即使未超行数预算也不得与相邻章节合并。
  `index` 单元还受 `refine.oversized_unit_split.threshold_tokens_by_type` 与
  `target_tokens_by_type` 的条目级拆分阈值约束；不得用固定字母范围或词典回填替代
  页码、层级和交叉引用校验。极短尾片只可在硬 token/字节预算内并入前片；否则只在
  合法块边界重平衡最后两片，不能突破当前章节目标预算来减少 worker 数。
- `chapter_groups` 是语义分组，不等于 worker 数量。`pack_adjacent_chapters: true`
  时，运行时按 `chapter_groups` 的 TOC 顺序连续装箱；`max_chapters_per_worker` 默认
  为 3。普通相邻章节合并时使用 `max_packed_source_tokens`（默认是
  `max_source_tokens` 的两倍），但每个章节仍必须先在普通预算内完整成批。单个章节只要
  需要内部拆分，或包含超过单文件字节/token 限制的文件，就必须独立成批；不能为了凑满
  worker 跨过它。
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
- 翻译 worker 必须按 manifest 的 `atomic_output_contract` 先写入临时同级文件，再原子替换
  正式目标；校验器会清理遗留临时文件，并把无效/半截目标移到 `scratch/<task>/invalid_outputs/`
  供恢复诊断。`--resume` 重建 handoff 时，所有 assigned 文件均已有当前校验 checkpoint 的旧
  handoff 标记为 `completed`，源哈希已变化的旧 handoff 标记为 `superseded`，不得再次派发。
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
`pages/ocr_progress.json`。所有新 PDF 运行都走视觉 OCR；probe 的
`classification`/`detected_recommendation` 只用于审计，`recommendation` 始终是
`ocr_required`。`ocr.secondary.enabled` 只控制视觉 OCR 的单 OCR/双 OCR：

| 页面来源 | 配置 | 实际证据模式 | 处理方式 |
|---|---|---|---|
| 视觉 OCR | `ocr.secondary.enabled: false` | `single_ocr` | 正常页使用 `pages/` 主 OCR并忽略旧共识；检测到疑似 OCR 解说而非转录时，只把异常页交给现有 `ocr-correct` 视觉复核，其他页作为未复核直通副本。 |
| 视觉 OCR | `ocr.secondary.enabled: true` | `two_ocr` | 必须配置 `ocr.secondary.backend`；`ocr-pages` 生成次 OCR 和当前共识报告，差异页进入 Subagent 复核。 |
| 旧原生产物（legacy） | 无新配置入口 | 原生 PDF layout；报告兼容字段为 `single_ocr` | 仅用于读取旧运行：`pages/ocr_progress.json` 为 `native_text`、每页 sidecar 的 `source_kind` 为 `native_text`。不运行视觉 OCR、`ocr-correct` 或共识检查。 |

已有完整的 `pages/`、`ocr_secondary/` 和次 OCR sidecar，且只是修改了共识判定规则时，按以下
恢复路径执行，不要重新 OCR：

1. 确认配置仍为视觉 PDF 且 `ocr.secondary.enabled: true`，然后运行
   `uv run pdf2epub -c config.yaml ocr-consensus-rebuild`。
2. 检查 `output/<title>/ocr_consensus.json` 的 `complete: true`、`failed_pages: []`，并按每条
   record 的 `action` 统计 `auto_accept` 与 `visual_review`；先区分文本差异和布局粒度差异，
   不要把单纯的 sidecar 几何差异当成整页 OCR 错误。
3. 运行 `ocr-correct`，只把 `action: visual_review` 对应的 `assigned_files` 交给工作区
   Subagent；Subagent 必须对照页图写入 `ocr_corrected_pages/` 和逐页 review JSON，再运行
   `ocr-correct-validate`。双 OCR 只处理 `visual_review`；单 OCR 只处理 anomaly report 中的
   异常页，不得把次 OCR 文本直接覆盖主 OCR。异常页输出须明确记录图像支持的转录或经图像确认的
   原文例外；不确定时保持阻断。
4. 只有 `ocr-correct-validate` 通过后，才继续 `refine-prepare` → 目录 Subagent 写
   `toc_tree.json` → `illustration-*` → `refine-local` → `footnote-*`。`footnote-prepare`
   依赖当前 `tree_progress.json`，不能在 `toc_tree.json`/`refine-local` 完成前提前运行。

`ocr-consensus-rebuild` 只重算既有产物，不启动 OCR 后端；缺页、缺 sidecar、源 PDF 哈希
或次 OCR 配置不匹配时必须失败并回到 `ocr-pages --resume`，不得以旧报告或文件存在性强行继续。

原生文本输入路径已删除，不再有 `pdf_text.mode` 或 `use_text_layer` 的新运行分支。
即使 PDF 可以复制文字，`ocr-pages` 也一律使用视觉 OCR；probe 只把文字层特征写入审计字段，
schema 3 的 `recommendation` 始终是 `ocr_required`。

旧原生产物只读兼容：旧运行可能留下以下组合，refine、脚注和插图阶段仍可读取它们来恢复
旧输出，但不得据此为新 PDF 选择原生输入，也不得生成新的 native 页面：

- 旧 `pdf_text_probe.json` 的 `recommendation` 为 `use_text_layer`；
- `pages/ocr_progress.json` 的 `mode` 和 `backend` 为 `native_text`；
- 每一页都有 `pages/page_NNN.ocr.json`，sidecar 的 `source_kind` 为 `native_text`、
  `coordinate_system` 为 `page_points`；
- 即使配置中 `ocr.secondary.enabled: true`，旧原生运行也不要求 `ocr_consensus.json`，
  `footnote-prepare` 只使用原生 sidecar。

双 OCR 只作为可选复核通道。主/次 OCR 的文本、数字和 sidecar 差异不能由本地脚本自动选择，
必须交给工作区 Subagent；次 OCR 的连续物理行可以合并为一个复核窗口。

对视觉 OCR，脚注和整页插图读取同一个开关。旧原生运行使用独立的原生版面证据（legacy
artifact only）；双 OCR 模式下，`footnote-prepare` 和 `illustration-prepare` 必须看到当前
共识检查点；主/次 OCR 的候选差异不能由本地脚本自动选择，必须进入工作区 Subagent。切换
页面来源或 OCR 模式后，旧的脚注决定、插图绑定和 `refine-local` 检查点不能复用。

脚注和整页插图共享 `pdf2epub/refine/pdf_evidence.py` 的页面来源/证据模式判断，以及
`pdf2epub/refine/layout_evidence.py` 的 sidecar 文本和坐标归一化；不要在新的 PDF 阶段复制
这两类逻辑。共享层只提供坐标和文本事实，脚注/插图模块仍分别负责自己的候选语义和 Subagent
决定。

PDF 结构阶段按以下顺序执行：

```text
ocr-pages
→ [two_ocr: ocr-correct → ocr-correct-validate]
→ refine-prepare → 工作区 Subagent 写 toc_tree.json
→ illustration-prepare
→ [有候选: 工作区 Subagent 写 illustration_decisions.json]
→ illustration-validate → illustration-apply
→ refine-local
→ footnote-prepare
→ [有待复核: 工作区 Subagent 写 footnote_decisions.json]
→ footnote-validate → footnote-apply
→ polish → polish-validate
```

`illustration-prepare` 必须在 `refine-local` 前完成，因为它影响页面合并；
`footnote-prepare` 必须在 `refine-local` 后完成，因为它按实际生成的 TOC 单元归并章末脚注。
没有候选时仍保留本地生成的报告和 manifest，并继续执行下一个阶段。

对所有 PDF，`polish` 都是翻译前的必经质量闸门，不是可选的版式优化；只有
`polish-validate` 通过后，才能提取实体表或准备正文翻译。旧原生文字运行也必须通过
`polish` 判断视觉换行与真实段落边界；其 polish 不得进行无依据的拼写或字形改写，重点是
合并软换行并保留真实段落、标题和块级结构。
`refine-local` 的页面合并器会保守地拼接跨页未完的普通正文；完整句子、标题、列表、脚注、
图片和表格仍保持块边界。Markdown→XHTML 默认遵循标准 Markdown 语义：单换行是软换行，
空行才创建新段落；只有显式 `<br />` 才生成硬换行。
正文若由连续的长编号段落组成，应依据上下文确认它们是普通论述后将数字标记转义为 `17\.`，使每段保留原编号且输出为普通段落；真实枚举仍用 Markdown 列表。`translate` 必须保留源稿中已转义的编号标记和顺序，校验会核对这些标记。
视觉 OCR sidecar 若在页顶将跨页重复的 `Section-Header` 标为章节标题，页面合并器会按共享坐标归一化和去除印刷页码后的重复文本识别页眉；只处理至少出现在两页的短块，正文位置的真实标题保留。润色交接还要求标题保持独立，不得把后续正文拼入标题。
`polish-validate` 还会报告高置信度的残留页码/页边行；这些候选默认进入
`review_required`；首次出现时必须先让 Subagent 重新判断，复核后仍存在则升级为
`human_review_required`，不授权本地脚本自动删除或继续。仅在明确确认合法保留后使用
`--allow-review-warnings`。
EPUB、轻小说和 TeX 流程不使用这一 PDF 润色阶段。

PDF 的具体循环为：`ocr-pages →（若启用第二套 OCR：ocr-correct → ocr-correct-validate）→ refine-prepare → illustration-prepare →（有候选时交给 Subagent）→ illustration-validate → illustration-apply → refine-local → footnote-prepare →（有待复核时交给 Subagent）→ footnote-validate → footnote-apply → polish → polish-validate`。
随后执行 `extract-entities → translate-toc → translate-toc-validate → translate →
translate-validate → build-epub`。可搜索但由扫描图像叠加 OCR 文字层的 PDF 仍必须重新
视觉 OCR。

PDF 纯转换模式使用 `pipeline: epub_conversion`（兼容别名
`mode: ocr_to_epub`），循环为：
`ocr-pages →（若启用第二套 OCR：ocr-correct → ocr-correct-validate）→ refine-prepare → illustration-prepare →（有候选时交给 Subagent）→ illustration-validate → illustration-apply → refine-local → footnote-prepare →（有待复核时交给 Subagent）→ footnote-validate → footnote-apply → polish → polish-validate → build-epub`。
该模式不读取语言设置，不执行实体提取、翻译 TOC 或正文翻译；但 polish 仍是所有 PDF
必须通过的结构质量门禁。构建时不得使用 `build-epub --translated` 或
`build-epub --only-translated`。

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
当前 TOC 合同要求每个节点有唯一、显式的 `node_id`；数组路径只表示结构位置，标题文字不作身份。
构建器通过 `tree_progress.json` 和源 Markdown 的共享 ownership projection 确定节点所属的物理文件
及 `visible`/`container_only` 状态，并在对应 part 内放置可见标题锚点（包括实际出现在后代单元中的
结构祖先）。可见标题缺少已绑定锚点时必须阻断 EPUB 构建；容器节点按合同落到所属内容文件或首个
有内容的后代。TOC 更新后须重新完成结构审阅、翻译与校验，不迁移旧树或旧翻译结果。
EPUB 构建器分配子章节锚点时先选精确标题，再接受仅前导编号格式差异；编号只在一侧缺失时，
仅当去编号后的标题正文精确且全书分片中唯一、没有不同编号冲突时才回退。模糊相似标题不绑定，
原有 HTML `id` 保留。
  `toc_heading_contexts.binding_mode: container_only` 表示源单元没有可见 TOC 标签（例如
  纯图片封面或边界片段）；此时 EPUB 构建器负责章节容器标题，Subagent 不得在 Markdown
  中凭空新增标题或纯段落标签。续文分片仍保持原样，不生成“续”标题。大章节拆分时每个 part
  收到独立的 scoped context：子标题只下发到其源标题首次出现的 part，父标题只在可见的 part
  标记 `visible`；所有 part 都找不到源标题的子标题只挂到第一个 part 并标记 `container_only`。
  `container_only` 节点不伪造 Markdown 标题锚点；TOC 层级保留，并导航到所属文件或首个有内容的
  后代。`visible` 锚点必须出现在其投影指定的 part 中并落在对应标题上，缺失时构建阻断。
  HTML TOC 与 NCX 在内容渲染后共用同一份已解析导航树；打包前核对目标文件、fragment 和
  XHTML id。NCX depth 按实际逻辑层级计算，合成的目录入口不额外增加层级。

## 2. PDF 翻译流程（扫描/混合稿）

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

   程序会先生成 `pdf_text_probe.json`。所有新运行都使用视觉 OCR；扫描 PDF、可搜索 OCR
   PDF 和混合稿都生成视觉 OCR 的 `pages/page_XXX.md`。probe schema 为 3，
   `recommendation` 始终是 `ocr_required`。旧版本可能留下 `source_kind: native_text`
   的原生 layout sidecar（`coordinate_system: page_points`），它们只供旧输出恢复读取，
   新运行不再生成，也没有配置入口。若
   `pages/ocr_progress.json` 的 `mode` 为 `native_text`，说明这是旧原生运行；不要再执行
   视觉 OCR。当
   `ocr.secondary.enabled: true` 且配置了有效次 OCR 时，此命令还会逐页复核主 OCR，生成
   `ocr_secondary/` 和 `ocr_consensus.json`；一致页自动接受，只有两个 OCR 有实质差异，
   或被共同漏检哨兵选中的页面才进入下一步视觉 Subagent。
   哨兵默认每 20 页抽查一页，并把内部文本密度显著低于相邻页的页面列为风险页；这些规则
   只增加复核，不会自动改写页面。`enabled: false` 时只运行主 OCR，不进行 OCR 纠错。
   如果只是修改了共识判定规则而已有两套 OCR 产物完整，可运行
   `uv run pdf2epub -c config.yaml ocr-consensus-rebuild` 离线重建共识；该命令不得重新调用
   OCR 后端，也不能绕过当前性校验。
4. 仅对视觉 OCR PDF 且 `ocr.secondary.enabled: true` 时执行 `ocr-correct`。然后打开工作区 Subagent，读取
   生成的 Prompt，按 `ocr-correct_worker_handoffs/` 中 manifest 的 `assigned_files` 对照同名页图；
   该 handoff 自动只包含 `ocr_consensus.json` 标记的差异页。将纠错后的同名文件写入
   `ocr_corrected_pages/`，并为每页写入 `ocr_correction_reviews/page_NNN.json`，再运行
   `ocr-correct-validate`。校验会拒绝缺少审阅记录、标记为不确定、或比原始 OCR 少行的页面。
   原始 `pages/` 不得覆盖；未通过该校验不得继续。旧 native artifact 且
   `recommendation == "use_text_layer"` 的运行跳过该阶段；第二套 OCR 开关关闭时也跳过。
5. 执行 `refine-prepare`。然后打开工作区 Subagent，读取
   `output/<title>/refine_subagent_prompt.md`，视觉 OCR 使用经过校验的
   `ocr_corrected_pages/validated/`，旧原生运行使用 `pages/`，写入 `toc_tree.json`。
   同时读取 `toc_heading_candidates.json` 与 `toc_structure_review_manifest.json`，只依据 manifest
   允许的页面/图像/正文证据完成全页审阅；写入 `toc_structure_decisions.json`，为每页记录覆盖证据，
   并为每个候选记录唯一处置。未决候选、缺失页审阅、节点起始位置与接受候选不一致都会阻断。
   `toc_tree.json` 使用 schema 2，每个节点有稳定唯一 `node_id`；结构数组路径仅用于定位。
   Subagent 应从书名页/版权页提取作者和出版社，并按内容标注 `notes`、`bibliography`、
   `index`；版权页/CIP 页标注 `type: "copyright"`，其他明确的前置页（如献词、题词、
   前言）标注 `type: "frontmatter"`，仅在证据明确时才使用 `type: "cover"` 或
   `type: "metadata"`。这些角色会贯通到 translate/polish 的特殊单元隔离；普通正文节点
   不写 `type`，也不得为了规避普通正文规则而滥用这些角色。
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
   TOC schema 2 的每个可见标题还须由 Subagent 在 `boundary_info.heading_end_line` 标出标题区域的
   exclusive 结束行；它与节点正文边界 `end_line` 是不同字段。`refine-local` 只将审阅确认且与
   TOC 标题精确对应的源行区域物化为 Markdown 标题，再进入 polish；原区域内联标记和脚注引用保留，
   不从相邻正文推断标题范围，也不改写原始 `pages/`。
   新章节从页面中部开始时，上一单元保留同页标题前的前缀；父标题和首个子标题同页时，
   两者都必须提供 `start_line`，本地步骤把父标题/导语放入首个子章节单元。
   `refine-local` 还会从当前页面生成 `pagination_map.json`，并以页码候选加起始页正文标题
   的双重证据保守清洗 TOC 末尾页码；所有修改、未修改候选和源/目标哈希写入
   `toc_title_normalization.json`，证据不足时保持原题名。`tree_progress.json` 会锁定规范化后的
   TOC/OCR 指纹；输入变化后必须重新生成受影响单元。
   视觉 OCR 若把真实 TOC 标题误标为 `Page-Header`/`Page-Footer`，`refine-local` 会从当前
   `pages/page_NNN.html` 或 sidecar 的 `raw_html` 中按 DOM 顺序合并页眉/页脚块，只在规范化
   后与 TOC 标题精确匹配时于内存中恢复标题；同页同标题只恢复一次，证据不足、已存在或存在
   歧义时写入 skipped 而不猜测。恢复结果写入 `toc_heading_recovery.json`，原始 `pages/`
   保持只读。`tree_progress.json` 的指纹包含恢复映射哈希和证据目录指纹；切换正文页面来源
   （如 `ocr_corrected_pages/validated/`）、证据模式或 HTML/sidecar 后不得复用旧恢复报告或
   旧 checkpoint。
   对旧原生文字运行，`refine-local` 仍会只读当前页的 PDF 坐标 sidecar，在不改写原始
   `pages/` 的前提下确定性移除页边 5% 内的 DOI/孤立页码，以及跨页重复的短页眉页脚；
   唯一正文或书目 DOI 不因单次出现而删除。
8. `refine-local` 完成后处理脚注：

   ```text
   uv run pdf2epub -c config.yaml footnote-prepare
   ```

   新建或复制配置时显式写入 `footnotes.mode: auto`；其他情况或注释类型不确定时也使用 `auto`。
   refine 阶段的 TOC Subagent 在 `toc_tree.json` 顶层写入 `footnote_mode`（`footnotes` 或
   `endnotes_only`），本地按 effective mode 执行。只有确认该书是集中尾注而非页底脚注时，才可考虑
   `disabled`；确认尾注存在本身不等于应禁用，通常由 `auto` 判为 `endnotes_only`。`disabled` 会同时
   关闭脚注处理和尾注归一化；仅在确认是纯尾注且用户明确不需要任何脚注/尾注处理时才设置。
   不得为省步骤或规避校验而关闭。也可在配置中显式固定 `footnotes` / `endnotes_only` / `disabled`，
   显式值始终覆盖 TOC 判断。TOC 中标记为
   `toc`、`notes`、`bibliography`、`index`、`author_notes`、`catalog`、`ad`、
   `copyright`、`frontmatter`、`metadata` 或 `cover` 的单元不会把其内容
   当作页面脚注；若这类单元与正文共用一页，优先依据 `boundary_info.start_line`/
   `end_line` 做块级过滤，缺少边界证据时采用保守的整页排除。

   `endnotes_only` 支持集中 Notes 中每章从 1 重新编号的“按章作用域尾注”。`footnote-apply`
   会从 `toc_tree.json` 和 `ocr_markdown/tree_progress.json` 构建只读 scope 目录，按
   Markdown 标题或保守识别的独立纯文本标题把正文引用和 Notes 定义配到具体章。纯文本标题
   必须同时满足：strip 后 1..120 字符、前后为空行或文件边界、不以 Markdown/HTML 标记或
   `N.`/`N)`/`N、` 定义开头、不以句末标点结尾，并与正文 scope 的标题变体精确匹配。
   `Notes to Chapter N` 等页眉和 `Abbreviations` 等未匹配标题不切换 scope；同一标题匹配
   多个 scope 时只记录 ambiguity 并保持当前 scope。scope key 形如 `u4c1`，内部 key 为
   `fnc-<scope>-<number>`，同一 scope 的第 N 个重复定义为 `...-oN`。跨 scope 重复或
   同一 scope 内重复定义的 key 会改写；其他 key 保持原数字。scope 目录只进入
   `footnote_normalization.json` 的报告字段，不写入 output。Notes 的“外层页计数器 +
   内层真实注释号”只在当前 scope 的正文引用集合
   包含内层数字且内层不小于外层时采用内层，否则保留外层，避免把 `3. 2005 was...` 误判为
   第 2005 条。重复 key 没有章标题、引用所在 scope 没有定义、定义 scope 没有正文 span
   或同一 key 有多个定义且引用数超过定义数时必须 fail closed，并提示修复 TOC/章标题（可先检查
   `toc_heading_recovery.json`）后重跑 `footnote-prepare → footnote-validate →
   footnote-apply`；不得改用 `footnotes.mode: disabled` 绕过。一个作用域内只有一个
   定义时可供多个正文引用共同使用；多个定义仍按出现顺序保守配对，超出定义数时停止归一化，
   不作猜测。尾注段落和列表、引文、表格等 Markdown 块应保留边界。
   `endnotes` 报告记录
   `scope_mode`、`scopes`、`scoped_keys`、`double_numbered_definition_count`、
   `duplicate_definition_occurrences` 和 `orphan_scoped_definitions`。EPUB 中的引用和定义
   仍显示原始数字，内部 `fnc-` key 只用于链接和锚点。

   Notes 定义还支持 OCR 产生的可选无序列表标记 `- N.`、`* N.`、`+ N.`；它们与
   `N.`/`N)`/`N：` 等价，由 `footnote-apply` 的本地 normalization 直接改写为
   `[^N]: ...`，不进入 polish 或 translate Subagent。

   集中尾注的定义边界采用混合识别：文件开头、前一行空行、前一行以句末标点结束，
   或前一行是 Markdown 标题的 `N.`/`N)`/`N：`（含可选 `-`/`*`/`+` 标记）行由本地直接接受。前一行以 `:`、`,`、
   `;`、`-` 或普通单词结尾的 numbered punctuation 行写入
   `endnote_definition_ambiguities.json`，只把这些少量歧义行交给 Subagent 判断
   `definition` / `continuation`；`N text` 日期续行仍走既有 `previous_line_blank` 规则，
   Markdown/HTML 定义和 scope 识别不受影响。未对歧义行作出决定时 `footnote-apply`
   fail closed，并在错误中给出 `file:line`，不得把页码或多行引文静默当成新定义。

   若 manifest 状态为 `pending_review`，打开工作区 Subagent，读取
   `footnote_subagent_prompt.md`，只处理 manifest 中的候选窗口，并写入
   `footnote_decisions.json`。当 manifest 的 `endnote_ambiguity_count` 大于 0 时，
   还要读取 `endnote_definition_ambiguities.json`，为每条歧义写入
   `endnote_definition_decisions.json`（`role` 只能是 `definition` 或
   `continuation`；`review_required` 视为未解决）。随后执行：

   ```text
   uv run pdf2epub -c config.yaml footnote-validate
   uv run pdf2epub -c config.yaml footnote-apply
   ```

   标记身份解析保留原始显示标签：数字与圈号维持历史数字别名，星号/匕首号及显式上标字母使用
   分离的 ASCII key，字母区分大小写。裸 `a)`、乘法星号和 Markdown 强调不因形状相似而自动成为脚注；
   重复标记跨空间 lane 时必须提供能包含目标标记的精确 `marker_context`，apply 不按“最后一个匹配”猜测。
   明确 `Footnote` 标签在可靠窄边栏或表格邻接位置进入候选后仍需视觉复核。
   集中尾注的符号/字母扩展仅接受 typed Notes 中显式 `<sup>…</sup>` 定义与正文显式上标引用配对；
   不从裸 `a)` 或普通段首字母推断尾注。
   对旧原生文字运行，页底以数字开头的文本块只是版面事实：`bbox[3]`、它与页底带的相交比例、相对正文的字号、字体名和
   原生上标都要一起看，不能单独自动判定为脚注。扫描/OCR 与旧原生文字运行都先由本地按块间距、
   左右对齐、连续编号和跨块连续性推断页面级区域。清晰的底部编号区域可以本地接受；边界不清、
   无编号续文、正文/脚注重叠、OCR 证据冲突以及疑似漏检会写入
   `footnote_visual_audit_manifest.json`，交给 Antigravity 视觉 Subagent。连续的递增数字开头块
   （如 `18、19、20、21`）默认会向上回溯相邻无编号块，形成一个区域任务；但如果这组数字中已经
   出现明确的 `Footnote` 标签，普通 `Text` 块默认不会再被机械回溯；即使为了覆盖“正文 → 上一脚注续文 →
   新脚注”而保留跨页回溯，低/无跨页证据的普通 `Text` 也只作为 `suppressed_continuation_candidates`
   上下文，不触发视觉任务。明确标注的无编号脚注、具有中/高置信度连续性的块和边界冲突仍交给视觉审核。
   页眉、页脚和纯页码仍会排除；页面图和可用的区域裁剪图会随任务提供。
   `footnote_candidates.json` 还会记录跨页续文的置信度：它综合上一页最后一个物理脚注、当前页候选前的
   最近正文块、当前/上一页的 bbox 对齐、字号/字体、块间距和句首句末连续性。候选会在“延续上一页脚注”
   与“当前页正文”两个假设之间做保守的本地词汇连续性比较；正文块中保留的 `<sup>…</sup>` 引用是
   强反证。只有正文假设有明确优势的普通 `Text` 块才降为上下文，不会静默移动；脚注假设胜出或两者
   接近时仍交给视觉 Subagent。上一页其他脚注只作为审计背景，不参与续文竞争；中/高置信度且最后一个
   物理脚注的文本未明显闭合时，可本地归类为该脚注的 `footnote_continuation`。最后一个脚注已闭合、
   版面证据不足或 OCR 顺序有冲突时仍交给视觉 Subagent，因为“上一页只有一个编号”本身不能证明它
   续页，也不能排除续页。
   `suspected_missed_count` 不再只是报告警告：达到回溯审计条件的漏检块会成为 `audit_candidates`，
   必须由视觉任务确认。默认使用 `footnotes.auto_accept: true`，只把异常区域交给 Subagent；需要
   全部候选复核时再设置 `false` 或运行 `footnote-prepare --review-all`。
   Subagent 必须输出区域级审计和其中每个 review block 的角色，在脚注、引用、参考文献、普通正文和
   不确定项之间作出决定；区域或 block 使用 `review_required` 时校验阻断。原生上标引用在 `pages/`
   中保留为 `<sup>…</sup>`，后续只有确认存在对应脚注定义时才转换为 `[^N]`。

   `footnote-apply` 只移动已确认的 `footnote_start`、`footnote_continuation` 和
   `footnote_definition`；`citation`、`bibliography` 和 `body` 保持原位。脚注按实际
   `tree_progress.json` 单元的完整 `unit_id` 归并到单元末尾，不使用顶层 TOC 作为唯一范围，
   因而能处理同一章内脚注编号重启。跨页脚注按页面实际顺序拼接，允许出现“正文 → 上一脚注续文
   → 新脚注”，不会默认把续文放到下一页开头。双 OCR 模式会再次检查当前共识报告和次 OCR
   sidecar；旧原生文字运行的 `ocr_evidence_mode` 虽为兼容性的 `single_ocr`，但其实际
   证据是 native layout，不能读取旧的双 OCR 报告。切换页面来源、OCR 模式或 PDF 后，旧的
   脚注决定不能复用。
   双 OCR 下若只有次 OCR 发现脚注，Subagent 必须在决定中写入最终可见的 `text`、目标
   `source_file` 和 `primary_disposition`：`absent` 表示主 OCR 确实缺失，`remove` 表示
   主 OCR 有重复块并必须同时给出 `primary_block(s)`。同一脚注编号存在多个正文引用时还要
   给出唯一 `marker_context`；缺少这些信息时 `footnote-apply` 必须阻断，不能静默追加或
   选择最后一个编号。
   `secondary_only_candidates` 会把连续次 OCR 物理行合并成一个窗口：`block` 是窗口首行的
   锚点，`block_indices`/`block_span` 记录覆盖的物理行；决定仍只引用这个首行 `block`，不要
   为窗口内每一行重复写决定。最小的次 OCR 独有脚注决定形状为：

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
   当 `primary_disposition` 为 `remove` 时，必须改为附带 `primary_block` 或
   `primary_blocks`；当同编号 marker 不止一个时附带 `marker_context`。不满足合同的决定属于
   `retry_required`/阻断，不得靠 `--allow-review-warnings` 放行。

   脚注阶段至少核对以下产物后才能继续 polish：

   - `footnote_candidates.json`：`source_kind`、`ocr_evidence_mode`、`sidecar_sha256`、
     `review_pages`、页面区域、`audit_candidates` 与候选数量；旧原生稿还要确认候选带有
     `font_size_ratio`/`font_names` 等可用的版面证据；
   - `footnote_visual_audit_manifest.json`：异常区域、区域 bbox、审计原因、页面图/裁剪图和
     review block 清单；有 `pending_review` 任务时不能跳过视觉 Subagent；
   - `footnote_subagent_manifest.json`：`status`、`review_candidate_count` 和
     `visual_audit_task_count`、`unit_context_files`、`endnote_ambiguity_count` 和审计
     manifest；`pending_review` 时不能跳过 Subagent；
   - `footnote_decision_validation.json`：必须是 `valid: true` 且 `status: validated`，
     或明确的 `no_subagent_review_required`；`retry_required` 和 `human_review_required`
     都阻断；有尾注歧义时还要检查 `endnote_definitions.valid` 和
     `endnote_definition_validation.json`；
   - `footnote_normalization.json`：必须存在且 `valid: true`、`status: validated`，其源稿、
     候选报告、决定校验和尾注歧义/决定哈希必须通过当前性检查；不能只凭
     `footnote_normalized/` 中有文件判断完成。
9. 所有 PDF 都必须执行 `polish`，打开工作区 Subagent 读取
   `polish_subagent_prompt.md`，并按 `polish_worker_handoffs/` 中各 manifest 的
   `assigned_files` 写入 `polished_markdown/`，然后运行 `polish-validate`。
   每个 Worker 写完一个 assigned file 后，必须先运行只读自检
   `uv run pdf2epub -c <config> polish-validate --file <file> --check-only`；该命令只校验，
   不写 checkpoint、不创建或清理 `validated/`，因此多个 Worker 可安全并发。自检不通过时
   Worker 必须在原地修复并重跑。自检通过不等于完成：Worker 全部结束后，主 Agent 仍须运行
   正式的 `polish-validate --file`（或全量 `polish-validate`）写入权威 checkpoint。
   若启用第二套 OCR，对 OCR/混合型 PDF，前置 `ocr-correct` 通过校验后的字符、词语和符号视为权威；
   该步骤只处理残留换行、段落边界和块级结构，不再纠正 OCR 字符、拼写或措辞。普通正文段落内部不得为了视觉排版新增换行；若仍疑似有 OCR 错误，应退回
   `ocr-correct`，不得在 polish 中改写。若只启用一套 OCR，则没有 page-level 视觉纠错闸门，polish 仍只处理
   结构，不应静默改写 OCR 字符。对旧原生矢量文本运行，该步骤用于从视觉行重建语义段落，同时保留
   原文字符和块级结构。`polish-validate` 还会将源稿与润色稿按忽略换行、Markdown 外层标记和已确认页边装饰的
   方式做内容保真比较；正文 token 或数字标记大量丢失时会阻断。未通过
   `polish-validate` 不得继续实体提取或翻译。
   polish 还必须清除已确认的页眉、页脚、独立印刷页码和人工 OCR 页码标记；
   诸如 `Preface XII` 的短标题加页码组合在确认属于页边装饰后应整行删除。
   正文数字、标题编号、日期、引用、脚注、参考文献和索引中的页码不得删除。
   润色不得把普通粗体、罗马数字、编号或序数上标升级成 Markdown 标题；已确认的
   `<sup>N</sup>` 注脚才可规范化为 `[^N]`。
  当前 TOC 标签会作为 polish 的保护证据；唯一出现且与 TOC 标签相同的源行被删除时，
  必须进入 `review_required`，不能仅因该行位于页首就自动视为页眉。已登记的 TOC
  Part/Chapter **Markdown 标题行**在页边装饰检测中豁免，例如 `# PART I`、`## PART II`
  不会因尾部罗马数字被误判为 `running_title_plus_page_label`；TOC 标题带副标题时，冒号前
  的短标签也可作为豁免前缀。纯文本 `PART I` 等重复页眉仍按页边装饰候选检测，不因匹配
  TOC 标题而豁免。`bilingual_warnings`/未翻译长英文段落门禁只在 `translate-validate`
  生效，polish 阶段输入输出同为源语言，不触发该警告。

### 2.2 术语提取和翻译

可选的受控书目/索引处理仅适用于 PDF Markdown translation pipeline，并通过配置显式开启：

```yaml
translation:
  special_units:
    bibliography: controlled
    index: controlled
```

运行普通 `translate` 后，严格可解析的已标注角色单元会列入父 manifest 的
`special_pending_files`，不会进入普通 `worker_handoffs/`。对每个文件分别运行
`special-unit-prepare --kind bibliography|index --file <文件名>.md`，只让工作区 Subagent
填写生成的 JSON 小结果，再运行同参数的 `special-unit-apply`。命令完整形式为
`uv run pdf2epub -c config.yaml special-unit-prepare --kind <bibliography|index> --file <文件名>.md`
与 `uv run pdf2epub -c config.yaml special-unit-apply --kind <bibliography|index> --file <文件名>.md`。
Bibliography 仅翻译标题，条目正文
按源文精确保留；Index 仅按条目ID映射术语，页码、顺序、层级和交叉引用需保持。混合正文、模糊
边界、无法精确确认的交叉引用必须回退普通 `translate`，不得按年份/DOI猜测边界或让实体表代替
索引翻译。apply 后仍须执行单文件及全量 `translate-validate`。该模式不替代 PDF 的强制 polish gate。

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
   `toc_translation_result.json`，然后运行 `translate-toc-validate`。本地校验会从不可变
   的 `toc_tree.json` 合并生成 `toc_tree_translated.json`。TOC 必须在正文翻译 worker
   启动前完成；正文 worker 不得修改这两个 TOC 产物。
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

   单文件校验会检查本文件绑定的可见 TOC 标题（`toc_heading_bindings_local`）；绑定失败时
   该文件会移出 `valid_files`、删除对应的 `translated/validated/` 副本并进入
   `retry_required`。单文件结果仍只作为 checkpoint，不能替代最终全量校验。
7. 所有单元完成后运行 `translate-validate`。翻译未完工时，全量命令会先输出完成度和
   pending 清单，并暂缓 TOC heading binding；只有所有目标文件存在时才运行该终审门，或显式
   使用 `translate-validate --final` 强制检查不完整输出。`retry_required` 中的明确错误直接重新交给
   Subagent；首次 `review_required` 也重新派发。若报告出现 `human_review_required`，必须暂停
   并询问人工，不能继续自动重试。只有完成复核后才可显式使用
   `translate-validate --allow-review-warnings` 放行。
10. 执行打包。翻译流程必须同时构建两个产物：使用正式书名的原稿英文 EPUB 和使用译名的
   译文 EPUB。推荐一次运行：

   ```text
   bash scripts/build_all_epubs.sh config.yaml
   ```

   等价于依次执行：

   ```text
   uv run pdf2epub -c config.yaml build-epub
   uv run pdf2epub -c config.yaml build-epub --only-translated
   ```

   第一条生成使用正式书名的原稿英文 EPUB；第二条只生成使用译名的译文 EPUB，不生成
   英文 `_en.epub`。需要英文 `_en.epub` 时可改用 `build-epub --translated`，它是可选
   的三产物模式：正式书名的原稿英文 EPUB、英文 `_en.epub` 和使用译名的译文 EPUB；
   英文版失败时不会中止，仍会继续尝试译文版。`--only-translated`（别名
   `--target-only`）不触发英文 `_en.epub`，与 `--translated` 同时出现时以
   `--only-translated` 为准，但仍要求当前有效的 polish 和 translate checkpoint。任一
   请求的构建失败时命令最终返回非 0，已成功的产物保留。第一条命令不可省略，否则会缺少
   正式书名的原稿英文版。只运行 `build-epub` 不带 `--translated` 或
   `--only-translated` 会缺少译文 EPUB，不得视为完成打包。
   `pipeline: epub_conversion` 不得使用本脚本，也不得使用 `--translated` 或
   `--only-translated`，只运行一次 `build-epub`。

   默认拒绝不完整译文；`--allow-partial` 仅用于明确的预览。翻译流程中若
   `translate_validation.json` 对当前 polished source 和 `translated/validated/` 仍有效，
   但本次未带 `--translated` 或 `--only-translated`，命令会打印醒目警告说明只构建原稿
   EPUB。该警告不改变默认构建目标，也不替代显式目标选项。

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
  `Volume II`、`Part IV` 等明确卷册语境的罗马数字按数值与阿拉伯数字比较；裸 `I`/`II`
  不作为数字标记，不得凭空添加或改写卷册语义。

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
   `html-prepare` 会记录 `html_prepare_cache.json`；输入 EPUB、压缩单元和行数预算未变化
   时，后续运行复用该缓存，只刷新实体/术语上下文和 worker handoff，不重复解包和压缩。
4. 立即打开工作区 Subagent，读取实体 Prompt/manifest，写入 `translation_entities.json`；
   完成后再次运行 `html-prepare`，让正文和元数据任务挂载当前实体表及外部术语表。
   EPUB 实体提取默认排除 `Index`、`Bibliography`、`Copyright` 和 `Cover` 等确定性附录，
   其余正文章节仍按 manifest 逐文件读取。
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
3. `source/` 快照只有在旁置身份记录、规范化输入身份/内容指纹和源树 SHA-256 均匹配时才可恢复；旧的无记录快照
   仅在本地输入可用且文件树完全相同时兼容恢复。身份不明或输入已切换时命令会失败并
   保留旧快照；请使用新的运行目录继续处理，不要手工覆盖 `source/`。
4. TeX manifest schema 2 只复用当前校验过的单元：源指纹、布局指纹、源/目标语言、主文件和
   `unit_chars` 必须与上次验证一致；每个单元的源哈希和已验证目标文件哈希也必须匹配。
   编译成功后会记录 `validated_units` 与 `validated_target_sha256`，连续 `--resume` 保留这些
   检查点。旧 manifest 没有目标哈希时一律视为未验证，重新交给 Subagent 并运行校验；不能
   仅凭非空译文或旧 `validated_units` 跳过。

## 6. 断点、失败和打包禁令

- 额度耗尽、模型 500、Subagent 超时或输出截断：保留已有文件，先运行对应校验，再用
  `--resume` 重建 manifest；只重试 pending 或 invalid 项。
- 不要凭目标文件非空判断完成；必须有同源文件 SHA-256 匹配的本地校验记录。
- 实体表、TOC、术语上下文或源稿哈希变化后，旧 checkpoint 不得复用。
- 旧原生运行若缺少 `pages/page_*.ocr.json`，新版本无法再生成 native sidecar；保留现有
  产物并用旧版本恢复，或把该页集视为没有原生版面证据，不能用新的视觉 OCR 静默补齐后
  继续复用旧的脚注/插图决定。
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

- 严禁硬编码未经授权的内部项目 ID、伪造请求头或冒用云服务凭据文件。
- 不从 Python 调用翻译 API；所有翻译和结构判断使用 Antigravity 工作区 Subagent，
  消耗官方 IDE 会话配额。
- 工作区内的编辑和测试可自动执行；仓库外路径或不可逆操作先确认。
- 本仓库直接维护当前主分支，不创建临时分支；用户明确要求上传 GitHub 时才提交并推送 fork。
- GitHub 发布只上传源码、测试、配置模板和维护文档。不要使用无选择的 `git add .`：
  明确排除 `.secrets/`、`config.yaml`、`input/`、`output/` 以及根目录的一次性翻译脚本和报告。
  本地凭据读取接口位于 `pdf2epub/local_credentials.py`，密钥和本地运行时不能上传。
- 上传前至少执行 `git status --short`、`git diff --check`、`uv run pytest -q`，并确认
  `git check-ignore -v .secrets config.yaml` 均命中忽略规则。
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
