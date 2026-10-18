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
├── document_roles.py                特殊单元角色和参考文献识别
├── entity_context.py               书内实体术语交接和新鲜度合同
├── page_furniture_rules.py          页眉、页脚、页码候选的纯规则
├── pdf_unit_roles.py                PDF 精修单元角色的只读投影
├── toc_translation_workflow.py      PDF TOC handoff、合并和校验
├── subagent_runtime.py              模型选择、token 估算和批次 handoff
├── subagent_dispatch.py             assignment digest、原子 lease 和冲突检测
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
注册器只保存 handler 的模块路径和名称，在 argparse 真正选中命令后才导入所属工作流；因此
显示用法或构建参数表时不会加载 OCR、PDF、HTML、TeX 和翻译运行时。

### 2.2 命令编排层

`commands/` 中的模块负责把 CLI 参数转换成领域服务调用，并统一处理配置、日志、错误码
和下一步提示：

- `runtime.py`：提供 `BookCommandContext`、配置加载和输出目录解析。
- `sources.py`：选择原始 `ocr_markdown` 或已验证的 `polished_markdown`；所有 PDF 的翻译、实体提取和打包都必须使用后者。
- `ocr.py`：执行唯一允许调用 OCR 服务的入口；启用次 OCR 时只把差异页交给工作区 Subagent
  做视觉纠错。
- `ocr_pages.py`：逐页调度主 OCR 和可选次 OCR，持久化页面 Markdown、HTML、原始 HTML、
  layout sidecar 和共识记录；不把次 OCR 静默写回主 OCR。
- `ocr/backends/chandra.py`：调用 Chandra 原生 layout OCR，保留 block、bbox、图片资产和
  语义脚注；图片 alt 只作为证据元数据，不进入正文 Markdown。
- `ocr_backends.py`：保留历史 VLLM chunk adapter；默认页面流程不依赖它。
- `refine/pdf_evidence.py`：统一决定当前页面集合使用 `native_text` 版面证据、`single_ocr`
  还是 `two_ocr` 共识，并集中校验共识检查点；脚注和插图阶段不得各自重新解释这个选择。
- `refine/layout_evidence.py`：统一加载 layout sidecar、清理 block 文本和归一化 bbox；它只
  处理几何/文本证据，不包含脚注或插图的语义判断。
- `pdf2epub/commands/refine.py`：编排 TOC、整页插图和脚注的结构闸门；`pdf2epub/refine/main.py`
  负责本地分页和单元合并；页内章节
  边界通过 `boundary_info.start_line`/`end_line` 保留，避免把同页标题前的句子误归入新章节。
- `markdown.py`：PDF Markdown 的 polish、translate、readiness 和 validation 编排。
- readiness 对派生源稿使用显式依赖诊断：脚注归一化过期时只报告其哈希/证据根因，
  polish 与实体检查标记为 `blocked_by`，不把级联症状伪装成独立损坏。
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
  校验次 OCR 配置、文件哈希和 `ocr_consensus.json` 是否仍对应当前页面集；提供
  `ocr-consensus-rebuild`，在已有两套 OCR 产物完整时离线重算共识而不重新调用后端。
- `refine/illustration_prepare.py`：只做整页插图候选筛选、局部审阅交接、决定校验和哈希绑定；
  不自行判断普通插图是否应该移动。
- `refine/toc_sanitization.py`：只在当前物理起始页同时提供匹配的印刷页码和去掉该数字后的
  正文标题时清洗 TOC 末尾页码；所有候选、证据和源/目标哈希写入
  `toc_title_normalization.json`，证据不足时保留原题名。
- `refine/toc_heading_recovery.py`：从只读的 `pages/page_NNN.html` 或 sidecar `raw_html`
  中提取 `Page-Header`/`Page-Footer` 块，并在规范化后与 TOC 标题精确匹配；只用于恢复视觉
  OCR 误删的真实标题，不做模糊相似度匹配。`native_text` sidecar、缺少证据、标题已存在和
  歧义匹配都写入 skipped。恢复映射供 `PageMerger` 在内存中注入，原始页面文件不变。
- `refine/page_merger.py`：消费已验证的整页插图绑定，恢复“前页半句 → 插图页 → 后页续句”，
  并把图片及说明放回连续正文之后；普通页面边界也会在前后均为未结束的正文块时保守拼接，
  不把完整句子、标题、列表、脚注、图片或表格误合并。原生文字 PDF 还会根据 page-point
  sidecar 在页边 5% 内确定性清除 DOI/孤立页码，并仅清除跨页重复短页眉页脚；原始 `pages/`
  不会被覆盖，sidecar 会参与 refine checkpoint 指纹。
- `refine/footnote_prepare.py`：只编排按页面 layout sidecar 生成脚注候选、结构排除和双 OCR
  比较，不负责页面扫描细节、区域策略、视觉证据或报告持久化；这些职责分别由
  `footnote_page_scan.py`、`footnote_continuation_regions.py`、`footnote_regions.py` 和
  `footnote_report.py` 承担。视觉 OCR 先用
  宽松的页底召回地板收集候选，再结合页面自身的内容尾部、相对块间距和独立语义证据决定是否本地接受；
  全书固定的 `bbox[3]` 不再是自动判定条件。标签只能作为辅助证据，不能单独绕过几何条件。它排除
  页眉、页脚、纯页码和引用标签，把连续数字开头块（例如
 `18, 19, 20, 21`）及其相邻续文组织为一个区域；如果数字组已有明确的 `Footnote` 标签，普通
 `Text` 块默认不会被机械回溯，只有同样带脚注标签的相邻块才可作为续文加入；若紧邻上一页已有脚注
 证据，则恢复无编号续文回溯并交给视觉审核，以覆盖跨页的“正文 → 上一脚注续文 → 新脚注”布局。
