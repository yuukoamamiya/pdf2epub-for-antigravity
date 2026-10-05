# pdf2epub 架构说明

本文说明仓库当前的代码分层和模块边界，服务于维护、扩展和排查问题。
它不替代执行规范；翻译任务仍以 [`AGENTS.md`](../AGENTS.md) 为唯一执行规范源，
具体操作流程见 [`antigravity-workflow.md`](antigravity-workflow.md)。

## 文档分工

- `AGENTS.md`：给主 Agent 和工作区 Subagent 的强制操作手册，规定权限边界、命令顺序、
  交接方式、失败重试和打包门禁。
- `README.md`：给使用者的项目介绍，说明能处理什么、如何选择流程、需要哪些依赖以及产物在哪里。
- `docs/architecture.md`：本文，记录模块边界、数据契约、依赖方向和可扩展点。
- `docs/antigravity-workflow.md`：记录各阶段的详细交接合同、恢复语义和维护示例。
- `docs/html-translation-improvements.md`：EPUB HTML 路径的专门维护说明。

修改命令、配置或产物格式时，先改实现和测试，再按上述层次同步文档；不要把维护细节全部
塞进 README，也不要把执行规范只写在维护文档里。

## 1. 总体原则

项目采用“本地确定性处理 + IDE 工作区 Subagent 判断/翻译”的架构：

- Python 负责文件发现、拆分、压缩、合并、校验、哈希记录和 EPUB/TeX 打包。
- 需要阅读正文、判断结构、提取术语或翻译的工作交给工作区 Subagent。
- 源文件、原始输入和已通过校验的中间结果不可被普通流程覆盖或删除。
- manifest、prompt、validation report 和源文件 SHA-256 共同构成可恢复的任务合同。
- 各工作流通过共享契约复用能力，但不直接互相调用另一种格式的业务实现。
- PDF 翻译与纯转换共享源稿准备、polish、校验和 EPUB 构建；流程差异由统一的
  `PipelinePolicy` 声明，不在各 command handler 中重复判断。

## 2. 分层结构

```text
cli.py
└── commands/registry.py             argparse 接线
    └── commands/*.py                命令编排与用户可见返回值
        ├── commands/runtime.py      配置、书名、输出目录、日志上下文
        ├── commands/sources.py      PDF 源稿阶段选择
        ├── pipeline_policy.py       PDF pipeline 能力和门禁策略
        └── 领域工作流模块
            ├── refine/              PDF 结构、分页、插图/脚注闸门和单元生成
            ├── html_translation/    EPUB HTML 解析、压缩、校验和重建
            ├── tex_translation/     TeX 项目扫描、编译和源文件解析
            ├── epub/                EPUB 通用构建能力、Markdown→XHTML 和 MathML
            └── ocr/                 页面 OCR 后端、sidecar 和双 OCR 共识

Subagent 合同层
├── markdown_handoff.py              Markdown handoff 生成
├── markdown_subagent_validation.py  Markdown 输出校验和 validated staging
├── markdown_validation.py           结构、脚注、双语和特殊标记规则
├── toc_translation_workflow.py      PDF TOC handoff、合并和校验
├── subagent_runtime.py              模型选择、token 估算和批次 handoff
└── subagent_safety.py               拒答/免责声明检测
```

### 2.1 CLI 层

[`pdf2epub/cli.py`](../pdf2epub/cli.py) 只负责：

- 初始化 UTF-8 标准输入输出和基础日志；
- 创建全局参数和 argparse 子命令容器；
- 调用 `register_command_parsers`；
- 将解析后的参数交给对应的 `args.func`。

命令注册集中在 [`commands/registry.py`](../pdf2epub/commands/registry.py)。注册模块只
定义命令名称、参数和 handler，不实现业务逻辑。这样新增命令时不会再次扩大 CLI 入口。

### 2.2 命令编排层

`commands/` 中的模块负责把 CLI 参数转换成领域服务调用，并统一处理配置、日志、错误码
和下一步提示：

