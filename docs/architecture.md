# pdf2epub 架构说明

本文说明仓库当前的代码分层和模块边界，服务于维护、扩展和排查问题。
它不替代执行规范；翻译任务仍以 [`AGENTS.md`](../AGENTS.md) 为唯一执行规范源，
具体操作流程见 [`antigravity-workflow.md`](antigravity-workflow.md)。

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
            ├── refine/              PDF 结构、分页和单元生成
            ├── html_translation/    EPUB HTML 解析、压缩、校验和重建
            ├── tex_translation/     TeX 项目扫描、编译和源文件解析
            └── epub/                EPUB 通用构建能力

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
- `ocr.py`：执行唯一允许调用 OCR 服务的工作流入口。
- `refine.py`：准备结构判断 handoff，或调用本地分页/单元合并。
- `markdown.py`：PDF Markdown 的 polish、translate、readiness 和 validation 编排。
- `page_furniture.py`：已有 PDF 译文的页眉页脚修复交接和校验编排。
- `entities.py`：生成和校验书内实体表 handoff。
- `toc.py`：生成和校验独立的 PDF TOC 翻译 handoff。
- `pdf.py`：校验源稿/译稿并构建 PDF 路径 EPUB。
- `html.py`：编排 EPUB HTML 提取、实体表、正文/元数据 handoff、校验和重建。
- `novel.py`：编排轻小说文本提取、校验和重建。
- `tex.py`：编排 arXiv/本地 TeX 项目准备、校验和编译。
- `glossary.py`：扫描外部术语表候选，并区分严格匹配的权威表与显式选择的跨语言只读参考表；不自动选择模糊候选。

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
- `markdown_validation.py`：提供纯函数式的 Markdown 风险检测、目标语言审计和规范化辅助函数。
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
refine-prepare + 工作区 Subagent
  → toc_tree.json
refine-local
  → ocr_markdown/ + tree_progress.json
polish + 工作区 Subagent + polish-validate（所有 PDF 必需；review_required 默认阻断，持续则人工）
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

测试入口为 `uv run pytest -q`。代码重构不应读取、改写或重新生成用户的书稿和译文输出；
涉及实际翻译时必须重新遵守 [`AGENTS.md`](../AGENTS.md) 的 Subagent 总闸和开工检查。

### 本地工作文件归档

仓库根目录只保留源码、配置模板、文档和可重复运行的测试。一次性翻译脚本、实验切片、
比对结果、审计报告和调试输出统一放在 `.work/<task>/`；`.work/` 是本地工作台，不进入 Git。
PDF/EPUB 流程自己的 scratch 文件放在 `output/<title>/scratch/<task>/`，而 `scripts/` 只放
可复用且准备纳入仓库的工具。正式术语表仍属于 `glossaries/`，不应被移动到工作台。历史
临时文件迁移时要先确认用途，并采用可恢复的移动，不得批量删除未知文件。