明确标注的区域首块也不会仅因上方相邻正文触发边界不确定审核。连续数字区向上回溯得到的普通 `Text` 块若
 没有跨页证据或只有低置信度且数字区已有 `Footnote` 标签，只写入 `suppressed_continuation_candidates`
 作为上下文，不触发视觉任务；明确标注、较强连续性和真正的边界冲突仍保留审核。清晰区域由本地接受；边界不清、区域内有未分类
 block、只有连续数字而没有独立证据、正文流延伸到页尾、OCR 证据冲突或几何漏检则生成
 `footnote_visual_audit_manifest.json`，并附页面图和可用的
  区域裁剪图，交给视觉 Subagent。`suspected_missed_count` 对应的可疑漏检会进入
  `audit_candidates`，不再只是静态统计。原生文字 PDF 仍使用 PDF 坐标、文本块和字体元数据。
  跨页候选还会写入 `continuation_evidence`，记录上一页最后一个物理脚注、当前页候选前的最近正文块、
  bbox/字号/字体对齐、块间距和句子边界连续性；上一页其他脚注只保留为审计背景，不作为续文匹配目标。候选
  会在“延续上一页脚注”与“当前页正文”两个假设之间做无外部模型的保守词汇连续性比较；正文块内保留的
  `<sup>…</sup>` 引用是强正文信号。只有普通 `Text` 块的正文假设明显胜出时才降为
  `suppressed_continuation_candidates` 上下文，不会自动移动；脚注假设胜出或比较不分明时仍由 Subagent
  决定。中/高置信度且最后一个物理脚注的文本未明显闭合时，本地可生成 `footnote_continuation` 决定；最后一个
  脚注已闭合或证据不足的候选仍由 Subagent 决定。上一页脚注编号是否唯一不参与自动判断，因为单个脚注可能
  跨页，多个脚注时也只有物理上最后一个脚注具备自然续接位置。视觉审核必须以整页图判断整体边界，区域裁剪图只
  用于放大细节。
  `footnotes.auto_accept` 是两种来源共用的区域级本地接受策略；双 OCR 模式比较主/次候选差异，
  并把次 OCR 的连续物理行合并成一个 secondary-only 复核窗口。`resolve_footnote_options()` 集中
  处理配置默认值和 CLI 覆盖。它不直接改写 Markdown，也不把引用推断成脚注。
`footnotes.mode` 可设为 `auto`、`footnotes`、`endnotes_only` 或 `disabled`，默认 `auto`。
显式值直接生效；`auto` 由 `resolve_effective_footnote_mode()` 读取
`ocr_markdown/tree_progress.json` 的 `book_metadata.footnote_mode`，再回退到
`toc_tree.json` 顶层 `footnote_mode`，都没有有效值时才回退 `footnotes`。
`endnotes_only` 跳过页面脚注提取，但仍归一化 typed Notes；`disabled` 才原样生成后续阶段使用的源稿。
`footnote_candidates.json` 同时记录 effective `footnote_mode`、配置态
`configured_footnote_mode` 和 `footnote_mode_source`（`config`/`toc`/`default`）。
候选、决定校验、materialize 和 status 阶段都用当前 effective mode 比较 checkpoint；
TOC 的 `footnote_mode` 变化导致 effective mode 改变时视为 stale，必须重跑
`footnote-prepare → footnote-validate → footnote-apply`。
TOC 中标记为
  `toc`、`notes`、`bibliography`、`index`、`author_notes`、
  `catalog` 或 `ad` 的单元也不会进入页面脚注候选。
- `refine/footnote_options.py`：集中维护脚注模式、默认阈值、CLI/config 覆盖解析和
  `auto` → effective mode 的只读解析；候选、校验和 materialize 阶段共享同一个不可变
  选项对象，避免各阶段各自解释配置。该模块只读取 TOC/进度元数据，不扫描页面。
- `refine/endnote_scopes.py`：从 `toc_tree.json`、`ocr_markdown/tree_progress.json` 和
  当前 Markdown 构建只读的按章 scope 目录。顶层单元使用 `u<unit_order>`，后代使用
  `u4c1`、`u4c1c2`；正文只按 Markdown 标题行切分 span。Notes 标题可以是 Markdown
  标题，也可以是独立短行（1..120 字符、前后为空行/文件边界、非编号定义且不以句末标点
  结尾），两者都通过剥离 `Chapter N:`/`N:`/前导 `N.` 后的标题变体匹配回正文 scope。
  `Notes to Chapter N` 等页眉和未匹配标题保持当前 scope；歧义标题只记录，不猜测。
- `refine/endnote_normalization.py`：只处理 `type: notes` 单元中的集中尾注定义及普通单元
  上标引用的确定性归一化，并提供构建前的残留检查；不参与页面脚注候选识别。存在重复
  打印编号时，它消费 `endnote_scopes.py` 的目录，只把需要去重的 key 改写为
  `fnc-<scope>-<number>`（第 N 个定义再加 `-oN`），并支持“外层页计数器 + 内层真实
  注释号”的双层编号判定。无 scope 目录或无重复 key 时保持原有全局行为。
- `refine/footnote_consensus.py`：只读取当前双 OCR 共识中的视觉复核页并解析匹配的次 OCR
  sidecar 路径；不决定候选，也不绕过 `pdf_evidence.py` 的 freshness 闸门。
- `refine/footnote_page_scan.py`：封装单页 primary/secondary OCR 扫描、连续区域展开、续文
  证据、漏检审计、候选差异和 page report；只返回事实与有限的 review 标记，不写最终脚注角色。
- `refine/footnote_continuation_regions.py`：只负责连续数字区域的几何识别、相邻块回溯和
  续文候选扩展；不评估跨页语义、不写 Markdown，也不决定最终脚注角色。
- `refine/footnote_report.py`：汇总跨页窗口、视觉页面/区域证据、统计和哈希快照，并原子写入
  `footnote_candidates.json`；不重新扫描 sidecar 或推断脚注语义。
- `refine/footnote_contracts.py`：集中维护决定角色和可移动角色集合，供准备、校验和应用阶段
  共享；不依赖准备编排模块，避免契约层反向导入业务流程。
- `refine/footnote_apply.py`：只消费已验证的脚注决定，按完整 TOC `unit_id` 合并脚注到单元末尾，
  保留 `citation`、`bibliography` 和普通正文原位。次 OCR 独有脚注必须带显式文本、来源单元
  和 `primary_disposition`；主 OCR 重复块只有在明确列出 `primary_block(s)` 时才会删除，重复
  编号引用无法唯一定位时阻断而不猜测。
- `refine/footnote_matching.py`：集中处理 sidecar block 到 Markdown 源文的安全定位、文本变体和
  非重叠编辑区间；不读取工作流状态，也不执行脚注物化。
- `refine/footnote_status.py`：集中计算脚注归一化 checkpoint 的哈希和 freshness 状态；不依赖
  apply/prepare 编排，供命令层和 readiness 诊断复用。