- `runtime.py`：提供 `BookCommandContext`、配置加载和输出目录解析。
- `sources.py`：选择原始 `ocr_markdown` 或已验证的 `polished_markdown`；所有 PDF 的翻译、实体提取和打包都必须使用后者。
- `ocr.py`：执行唯一允许调用 OCR 服务的入口，并可在同一阶段运行本地 PaddleOCR 共识筛查；只把差异页交给工作区 Subagent 做视觉纠错。
- `ocr_pages.py`：逐页调度主 OCR 和次 OCR，持久化页面 Markdown、HTML、原始 HTML、layout
  sidecar 和共识记录；不把次 OCR 静默写回主 OCR。
- `ocr/backends/chandra.py`：调用 Chandra 原生 layout OCR，保留 block、bbox、图片资产和
  语义脚注；图片 alt 只作为证据元数据，不进入正文 Markdown。
- `ocr/backends/paddle.py`：可选的本地次 OCR。它输出 Chandra-shaped layout sidecar，脚注
  只按保守的页底几何和数字开头推导；明确的定义块使用相同 `footnote-def`/`[^N]:` 语义，
  但不臆测普通上标、序数或行内引用。
- `refine.py`：编排 TOC、整页插图和脚注的结构闸门，并调用本地分页/单元合并；页内章节
  边界通过 `boundary_info.start_line`/`end_line` 保留，避免把同页标题前的句子误归入新章节。
- `markdown.py`：PDF Markdown 的 polish、translate、readiness 和 validation 编排。
- `page_furniture.py`：已有 PDF 译文的页眉页脚修复交接和校验编排。
- `entities.py`：生成和校验书内实体表 handoff。
- `toc.py`：生成和校验独立的 PDF TOC 翻译 handoff。
- `pdf.py`：校验源稿/译稿并构建 PDF 路径 EPUB。
- `html.py`：编排 EPUB HTML 提取、实体表、正文/元数据 handoff、校验和重建。
- `novel.py`：编排轻小说文本提取、校验和重建。
- `tex.py`：编排 arXiv/本地 TeX 项目准备、校验和编译。
- `glossary.py`：扫描外部术语表候选，并区分严格匹配的权威表与显式选择的跨语言只读参考表；不自动选择模糊候选。

PDF 精修的核心领域模块如下：

- `ocr_consensus.py`：解释 `ocr.secondary.enabled`，生成 `single_ocr`/`two_ocr` 证据模式，
  校验次 OCR 配置、文件哈希和 `ocr_consensus.json` 是否仍对应当前页面集。
- `refine/illustration_prepare.py`：只做整页插图候选筛选、局部审阅交接、决定校验和哈希绑定；
  不自行判断普通插图是否应该移动。
- `refine/page_merger.py`：消费已验证的整页插图绑定，恢复“前页半句 → 插图页 → 后页续句”，
  并把图片及说明放回连续正文之后。
- `refine/footnote_prepare.py`：按页面 layout sidecar 生成脚注候选。视觉 OCR 使用标签、底部
  位置和编号；原生文字 PDF 使用 PDF 坐标、文本块和字体元数据，并将页底编号候选交给复核。
  两种来源共享决定、校验和归一化后端；双 OCR 模式还比较主/次候选差异。它不直接改写
  Markdown，也不把引用推断成脚注。
- `refine/footnote_apply.py`：只消费已验证的脚注决定，按完整 TOC `unit_id` 合并脚注到单元末尾，
  保留 `citation`、`bibliography` 和普通正文原位。

`pipeline_policy.py` 是 PDF pipeline 的策略边界。`PipelinePolicy.from_config()` 将缺省
配置视为传统翻译流程，将 `pipeline: epub_conversion` 和兼容别名
`mode: ocr_to_epub` 规范化为纯转换流程，并声明是否需要翻译、实体表、翻译 TOC 和
polish。`commands/markdown.py`、`entities.py`、`toc.py` 和 `pdf.py` 均应依赖该策略；
不要在这些模块重新解析 `pipeline` 或 `mode`。