按章尾注 scope 只是派生报告，不写入 `output/`。`footnote_apply.py` 在生成
`footnote_normalized/` 前加载 `toc_tree.json` 和 `ocr_markdown/tree_progress.json`；
缺少 TOC 或 scope 构建失败时，如果 Notes 仍有重复 key，归一化必须失败并提示，不能静默退回
`footnotes.mode: disabled`。scope 触发条件为同一数字 key 在至少两个 scope 有定义，或同一
`(scope, key)` 有多个定义。跨 scope 重复或同一 scope 内重复定义的 key 会改写为
`fnc-<scope>-<number>` / `...-oN`；其他 key 保持原数字。定义多于
引用时多出的定义保留为 orphan；引用所在 scope 无定义、定义 scope 无正文 span、引用数超过
定义数或重复 key 落在未匹配区域时 fail closed。EPUB 侧由
`pdf2epub/epub/footnotes/keys.py` 的 `display_footnote_key()` 统一把内部 key 还原为读者
看到的原始数字；scanner、链接和锚点仍使用真实 key。

`pipeline_policy.py` 是 PDF pipeline 的策略边界。`PipelinePolicy.from_config()` 将缺省
配置视为传统翻译流程，将 `pipeline: epub_conversion` 和兼容别名
`mode: ocr_to_epub` 规范化为纯转换流程，并声明是否需要翻译、实体表、翻译 TOC 和
polish。`commands/markdown.py`、`entities.py`、`toc.py` 和 `pdf.py` 均应依赖该策略；
不要在这些模块重新解析 `pipeline` 或 `mode`。

新命令应优先使用 `runtime.py` 的公共上下文；不要从 `cli.py` 导入业务函数，也不要把
配置解析和输出目录推断复制到每个 handler 中。

`cli.py` 仅保留命令注册和启动逻辑。为兼容历史上从入口模块导入工作流 helper 的调用方，
少量旧名称通过模块级懒加载映射按需解析；新代码必须直接从所属领域模块导入。这样普通 CLI
启动不会预先导入全部 OCR、PDF、HTML、TeX 和翻译工作流，也不会把入口文件变成共享服务层。

#### 2.2.1 OCR 后端边界和运行时隔离

OCR 后端通过 `ocr/backends/__init__.py` 的 `OCRBackendSpec` 延迟注册。注册层只描述后端
能力，不在导入阶段初始化网络客户端、GPU runtime 或模型：

| 能力字段 | 用途 |
|---|---|
| `chunk_processor` | 兼容旧的 PDF chunk 调用，当前仅由 VLLM 使用 |
| `image_page_processor` | 输入单页图像并返回 `OCRPageResult`，当前由 Azure/Vision 使用 |
| `native_page_processor` | 返回原生 layout 证据，当前由 Chandra 使用 |
| `init_client` | 在第一次实际使用前创建后端客户端或进程代理 |

启用次 OCR 时，`ocr_pages.py` 会在任何主 OCR 页面 worker 启动前生成
`ocr_secondary_preflight.json`。它按页数、次 OCR worker 数和
`ocr.secondary.performance.estimated_seconds_per_page` 计算保守耗时；超过
`max_estimated_seconds` 会阻断本次运行，只有用户显式传入 `--allow-slow-secondary` 才继续。
若用户要切换到单 OCR，必须明确关闭 `ocr.secondary.enabled` 后重新运行，旧的双 OCR 共识不会
参与后续结构阶段。

`local_credentials.py` 负责读取被 Git 忽略的本地凭据；各 OCR 后端不共享运行时状态。

`commands/diagnostics.py` 提供只读的 `status` 和 `doctor`。`status` 根据当前输入哈希、阶段
检查点和验证报告给出恢复顺序；`doctor` 在载入完整工作流前拒绝未知或退役的 OCR backend
以及缺失的选配 SDK。诊断命令不写入 readiness 或其他恢复产物。

发布边界是“发布接口，不发布运行时”：应包含后端源码、注册表、测试、配置模板和维护文档；
应排除 `.secrets/`、真实 `config.yaml`、`input/`、`output/` 和一次性工作脚本。维护者在发布前用
`git status --short` 与 `git check-ignore` 审核，而不是无选择地执行 `git add .`。

### 2.3 Subagent 合同层

这些模块是格式无关或 Markdown/TOC 专用的本地合同实现：

- `markdown_handoff.py`：扫描源单元、计算统计信息、恢复 checkpoint、生成 manifest 和 prompt。
- `markdown_subagent_validation.py`：检查目标文件、结构标记、拒答、哈希和特殊角色内容，
  并在通过后复制到 `validated/`。
- `markdown_validation.py`：提供纯函数式的 Markdown 风险检测、目标语言审计、规范化辅助
  函数和 polish 内容保真检查；它会阻断结构性空父标题导致的重复正文，并对 `notes`
  单元中的标准尾注章节标题提供窄范围页眉误报豁免。该模块把角色判断委托给
  `document_roles.py`，把页眉页脚候选判断委托给 `page_furniture_rules.py`，避免校验模块
  反向依赖具体修复工作流。
- `footnote_normalization.py`：提供 polish 阶段的尾注引用/定义迁移校验；它只校验
  Subagent 输出，不负责识别页面脚注或修改源稿。
- `commands/markdown_context.py`：只读加载 TOC、续文和章节分组上下文；准备、校验和
  worker 装箱共享同一份 PDF 单元投影，`commands.markdown` 不再承载这组状态读取细节。
- `document_roles.py`：集中维护特殊翻译角色、参考文献/前置内容识别、目标语言密度豁免和
  文件名角色回退规则。它只处理文本与文件名事实，不读取工作流状态，也不执行写入。
- `entity_context.py`：集中维护 PDF/EPUB 共用的书内实体源文件选择、Subagent 交接和源哈希
  新鲜度检查。`commands.entities` 只负责命令参数和 PDF 前置条件，HTML、Markdown 与诊断模块
  直接依赖该格式无关服务，不再跨 command 导入私有 helper。
- `page_furniture_rules.py`：提供无副作用的页眉、页脚、页码和出版信息候选识别；候选只是
  交给 Subagent 复核的提示，绝不在规则层删除正文。`page_furniture_repair.py` 只负责编排、
  快照和校验，并直接依赖规则模块的正式接口。
- `pdf_unit_roles.py`：从当前 `tree_progress.json` 或兼容的 `toc_tree.json` 投影 PDF 单元角色。
  `commands.markdown` 和页眉页脚修复命令共享该只读投影，不再互相导入私有 helper。
- Markdown→XHTML 默认遵循标准 Markdown 的软换行语义：单换行不会生成 `<br />`，只有
  空行或显式硬换行标记创建段落/硬换行；需要旧式单换行渲染时只能显式启用兼容参数。
- `subagent_runtime.py`：提供模型配置解析、token 估算、批次规划及按章节/批次隔离的 handoff；
  worker 文件数的有效上限为 8。
- `subagent_safety.py`：集中处理拒答、免责声明和翻译占位套话检测，避免各工作流使用不同规则。
- `toc_translation_workflow.py`：维护 TOC 的原始结构、节点数量、非标题字段和翻译结果校验。
  prepare 阶段同时生成源合同和扁平模板；Subagent 只提交
  `toc_translation_result.json`，本地程序从当前且哈希匹配的不可变源树合并生成
  `toc_tree_translated.json`，再从已验证的译文 TOC 生成按 token 预算自适应的全书方向性轮廓。
  `status`/`doctor` 调用只读校验，不物化合并文件；旧的 contract v1 完整树格式仅保留兼容
  恢复路径。
- TOC 标题绑定区分 `visible` 与 `container_only`：无可见源标签的纯图片/边界单元不要求
  worker 伪造 Markdown 标题，章节容器标题由 EPUB 构建器生成；可见子标题仍使用严格绑定。
  大章节拆成多个物理 part 时，`build_toc_heading_contexts` 为每个 part 生成独立 context：
  父标题只在源标题实际出现的 part 标记 `visible`，其余 part 标记 `container_only`；
  每个子标题按其源标题首次出现的 part 归属，源标题在所有 part 都缺失时只挂到第一个
  part 并标记 `container_only`，避免 scoped worker 收到其他分片的标题或凭空新增标题。
  当 `container_only` 或缺失的子标题在生成的 Markdown 中找不到可见标题时，EPUB 构建器
  保留 TOC 层级，但导航回退到父单元文件或首个含文件的后代，不生成指向不存在锚点的
  fragment 链接。

Subagent 合同已按职责拆分到上述具体模块，不再提供聚合式兼容门面。调用方应直接导入
所属模块；移动公共函数时必须同步更新调用方、文档和契约测试。

### 2.4 按 TOC 章节装箱的实现边界

正文翻译的“章节”与“worker”是两个不同的概念。`chapter_groups` 是稳定的语义索引：
有序地把一个顶层 TOC 分支映射到它拥有的源单元文件；`worker_handoffs` 是本次运行按
额度和断点状态生成的执行投影。前者用于恢复、审计和术语作用域，后者用于授权某个
Subagent 实际读写哪些文件。两者不能互相替代。

章节映射由格式命令建立：

- PDF 的 `commands.markdown._load_pdf_chapter_groups()` 优先读取
  `ocr_markdown/tree_progress.json` 的完整单元/part 关系，再用 `ChapterIdentity` 作为旧
  产物的文件名回退；因此同一大章节的 part 文件仍属于一个语义组。
- EPUB 的 `commands.html._load_html_chapter_groups()` 通过 `TOCExtractor` 把目录 href
  映射到压缩单元文件，并递归收集子目录单元。一个文件只归入第一次命中的 TOC 分支；
  没有被目录覆盖的文件会生成 `unit:<filename>` 的单文件回退组，不能静默丢失。
- `markdown_handoff.prepare_markdown_subagent()` 只把声明过的源文件写入映射，并把不在
  映射中的源文件补成单文件组。它同时计算源哈希、单元上下文哈希和 `pending_files`，
  所以装箱器不需要重新猜测哪些文件可用。

`subagent_runtime._chapter_worker_groups()` 的处理顺序是固定的：先按 TOC 顺序读取待处理
章节，对每个章节单独运行 `_recommended_batches()`；若章节需要多个批次，或任一文件超过
字节/token 上限，就先冲刷当前短章节队列，再只在该章节内部生成 part worker。普通短章节
才会进入连续装箱，单章节仍受 `max_source_tokens`、`max_files` 和单文件字节上限约束；
多个相邻且各自完整的普通章节合并时使用独立的 `max_packed_source_tokens` 总预算。
连续且每章不超过 `tiny_chapter_max_tokens` 的极短章节可在同一批次中使用
`max_tiny_chapters_per_worker` 的更高章节上限，但也不能突破合并总预算或文件字节上限。
已完成而被 `--resume` 跳过的章节会冲刷当前队列，作为相邻性
屏障；这保证恢复时不会把两个原本不相邻的章节拼到一起。这个过程只合并 worker 的文件
清单和上下文，不把源 Markdown/HTML 物理拼接。

并发配置的默认值保持保守；`max_concurrency` 只决定交接提示和本地 worker 分组的上限，
不会绕过模型或 IDE 的额度。运行时硬上限为 6；存在大文件时仍会按文件规模自动降低有效并发。
索引、参考文献和需要内部拆分的章节继续保持语义隔离，不因提高并发而跨章节合批。

`write_worker_handoffs()` 随后把父级 manifest 投影为每个 worker 的最小 manifest：

1. `assigned_files`、对应的 file stats、单元/TOC 上下文哈希和必要的章节元数据被保留；
   父级的全书 `file_stats`、`chapter_groups`、推荐队列和审计快照路径不作为 worker 权限。
2. 单章节 handoff 保留兼容字段 `chapter_id`、`chapter_file_count`；多章节 handoff 使用
   `chapter_ids`、`chapter_count` 和 `chapter_file_counts`，方便日志、校验和恢复识别边界。
3. 合并章节使用 `adjacent_chapter_direct_file_contexts`，把术语条目写成
   `files: {filename: entries}`；普通单章节使用 `chapter_sparse_direct_context`，大章节
   分片使用 `chapter_shared_local_direct_context`。上下文 schema 的选择由 handoff 范围
   决定，不能由 Subagent 自行改用完整术语快照。
4. worker prompt 再次列出同一组 `assigned_files`，并明确不得读取或修改其他 worker 的
   文件。翻译输出仍是每个源文件一个同名目标文件，因此单文件校验和 `--resume` 可以继续
   精确到单元。
5. 每个 handoff 记录 `assignment_sha256`，由任务、worker、批次、文件清单和当前源哈希
   组成；重新生成完全相同的 assignment 时只继承 `assigned`/`running` 的 lease 元数据，
   不继承 `completed` 状态。该字段帮助调度端去重，但不伪装成 IDE 进程锁；真正派发前仍
   需要查询工作区 Subagent 的实际活动状态。
6. 翻译 handoff 的 `atomic_output_contract` 要求临时同级文件原子替换正式目标；校验器会
   清理遗留临时文件，把无效目标移入可恢复的 scratch 区。恢复重建不会重新派发已有当前
   checkpoint 的 worker，旧 handoff 会保留为 `retired_worker_handoffs` 审计记录并标记
   `completed` 或 `superseded`。