新命令应优先使用 `runtime.py` 的公共上下文；不要从 `cli.py` 导入业务函数，也不要把
配置解析和输出目录推断复制到每个 handler 中。

### 2.3 Subagent 合同层

这些模块是格式无关或 Markdown/TOC 专用的本地合同实现：

- `markdown_handoff.py`：扫描源单元、计算统计信息、恢复 checkpoint、生成 manifest 和 prompt。
- `markdown_subagent_validation.py`：检查目标文件、结构标记、拒答、哈希和特殊角色内容，
  并在通过后复制到 `validated/`。
- `markdown_validation.py`：提供纯函数式的 Markdown 风险检测、目标语言审计、规范化辅助
  函数和 polish 内容保真检查；它会阻断结构性空父标题导致的重复正文。
- `subagent_runtime.py`：提供模型配置解析、token 估算、批次规划及按章节/批次隔离的 handoff；
  worker 文件数的有效上限为 8。
- `subagent_safety.py`：集中处理拒答、免责声明和翻译占位套话检测，避免各工作流使用不同规则。
- `toc_translation_workflow.py`：维护 TOC 的原始结构、节点数量、非标题字段和翻译结果校验，
  并从已验证的译文 TOC 生成按 token 预算自适应的全书方向性轮廓。

旧代码可能仍从 [`subagent_workflow.py`](../pdf2epub/subagent_workflow.py) 导入这些函数。
该文件现在是兼容门面。新代码应直接导入具体模块；如果移动公共函数，必须保留门面转出
并增加兼容性测试。

## 3. 主要工作流的数据流

### 3.1 PDF 工作流

```text
ocr-pages
  → pdf_text_probe.json
  → pages/ (native text extraction only for high-confidence vector PDFs;
            searchable OCR and scanned PDFs still use visual OCR)
ocr-pages 可选：主 OCR + PaddleOCR（兼容 layout sidecar + 文本共识）→ ocr_consensus.json
（仅当 ocr.secondary.enabled=true）ocr-correct + 工作区 Subagent + ocr-correct-validate
（只复核差异页、共同漏检风险页和确定性抽样页；一致页其余页面自动接受；原生文字跳过）
  → ocr_corrected_pages/validated/
（ocr.secondary.enabled=false 时直接使用 pages/，不运行 OCR 纠错）
refine-prepare + 工作区 Subagent
  → toc_tree.json
illustration-prepare + 工作区 Subagent（只复核疑似整页插图页）
  → illustration_candidate_report.json → illustration-validate → illustration-apply
  → illustration_bindings.json
refine-local
  → ocr_markdown/ + tree_progress.json
footnote-prepare +（必要时）工作区 Subagent
  → footnote_candidates.json → footnote-validate → footnote-apply
  → footnote_normalized/
polish + 工作区 Subagent + polish-validate（所有 PDF 必需；review_required 默认阻断，持续则人工；另做内容保真检查）
  → polished_markdown/validated/

翻译分支：
extract-entities + 工作区 Subagent + extract-entities-validate
  → translation_entities.json
translate-toc + 工作区 Subagent + translate-toc-validate
  → toc_tree_translated.json
translate + 按顶层章节划分的 worker_handoffs + 工作区 Subagent
  （超大章节只在章节内部拆分；Prompt 另含预算化的全书 TOC 方向性轮廓）
  → translated/
translate-validate → translate_validation.json（retry_required 自动返工；持续 review 升级人工）
build-epub --translated → 最终译文 EPUB

已有译文的单次页眉页脚修复：
repair-page-furniture（候选片段扫描/worker handoff） + 工作区 Subagent
→ translated/（同名文件定点修复）
repair-page-furniture-validate → translated/validated/
build-epub --translated → 最终译文 EPUB

纯转换分支（`pipeline: epub_conversion`）：
check-ready --stage package → build-epub → 原语言 EPUB
```

结构判断、润色和翻译不会在本地 Python 进程中完成。每个 Subagent 只处理 worker
manifest 指定的文件；大单元单独成批。polish 使用 `polish_worker_handoffs/`，正文翻译
使用按顶层章节隔离的 `worker_handoffs/`。同一章节拆分时，worker 共享章节级术语上下文，
但不读取其他章节的上下文。翻译 TOC 是正文 worker 启动前的独立前置任务，正文 worker
不得修改翻译 TOC。PDF 正文还接收一个按 `global_toc_tokens` 预算压缩的全书方向性轮廓，
但当前章节的精确 TOC heading contract 始终优先；标题绑定只容忍安全的展示格式差异，
不容忍语义改写。普通中文正文还必须通过目标语言内容审计。纯转换分支不生成实体表或
翻译 TOC，但仍必须通过 polish。

### 3.1.1 OCR 证据模式和结构闸门

`ocr.secondary.enabled` 是视觉 OCR 结构判断的单一开关，不应在脚注或插图模块中再添加平行
开关。原生文字 PDF 先由 `pdf_text_probe` 分类，使用独立的 native layout 证据；它不属于
双 OCR，也不因配置残留而运行第二套 OCR：

| 页面来源 | 配置 | 主/次证据 | 结构阶段行为 |
|---|---|---|---|
| `native_text` | 忽略 `ocr.secondary.enabled` | PDF 原生文本块/坐标/字体；报告兼容字段为 `single_ocr` | 不运行视觉 OCR、Paddle、`ocr-correct` 或 OCR 共识；页底编号全部作为 Subagent 复核候选。 |
| 视觉 OCR | `false` | 只有 `pages/` 主 OCR | 忽略旧共识产物；明确的高置信度 OCR 脚注可本地接受，疑难候选交给 Subagent。 |
| 视觉 OCR | `true` | 当前 `ocr_secondary/` + `ocr_consensus.json` | 共识报告和次 OCR sidecar 参与脚注/插图候选比较；差异页必须复核。 |

准备阶段把 `source_kind` 和 `ocr_evidence_mode` 写入候选报告和 manifest。对视觉 OCR，
校验/应用阶段比较当前配置、候选报告、主/次 sidecar 哈希和共识检查点；对原生文字稿，
比较原生 sidecar 哈希，并明确排除次 OCR 文件。任何一项变化都阻断旧结果。这样单 OCR
运行不会误读上一次双 OCR 的 `ocr_consensus.json`，原生文字运行也不会因为目录中存在旧
共识文件而切换证据路径。

整页插图只在 `illustration_bindings.json` 中出现 `full_page_insert` 时影响 `PageMerger`；
脚注只在 Subagent 将候选标记为 `footnote_start`、`footnote_continuation` 或
`footnote_definition` 时移动。脚注的作用域来自 `tree_progress.json` 的完整 `unit_id`，
而不是顶层 TOC；相同数字在不同实际单元中可以重新开始。跨页顺序由页面和 block 顺序保留，
所以“正文 → A 脚注 → B 正文 → A 脚注续文 → B 脚注”不会被错误重排。

### 3.1.2 页面 layout sidecar 和共识比较

每个页面至少有以下文件；原生文字 PDF 的 sidecar 使用相同文件名合同，但其 `backend` 和
`source_kind` 为 `native_text`，坐标单位为 PDF page points：

```text
pages/page_001.md          # 主 OCR 的工作流文本
pages/page_001.html        # 有 layout 时的 HTML
pages/page_001.raw.html    # 原始模型 HTML（如后端提供）
pages/page_001.ocr.json    # OCR 或 native-text layout sidecar
```

双 OCR 时，次 OCR 使用同样的文件名写入 `ocr_secondary/`，`ocr_consensus/` 保存逐页比较，
顶层 `ocr_consensus.json` 保存当前配置、源 PDF、失败页和复核页索引。`OCRPageResult` 是
后端之间的共同接口；后端可以只提供 Markdown，也可以提供 `html`、`raw_html`、`blocks`、
`assets`、bbox 和模型信息等增强字段。