这一设计的可维护不变量是：TOC 顺序不变、章节组不跨越大单元硬边界、worker 不拥有组外
文件权限、术语上下文不跨文件广播、输出文件集合与源文件集合保持一一对应。任何改变装箱
或 context schema 的代码，都必须同时更新 `AGENTS.md`、`docs/antigravity-workflow.md`、
相应格式维护文档和 `tests/workflow_optimizations_test.py` 的合同测试。

## 3. 主要工作流的数据流

### 3.1 PDF 工作流

```text
ocr-pages
  → pdf_text_probe.json
  → pages/ (auto/ocr always use visual OCR; native text extraction is a
            legacy opt-in via pdf_text.mode=native for high-confidence
            vector PDFs)
ocr-pages 可选：主 OCR + 配置的次 OCR（文本/版面共识）→ ocr_consensus.json
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
  → pagination_map.json + toc_title_normalization.json
  → toc_heading_recovery.json（被误删 TOC 标题的确定性恢复报告）
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
  → toc_translation_result.json →（本地合并）toc_tree_translated.json
translate + 按 TOC 顺序装箱的 worker_handoffs + 工作区 Subagent
  （相邻短章节可合并；超大章节只在章节内部拆分；Prompt 另含预算化的全书 TOC 方向性轮廓）
  → translated/
translate-validate → translate_validation.json（retry_required 自动返工；持续 review 升级人工）
build-epub --translated → 最终译文 EPUB

翻译流程中，若 `translate_validation.json` 对当前 `polished_markdown/validated/` 和
`translated/validated/` 仍为 current，而 `build-epub` 未带 `--translated`，命令只打印警告
并继续构建原稿 EPUB；默认行为不变，译文版仍必须显式传 `--translated`。

PDF `build-epub` 的默认输出文件名由 `utils.common.sanitize_filename()` 从当前书名生成：
Windows 禁止字符和控制字符替换为 `_`，首尾空格/句点会去除，保留设备名会加前缀，长度
限制为 200 个字符；Unicode 和全角标点保持不变。`output_epub` 显式路径是例外，会按用户
给定路径写入。输出目录在命令上下文创建时已由配置书名确定，因此翻译后的文件名和输出目录
可能使用不同书名。

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
使用按 TOC 顺序装箱的 `worker_handoffs/`。相邻短章节可以共享一个 worker，但每个
章节仍是独立的语义组；合并 worker 使用按文件分区的直接术语上下文，避免不同章节
串词。同一大章节拆分时，worker 共享章节级术语上下文，但不读取其他章节的上下文。
翻译 TOC 是正文 worker 启动前的独立前置任务，正文 worker 不得修改翻译 TOC。PDF 正文
还接收一个按 `global_toc_tokens` 预算压缩的全书方向性轮廓，但当前章节的精确 TOC
heading contract 始终优先；标题绑定只容忍安全的展示格式差异，不容忍语义改写。普通中文正文还必须通过目标语言内容审计。纯转换分支不生成实体表或
翻译 TOC，但仍必须通过 polish。

### 3.1.1 OCR 证据模式和结构闸门

`pdf_text.mode` 决定 PDF 页面源；`ocr.secondary.enabled` 是视觉 OCR 结构判断的单一开关，
不应在脚注或插图模块中再添加平行开关。`auto` 和 `ocr` 都走视觉 OCR；原生文本是 legacy
opt-in，只有显式 `pdf_text.mode: native` 且 `pdf_text_probe` 判定为高置信度原生文字时才
使用。`refine/pdf_evidence.py` 是脚注和插图共同使用的证据选择边界；原生文字 PDF 使用独立的
native layout 证据；它不属于双 OCR，也不因配置残留而运行第二套 OCR：

| 页面来源 | 配置 | 主/次证据 | 结构阶段行为 |
|---|---|---|---|
| `native_text`（legacy，单栏或多栏） | 显式 `pdf_text.mode: native`，忽略 `ocr.secondary.enabled` | PDF 原生文本块/坐标/字体；报告兼容字段为 `single_ocr`；多栏页额外记录 `column_major` 阅读顺序 | 不运行视觉 OCR、`ocr-correct` 或 OCR 共识；同页上标+小字号的高置信度候选本地接受，其余候选交 Subagent。 |
| 视觉 OCR | `false` | 只有 `pages/` 主 OCR | 忽略旧共识产物；明确的高置信度 OCR 脚注可本地接受，疑难候选交给 Subagent。 |
| 视觉 OCR | `true` | 当前 `ocr_secondary/` + `ocr_consensus.json` | 共识报告和次 OCR sidecar 参与脚注/插图候选比较；差异页必须复核。 |

准备阶段把 `source_kind` 和 `ocr_evidence_mode` 写入候选报告和 manifest。脚注和插图都从
同一个 `pdf_evidence_mode()` 获取该值，并通过 `require_current_consensus()` 检查双 OCR
检查点；它们不直接依赖彼此的 prepare 模块。对视觉 OCR，
校验/应用阶段比较当前配置、候选报告、主/次 sidecar 哈希和共识检查点；对原生文字稿，
比较原生 sidecar 哈希，并明确排除次 OCR 文件。任何一项变化都阻断旧结果。这样单 OCR
运行不会误读上一次双 OCR 的 `ocr_consensus.json`，原生文字运行也不会因为目录中存在旧
共识文件而切换证据路径。

整页插图只在 `illustration_bindings.json` 中出现 `full_page_insert` 时影响 `PageMerger`；
脚注只在 Subagent 将候选标记为 `footnote_start`、`footnote_continuation` 或
`footnote_definition` 时移动。脚注的作用域来自 `tree_progress.json` 的完整 `unit_id`，
而不是顶层 TOC；相同数字在不同实际单元中可以重新开始。跨页顺序由页面和 block 顺序保留，
所以“正文 → A 脚注 → B 正文 → A 脚注续文 → B 脚注”不会被错误重排。

集中尾注走独立的确定性归一化路径：`tree_progress.json`/TOC 中 `type: notes` 的文件是
尾注定义源，不要求 Markdown 内再次出现 `# Notes` 标题。`footnote-apply` 在生成
`footnote_normalized/` 时识别 `N. text`、`N text`、`<sup>N</sup> text` 和已有
`[^N]: text`，并只把与这些定义匹配的普通单元上标转为 `[^N]`。原始 OCR 单元不覆盖。
可按章重启的重复编号由 `endnote_scopes.py` 配对后改写为 `fnc-<scope>-<number>`；无法
唯一配对（缺少章标题、scope 没有正文 span、引用/定义数量不符）时阻断归一化。
`footnote_normalization.json` 的 `endnotes` 子报告记录最终定义键、转换引用数、scope 统计、
双层编号识别数、重复定义和孤立定义，供后续审计。