共识分两层：

1. 文本比较规范化换行、Markdown 外层格式、链接和脚注分隔符，但不吞掉数字、标点或缺行；
2. layout 比较脚注标签、脚注编号序列和垂直范围。

`agree` 只表示该页通过筛查；`review_required` 表示必须把页图、主 OCR 和次 OCR 一起交给
`ocr-correct` 的 Subagent。任何后端都不能在共识阶段自动取代主 OCR。共同漏检由固定抽样和
页面密度异常信号补充发现。

#### 3.1.2.1 原生文字 PDF sidecar 合同

高置信度原生 PDF 由 `pdf_text_probe` 选择直接文本提取。`extract_native_text_pages()`
仍生成与 OCR 页相同的 `pages/page_NNN.md`，但同时生成带源坐标的
`pages/page_NNN.ocr.json`。该 sidecar 是脚注和整页插图的版面证据，不是另一套 OCR 结果，
也不是让本地脚本直接替代 Subagent 判断的标签。

最小结构如下（字段值为示意）：

```json
{
  "schema_version": 1,
  "page_number": 125,
  "backend": "native_text",
  "source_kind": "native_text",
  "coordinate_system": "page_points",
  "page_box": [0, 0, 612, 792],
  "body_font_size": 10.2,
  "formats": {"markdown": "page_125.md"},
  "blocks": [
    {
      "order": 3,
      "label": "Text",
      "bbox": [72, 690, 540, 735],
      "text": "12 A note definition...",
      "html": "12 A note definition...",
      "line_count": 2,
      "source_block": 4,
      "font_size": 8.4,
      "font_names": ["MinionPro-Regular"],
      "flags": 0
    }
  ],
  "assets": []
}
```

维护约定：

- `page_box` 和 `blocks[].bbox` 使用 PyMuPDF/PDF page points；原生稿不能套用 OCR 的
  `0..1000` 坐标启发式。`footnote_prepare._normalise_bbox()` 先按 page box 转成 `0..1`
  比例，再进行底部位置判断。
- `body_font_size` 是该页文本 span 字号的中位数。它只用于计算候选块的
  `font_size_ratio`；字号小不等于脚注。
- 每个原生文本 block 的 `source_block` 指向 PDF 原始 block，`order` 保持页面阅读顺序。
  原生文本提取会在一个 PDF block 内按“页底数字开头”拆成多个 layout block，使同页多条
  脚注可以独立复核和定位。
- 识别到的短数字/符号上标在 Markdown 中保留为 `<sup>…</sup>`；sidecar 的原始 `text`
  不被改写。PDF superscript flag 和“字号明显小且位于行上方”只提供上标证据。
- 原生候选检测要求块位于页面底部、以数字加分隔符开头且不是纯数字页码；它生成
  `review_required` 候选，不生成可直接搬移的决定。页底坐标、字号、字体和上标必须由
  Subagent 结合正文上下文确认。

脚注准备报告还记录 `sidecar_sha256`。`footnote_decisions.json` 只能引用候选报告中的
`page`/`block` 地址；`footnote-validate` 会检查 source kind、sidecar 哈希、决定覆盖率、
脚注键和 continuation 的可追溯性。`footnote-apply` 再用相同地址在当前章稿中唯一定位
文本，定位不到、定位到多个或命中正文不唯一时阻断，不猜测替换。

两类来源之所以可以复用同一套 apply 后端，是因为它们都最终提供：

1. 有序的 `page`/`block` 地址和紧凑 review window；
2. 相同的 decision roles（`body`、`citation`、`bibliography`、三种脚注角色）；
3. `tree_progress.json` 的完整 `unit_id` 作用域与跨页顺序；
4. sidecar/source/checkpoint 哈希，可在 apply 前重新验证。

来源差异只停留在“如何生成候选”和“需要多少 Subagent 复核”：OCR layout 可以在明确
标签和几何证据下产生高置信度候选，native layout 的页底编号全部保守地进入复核。

### 3.1.3 公式输出契约

PDF/EPUB 的 Markdown→XHTML 处理目前采用两级公式策略：

- 可直接表达的行内公式优先使用 `unicodeitplus` 转成 Unicode；
- 复杂行内公式、块公式和 OCR 后端输出的原始 `<math>` 片段使用 `latex2mathml` 转成 MathML；
- 代码块、行内代码和已生成的公式片段在 Markdown 预处理期间使用占位保护，避免 `*`、表格
  或属性处理器破坏公式；
- 公式失败时保留可读的 LaTeX 退路，并记录日志；公式不是翻译 API 的调用点。

当前没有 `dvisvgm`/XeLaTeX SVG 渲染器，因而普通 EPUB 构建不依赖 TeX Live。若未来增加
SVG，必须作为显式可选能力设计：配置开关、工具链预检、缓存目录、SVG 安全清洗、失败策略、
跨平台测试和 README/AGENTS/维护文档必须同时更新，不能让构建隐式要求安装系统 TeX。

### 3.2 高保真 EPUB 工作流

```text
html-prepare（首次）
  → compressed_units/ + 实体提取 handoff
工作区 Subagent 写入 translation_entities.json
html-prepare（再次）
  → 正文 handoff + 元数据 handoff
工作区 Subagent 写入 translated_compressed/ 和 translated_metadata.json
html-validate
  → translate-html_validation.json
build-html-epub
  → 保留原始 HTML 结构的译文 EPUB
```

HTML 单元要求非空内容一一对应；标签、属性、实体、容器和嵌套关系由本地校验器检查。
HTML 构建还会把 NCX/nav 更新结果写入导航报告；已有导航文档更新失败时默认阻断打包，
只有显式确认参数才能放行。EPUB 只提供其中一种导航格式时，另一种记录为缺失而不是异常。
最终成品通过固定夹具的 PyMuPDF 渲染回归测试，检查打包结果能否被阅读器引擎稳定打开。

### 3.3 轻小说和 TeX 工作流

- 轻小说：`translate-novel` 准备文本单元和元数据 handoff，Subagent 写入译文，
  `translate-novel-validate` 通过后才能 `build-novel-epub`。
- TeX：`translate-arxiv` 建立独立运行目录和 `translated_tex_units/`，Subagent 只修改译文单元；
  `translate-arxiv-validate` 重建项目并进行本地编译，不能把未翻译的原始工程当作完成结果。

## 4. 依赖方向

推荐依赖方向如下：

```text
CLI → command registry → command handlers
                         ├→ command runtime/context
                         ├→ pipeline policy
                         ├→ format workflow services
                         └→ Subagent contract modules

Subagent contract modules → shared utilities/domain services
Format workflow services  → shared utilities/domain services
```

需要避免的方向：

- 业务模块依赖 `cli.py`；
- 一个格式的 command handler 依赖另一个格式的打包实现；
- 通过兼容门面反向调用具体实现；
- 为了复用一小段逻辑而加载完整的 CLI 或模型客户端；
- 让源文件或 Subagent 生成的文本改变任务合同、访问外部文件或发起网络请求。

## 5. 校验和恢复模型

“目标文件存在”不等于“任务完成”。恢复条件必须同时满足：

1. 目标文件非空且结构校验通过；
2. validation report 记录了对应源文件的 SHA-256；
3. 当前源文件、该单元的 TOC 上下文、实体/术语投影和对应记录一致；全书级上下文变化不再默认使未受影响单元失效；
4. 普通正文的目标语言审计、特殊角色数字保护、TOC 绑定和全量流程报告全部通过后，才允许打包。

实体表的恢复条件还包括源文件集合与 manifest 完全一致；只要集合新增或删除，即使剩余文件
哈希未变，也必须重新生成实体 handoff。OCR 的第二套后端则在配置加载阶段完成一致性校验，
避免策略层、命令层和实际 OCR 执行层对同一开关产生不同解释。