Polish 校验依据文件角色而不是正文标题检查尾注；EPUB 构建还会拒绝 Notes 单元中残留的
原始编号定义或正文中残留的对应上标。这样页脚脚注仍由页面版面证据决定，集中尾注则由
TOC 语义角色决定，两者不会互相吞并。

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

1. 文本比较规范化换行、Markdown 外层格式、链接和脚注分隔符；
2. layout 比较脚注标签、脚注编号序列和垂直范围。

主/次 OCR 可能使用不同的段落粒度。行数、数字序列和脚注垂直范围差异会保留在诊断与候选
报告中；实质文本差异、空结果和共同漏检哨兵才触发正文视觉复核。脚注/插图模块仍可把
候选存在性差异交给各自的 Subagent。

共识 record 的职责边界如下：

- `comparison.status` 和顶层 `action` 只表示正文 OCR 是否需要 `ocr-correct`；`action` 不再
  因 `compare_ocr_layouts()` 的脚注标签、编号序列或垂直范围差异单独变为 `visual_review`。
- `comparison.diagnostics.line_counts_differ` 和 `numeric_markers_differ` 是审计信号；是否
  阻断正文复核由统一的文本比较阈值决定。
- `layout_comparison` 与 `layout_review` 仍必须保留，供 `footnote-prepare` 和
  `illustration-prepare` 作为候选证据使用。它们不能覆盖主 OCR 文本，也不能由本地脚本直接
  决定脚注归属。
- `ocr_consensus.json` 的 schema 当前为 4。`ocr-consensus-rebuild` 必须读取当前
  `pages/ocr_progress.json`、主/次 Markdown、两套 layout sidecar 和当前配置哈希，逐页重写
  record；它不调用 OCR 后端。任何缺失或过期输入都使 manifest 不完整。

因此，维护者不能通过提高 `max_line_delta` 或放宽数字正则来“修复”本问题；正确做法是保持
正文共识与脚注/版面证据分层，并让真正的内容差异进入页图复核。

`agree` 只表示该页通过筛查；`review_required` 表示必须把页图、主 OCR 和次 OCR 一起交给
`ocr-correct` 的 Subagent。任何后端都不能在共识阶段自动取代主 OCR。共同漏检由固定抽样和
页面密度异常信号补充发现。

#### 3.1.2.1 原生文字 PDF sidecar 合同

原生文本提取是 legacy opt-in：只有 `pdf_text.mode: native` 才可能选择直接文本提取，并在
探测不到高置信度原生文字时失败；默认的 `pdf_text.mode: auto` 和 `ocr` 都强制使用视觉 OCR。
探测结果始终保留为 `classification`/`detected_recommendation`，但 `auto` 的
`recommendation` 为 `ocr_required`、`selection` 为 `auto_ocr_native_legacy`；显式 native 选中
文本层时才写 `recommendation: use_text_layer`、`selection: forced_native_legacy` 和
`native_legacy: true`。探测器同时检查文本行的横向分布；如果发现两个具有明显 gutter、足够
行数和纵向重叠的稳定栏，原生提取器会依据文本 span 的起始 x 坐标拆分左右栏，再按“左栏自上
而下、右栏自上而下”输出，而不是把整本 PDF 退回视觉 OCR。报告同时记录
`layout_mode: multi_column`、`reading_order: column_major`，以及 `requested_text_mode`、
`detected_recommendation` 和 `selection`，用于恢复时拒绝复用不匹配的缓存。probe schema 为
2；schema 1 的旧 probe 不再复用，旧 `auto` 原生选择必须显式改回 `mode: native`。

单栏和多栏原生 PDF 的 `extract_native_text_pages()` 都生成与 OCR 页相同的 `pages/page_NNN.md`，但同时生成带源坐标的
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
  `0..1000` 坐标启发式。`layout_evidence.normalized_bbox()` 先按 page box 转成 `0..1`
  比例。`bottom_ratio: 0.50` 和 `bottom_intersection_ratio: 0.08` 只是候选召回地板，不能
  单独决定脚注；页面自身的内容尾部和相对块间距会产生自适应版面证据，独立的脚注标签、同页
  上标引用或原生字号/标记证据才可支持本地接受。正文流异常、证据冲突或只有连续数字的区域
  必须降级到视觉审计。
- `body_font_size` 是该页文本 span 字号的中位数。它只用于计算候选块的
  `font_size_ratio`；字号小不等于脚注。
- 每个原生文本 block 的 `source_block` 指向 PDF 原始 block，`order` 保持页面阅读顺序。
  原生文本提取会在一个 PDF block 内按“页底数字开头”拆成多个 layout block，使同页多条
  脚注可以独立复核和定位。
- 识别到的短数字/符号上标在 Markdown 中保留为 `<sup>…</sup>`；sidecar 的原始 `text`
  不被改写。PDF superscript flag 和“字号明显小且位于行上方”只提供上标证据。
- 原生候选检测要求块位于页面候选尾部、以数字加分隔符开头且不是纯数字页码。默认只有同页
  更早位置存在对应 `<sup>N</sup>` 引用、字号比例不超过 `0.88`，并且所在区域与页面相对
  内容尾部对齐的候选，才标记为 `confidence: high`/`local_candidate`，由 apply 后端按
  `footnote_start` 处理；其他候选标记为 `review_required`。字号、坐标、字体和上标仍只是
  证据，不能把普通上标或引用单独升级成脚注。
- `footnotes.auto_accept: true` 会让清晰的页面脚注区域走本地路径，只把异常区域交给视觉
  Subagent；设为 `false` 或使用 CLI 的 `--review-all` 才会把所有候选区域纳入视觉复核。
  `footnotes.native_max_font_ratio`（默认 `0.88`）仍只调节原生 PDF 的字号门槛。

候选报告还会写入 `bottom_detection`：页底 block 总数、页底数字 block 总数、进入候选的数字
block 数、被排除的页眉/页脚与纯数字页码数，以及 `suspected_missed_count`。达到审计条件的漏检
还会进入 `audit_candidates` 和 `footnote_visual_audit_manifest.json`，因此不会只停留在统计层。
只要存在异常区域，准备阶段会复用 OCR 校正阶段的当前 PDF 页面 PNG，并尽量生成区域裁剪图，
把 `visual_file`/`region_visual_file` 写入 manifest 和 Prompt；没有源 PDF 的 sidecar-only 运行会
明确记录图像证据不可用。Subagent 可提交新版 `region_audits`，旧版 flat `decisions` 仍可恢复。