脚注候选报告至少绑定主 OCR sidecar 哈希、`source_kind`、证据模式和（双 OCR 时）次 OCR
sidecar 哈希；原生文字稿还必须保留 `coordinate_system: page_points` 的 sidecar。脚注决定
只能引用当前候选报告中的 page/block 地址。插图绑定还绑定候选报告哈希、源页哈希、sidecar
哈希和证据模式。`refine-local`、`footnote-apply` 和插图加载阶段都会重新检查这些绑定；
配置从单 OCR 切换到双 OCR，或反向切换时，必须重新准备对应阶段。

### 5.1 原生文字脚注的恢复合同

原生文字运行的最小恢复链是：

```text
ocr-pages --resume
→ refine-local --resume
→ footnote-prepare
→ [Subagent 写 footnote_decisions.json]
→ footnote-validate
→ footnote-apply
```

其中：

- 旧运行若没有 `pages/page_*.ocr.json`，即使 `pages/*.md` 仍在，也必须重新运行
  `ocr-pages --resume`；不能从 Markdown 反推 PDF 坐标和字体。
- PDF 源哈希、页 sidecar、TOC 或已应用的整页插图发生变化时，`tree_progress.json` 和
  脚注的 page/block 地址可能失效。先让 `refine-local --resume` 更新单元，再重新准备
  脚注候选；不直接重用旧决定。
- `footnote_candidates.json` 是候选事实和哈希快照，
  `footnote_subagent_manifest.json` 是 handoff 索引，`footnote_decisions.json` 是唯一的
  人工/模型决定来源，`footnote_decision_validation.json` 是决定门禁，
  `footnote_normalization.json` 是 apply 后的结果检查点。它们的职责不能互换。
- `No page layout sidecars found`、`source_kind`/`coordinate_system` 不一致、sidecar
  hash 过期、page/block 地址无法唯一命中或缺少正文命中，都是源阶段/定位失败，应阻断并
  重建候选；不能用手工改 JSON 或 `--allow-review-warnings` 绕过。

单文件校验只提供 checkpoint，不能替代全量校验。源稿、实体表、TOC 或术语上下文变化
只应使受影响的 checkpoint 重新进入 `pending`；旧 manifest 没有单元级上下文哈希时，
为安全起见按整批重新处理。父 manifest 是完整审计/恢复索引，worker handoff 则是只含
当前 assignment 的最小运行投影。

## 6. 扩展和维护约定

新增功能时按以下顺序处理：

1. 先确定它属于命令编排、领域服务还是 Subagent 合同层。
2. 将可复用的纯逻辑放入对应的共享模块，不复制到多个 command handler。
3. 在 `commands/registry.py` 注册参数和 handler。
4. 为模块边界、旧导入兼容性、失败恢复和结构校验增加测试。
5. 更新本文件中的模块地图；如果 pipeline 选择、Subagent 总闸、校验门禁或恢复规则
   发生变化，同时更新 `AGENTS.md`。
6. 如果改变使用者可见的功能、配置示例、依赖或命令顺序，同时更新 `README.md`；如果改变
   handoff、manifest、sidecar、validation report 或恢复语义，同时更新
   `docs/antigravity-workflow.md` 或对应专项维护文档。

测试入口为 `uv run pytest -q`。代码重构不应读取、改写或重新生成用户的书稿和译文输出；
涉及实际翻译时必须重新遵守 [`AGENTS.md`](../AGENTS.md) 的 Subagent 总闸和开工检查。

### 本地工作文件归档

仓库根目录只保留源码、配置模板、文档和可重复运行的测试。一次性翻译脚本、实验切片、
比对结果、审计报告和调试输出统一放在 `.work/<task>/`；`.work/` 是本地工作台，不进入 Git。
PDF/EPUB 流程自己的 scratch 文件放在 `output/<title>/scratch/<task>/`，而 `scripts/` 只放
可复用且准备纳入仓库的工具。正式术语表仍属于 `glossaries/`，不应被移动到工作台。历史
临时文件迁移时要先确认用途，并采用可恢复的移动，不得批量删除未知文件。