脚注准备报告还记录 `sidecar_sha256`。`footnote_decisions.json` 只能引用候选报告中的
`page`/`block` 地址；`footnote-validate` 会检查 source kind、sidecar 哈希、决定覆盖率、
脚注键和 continuation 的可追溯性。`footnote-apply` 再用相同地址在当前章稿中唯一定位
文本，定位不到、定位到多个或命中正文不唯一时阻断，不猜测替换。

两类来源之所以可以复用同一套 apply 后端，是因为它们都最终提供：

1. 有序的 `page`/`block` 地址和紧凑 review window；
2. 相同的 decision roles（`body`、`citation`、`bibliography`、三种脚注角色）；
3. `tree_progress.json` 的完整 `unit_id` 作用域与跨页顺序；
4. sidecar/source/checkpoint 哈希，可在 apply 前重新验证。

#### 双 OCR 脚注差异合同

主 OCR 可能把页底脚注识别成普通正文，或完全漏掉脚注；次 OCR 也可能把同一脚注拆成多条
物理行。`footnote_prepare` 不把这些行逐条交给 Subagent，而是对 secondary-only 候选按物理
block 顺序聚合：候选的 `block` 是首行锚点，`block_indices` 和 `block_span` 是窗口事实，
Subagent 只需为首行写一条决定。

secondary-only 的 moved decision 必须包含：

| 字段 | 约束 |
|---|---|
| `source: "secondary"`、`page`、`block` | 必须精确命中 `secondary_only_candidates` 的首行锚点 |
| `text` | Subagent 根据页图确认后的最终可见脚注文本；不能直接信任次 OCR 摘要 |
| `source_file` | 当前 `tree_progress.json` 中覆盖该页的 refinement unit Markdown 文件名 |
| `primary_disposition` | `absent` 表示主 OCR 中没有该脚注；`remove` 表示主 OCR 存在重复块 |
| `primary_block`/`primary_blocks` | 仅 `remove` 时必填；apply 会用主 sidecar 唯一定位并删除这些块 |
| `marker_context` | 同一逻辑单元内同编号 marker 不唯一时必填，用来选择正文引用 |

`primary_disposition: absent` 不允许附带主 block 地址；`remove` 找不到、跨 refinement unit
或无法唯一定位主 block 时失败。secondary-only 脚注的正文 marker 也不再使用整个源文件的
“最后一个同编号 marker”作为隐式选择：候选不唯一且没有 `marker_context` 时，
`footnote-apply` 生成阻断报告，不写 `footnote_normalized/`。这是防止复杂脚注被重复追加或
错误绑定到另一处引用的故意 fail-closed 设计。

- `refine/footnote_handoff.py`：生成脚注 Subagent 的 manifest、Prompt 和按 refinement unit
  分片的稀疏上下文，并集中执行交接文件名和路径安全校验；不重新识别页面候选。
- `refine/footnote_validation.py`：校验 Subagent 决策的 schema、候选地址、区域审计、当前
  sidecar/共识哈希和续文来源，归一化后才允许 `footnote-apply` 消费；不修改原始 Markdown。
- `refine/footnote_continuation.py`：评估跨页续文的版面/语义证据并汇总可审计信号；通过
  惰性候选构造回调复用候选逻辑，不直接决定脚注角色。
- `refine/footnote_signals.py`：集中维护脚注/引用/页眉页脚标签、编号边界和上标解析，
  供候选识别与续文证据层复用。
- `refine/footnote_layout.py`：只提供 sidecar 的归一化版面事实、页尾相对布局、区域 bbox、
  漏检召回统计和紧凑 block 投影；不判断脚注语义，也不写工作流产物。
- `refine/footnote_candidates.py`：构造 OCR/native-text 候选、有限 review window、次 OCR 物理行
  聚合和主次候选匹配；只返回候选事实，不做区域级接受或最终角色决定。
- `refine/footnote_regions.py`：把候选事实聚合为页面区域，集中执行区域边界、独立证据和本地
  接受策略；不构造候选、不渲染图像、不校验 Subagent 决定。
- `refine/footnote_structure.py`：加载 refinement unit/TOC 角色并把 typed unit 边界映射到
  sidecar block；为脚注候选提供保守排除范围，也供 handoff 生成稀疏 unit context。
- `refine/footnote_windows.py`：只生成相邻页面的有序 review window，不猜测跨页续文角色。
- `refine/footnote_visual.py`：复用 OCR 校正的整页渲染，生成区域裁剪图并写入区域级视觉审计
  manifest；不改变候选、决定或 Markdown。

来源差异只停留在“如何生成候选”和“需要多少 Subagent 复核”：OCR layout 可以在明确
标签和几何证据下产生高置信度候选，native layout 则只自动接受同页上标与字号双重匹配的
候选，其余候选保留给 Subagent。

### 3.1.4 PDF 阶段的复用边界

PDF 结构阶段采用“共享证据、分离语义”的边界：

| 共享层 | 负责 | 不负责 |
|---|---|---|
| `pdf_evidence.py` | 页面来源、OCR 证据模式、共识检查点 freshness | 脚注/插图候选的语义判断 |
| `layout_evidence.py` | sidecar JSON、block 文本、bbox 和坐标系归一化 | 修改 Markdown、决定是否移动内容 |
| `footnote_prepare.py` | 脚注阶段编排 | 页面扫描、区域策略、报告写入、Subagent 交接、决定校验、插图分类、页面物理合并 |
| `footnote_options.py` | 配置默认值、模式和选项解析 | 页面扫描、候选判定、Subagent 决定、Markdown 修改 |
| `footnote_consensus.py` | 共识复核页和次 OCR sidecar 路径解析 | freshness 校验、候选判定、Subagent 决定 |
| `footnote_page_scan.py` | 单页 primary/secondary 证据扫描和 page report | 最终脚注角色、报告写入、Markdown 修改 |
| `footnote_continuation_regions.py` | 连续数字区域、几何回溯和候选扩展 | 跨页语义评分、最终脚注角色、Markdown 修改 |
| `footnote_report.py` | 跨页窗口、视觉证据、统计和候选报告持久化 | sidecar 扫描、候选语义、脚注角色决定 |
| `footnote_contracts.py` | 决定角色和可移动角色集合 | 候选识别、视觉渲染、Markdown 修改 |
| `footnote_layout.py` | 归一化 bbox、页尾布局、区域事实和漏检统计 | 脚注语义、视觉渲染、Subagent 决定、Markdown 修改 |
| `footnote_candidates.py` | OCR/native 候选、review window 和双 OCR 候选匹配 | 区域接受、Subagent 决定、Markdown 修改 |
| `footnote_regions.py` | 页面区域分组、边界判断和本地接受证据 | 候选构造、视觉渲染、Subagent 决定、Markdown 修改 |
| `footnote_structure.py` | refinement unit/TOC 角色和 sidecar 排除映射 | 候选语义、视觉渲染、Subagent 决定、Markdown 修改 |
| `footnote_windows.py` | 相邻页面 review window 的有序构造 | 续文角色判断、候选修改、Markdown 修改 |
| `footnote_visual.py` | 整页/区域视觉证据和审计 manifest | 候选识别、脚注角色决定、Markdown 修改 |
| `footnote_handoff.py` | 脚注 manifest、Prompt 和稀疏 unit context | 候选识别、决定校验、插图分类 |
| `footnote_validation.py` | 决策合同、哈希 freshness 和角色归一化 | 候选识别、Markdown 修改、插图分类 |
| `footnote_continuation.py` | 跨页续文版面/语义证据和汇总 | 连续区域发现、最终脚注角色、Markdown 修改、插图分类 |
| `footnote_signals.py` | 共用标签、编号和上标识别信号 | 页面候选编排、Subagent 决策、Markdown 修改 |
| `illustration_prepare.py` | 整页插图候选、图片复核窗口和页面角色 handoff | 脚注归并、章节末尾注释 |
| `footnote_apply.py` / `page_merger.py` | 消费已验证决定并生成派生源稿 | 再次猜测 Subagent 决定 |
| `footnote_matching.py` | block 文本变体、唯一源文定位和非重叠编辑区间 | 扫描 sidecar、决定脚注角色、修改工作流状态 |
| `footnote_status.py` | 归一化 checkpoint 的哈希和 freshness 诊断 | 候选扫描、脚注物化、修改 Markdown |

新增 PDF 结构信号时，先判断它是共享的几何/证据事实还是某个领域的语义规则。前者进入
共享模块并补一组跨阶段测试，后者只进入对应的 prepare/apply 模块。命令 handler 只读取
配置、传递 CLI 覆盖和报告状态，不复制 sidecar 解析、证据模式或 checkpoint 校验。

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

EPUB 正文也使用 2.4 节的章节装箱器。`html-prepare` 先依据原始 TOC 生成
`chapter_groups`，再让 `write_worker_handoffs()` 按 TOC 顺序装入 `worker_handoffs/`；
默认允许相邻短分支共享一个 worker，超过文件数、token、单文件字节数、非空行数或章节数
上限时封箱。默认 `max_nonempty_lines` 为 350；超过该值的 EPUB 压缩单元会在准备阶段
生成有序 `.partN.md`，并在最终恢复时按原 mapping 合并。`Index` 和 `Bibliography`
即使未超行数预算也会独立成批。
大分支只能在自己的单元列表内拆分，不能和邻章拼接。元数据翻译始终是独立的
`translated_metadata.json` 任务，不计入正文 worker 的章节装箱。

`html_prepare_cache.json` 记录输入 EPUB、当前压缩单元和行数预算的哈希。第二次运行
`html-prepare` 在快照仍然有效时跳过 XHTML 解包、图片/CSS 重读和压缩，只重新生成实体、
术语上下文、元数据与 worker handoff。EPUB 实体提取默认只读取正文等高价值单元，
排除 `Index`、`Bibliography`、`Copyright` 和 `Cover`，排除清单和版本写入实体 manifest。

EPUB 的“合并”只影响 Subagent 的工作上下文：`adjacent_chapter_direct_file_contexts`
按文件提供术语条目，同时允许同一个 Subagent 维持相邻章节的风格连续；恢复和校验仍以
`compressed_units/<name>.md` 与 `translated_compressed/<name>.md` 为粒度。这样不会因为
共享 worker 而破坏 HTML 单元 1:1、目录 href/anchor 或构建时的文件映射。

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

### 5.0 TOC 标题恢复合同

视觉 OCR 关闭 `include_headers_footers` 时会从 `page_NNN.md` 删除被标为
`Page-Header`/`Page-Footer` 的块。`refine-local` 在生成 `ocr_markdown/` 前从证据目录
（固定为 `output/<title>/pages/`）的 `page_NNN.html` 或 sidecar `raw_html` 重建页眉/页脚
文本，并按 TOC 顺序尝试恢复：

- 正文字段来自当前 refine 选择的页面目录（可能是
  `ocr_corrected_pages/validated/`）；证据 HTML/sidecar 始终来自 `pages/`，两者不能混用。
- 同页多个页眉/页脚块按 DOM 顺序以空格合并后再比较；匹配只做字母数字 casefold 和
  `Chapter N:`/前导编号的确定性剥离，不做模糊相似度。
- 标题已存在、缺少页面证据、`source_kind: native_text`、无匹配、歧义和重复恢复都记入
  `toc_heading_recovery.json` 的 `skipped`，不阻断流程；只有精确匹配的标题才在内存中注入，
  `pages/` 不被改写。
- 报告至少包含 `schema_version`、`status`（`validated`/`no_recovery`）、`recovered`、
  `skipped`、`toc_sha256`、`page_markdown_fingerprint`、`evidence_pages_fingerprint`。
- `evidence_pages_fingerprint` 哈希 `page_*.md`、`page_*.ocr.json` 和 `page_*.html`；
  `tree_progress.json` 的 fingerprint 还包含恢复映射的稳定哈希
  `toc_heading_recovery_sha256`。缺少该键的旧 checkpoint 直接重新生成。
- 切换正文页面来源、OCR 证据模式或 HTML/sidecar 后，旧报告和旧 `tree_progress.json`
  checkpoint 不得复用。

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

单文件校验会先执行该文件绑定的可见 TOC heading 局部检查，报告写入
`toc_heading_bindings_local`；绑定失败时该文件从 `valid_files` 移除，对应的
`translated/validated/` 副本被删除，并写入 `retry_required`。单文件结果仍只提供
checkpoint，不能替代全量校验。源稿、实体表、TOC 或术语上下文变化
只应使受影响的 checkpoint 重新进入 `pending`；旧 manifest 没有单元级上下文哈希时，
为安全起见按整批重新处理。父 manifest 是完整审计/恢复索引，worker handoff 则是只含
当前 assignment 的最小运行投影。

polish 校验对续片的 TOC 保护是上下文感知的：只有文件开头、且确认为当前分支顶层祖先
的重复标签可记录为窄范围例外；书目和索引的页眉启发式则要求重复证据，不以单个短标题、
页码或无句号的出处条目为阻断依据。

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
