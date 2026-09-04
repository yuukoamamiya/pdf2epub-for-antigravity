# Agent Execution Guidelines

## Auto-Execution & Approval Policy
- **工作区内自动执行 (Auto-Execute within Workspace)**：在当前仓库目录内的代码编辑、文件创建、配置修改与命令测试可以自动执行；涉及仓库外路径或不可逆操作时必须先确认。
- **非阻断式规划 (Non-Blocking Review Gates)**：日常任务、调试与代码改动，工件默认设置 `RequestFeedback: false`，不产生多余的阻断式确认卡片。
- **工作区外严格防护 (Strict Non-Workspace Protection)**：涉及工作目录以外的敏感系统路径时，必须明确告知用户并获得许可。

## Git 维护约定
- 本仓库由 Codex 直接维护，日常修改直接提交到当前主分支，不创建或切换到 `codex/` 等临时分支。
- 用户明确要求上传 GitHub 时，提交并推送到本仓库的 `fork` 远程；推送前运行相关测试并核对工作区状态。

## Command & Scripting Preference
- **优先使用 Python 脚本**：对于文件读写、数据处理、JSON 凭证配置或复杂逻辑，优先使用 Python 脚本（如 `uv run python` 或独立 `.py` 脚本）执行，确保跨平台确定性与 UTF-8 编码安全。
- **优先使用 Git Bash / POSIX 兼容命令**：运行自动化工作流和脚本时，优先使用 `bash` 兼容语法或 POSIX 风格脚本，避免依赖平台特定的复杂终端特性。
- **减少使用 PowerShell**：尽量避免生成复杂的 PowerShell（`pwsh`）单行指令、复杂转义或多行 Heredoc 字符串，防止因引号解析与控制台编码导致进程挂起或误阻断。

## 标准书籍翻译工作流规范 (Standard Translation Workflow SOP)
**【核心原则：严禁重新造轮子，严禁编写临时脚本】**
当用户在任何会话中提出“翻译某本书”、“处理 EPUB/PDF”等需求时，**必须严格遵循以下精确 SOP 步骤与 Subagent 协同协议**：

### 总体执行规则

- 需要阅读、判断、翻译或润色内容时，在 Antigravity IDE 中使用工作区
  Subagent，按本地命令生成的 `*_subagent_prompt.md` 和 manifest 执行。
- 不要假设存在 `define_subagent`、`invoke_subagent` 等固定工具名；使用当前
  IDE 提供的 Subagent 入口即可。
- Subagent 必须直接读写工作区文件。只在聊天中返回译文而不写目标文件，视为
  未完成。
- Python 命令只负责拆分、压缩、校验、合并和打包，不调用翻译 API。
- 只有 `ocr-pages` 可以按 OCR 配置调用本地或远程 OCR 服务；OCR 服务不得
  被用于翻译、润色或结构判断。
- 本地校验若发现高置信度拒答或免责声明，会在报告的 `safety_blocked` 中列出，
  该单元不得进入 `validated`，也不得通过打包；不要通过改写提示词来绕过安全限制。

---

### 1. EPUB 高保真翻译工作流 (EPUB Pipeline SOP)

#### Step 1: 检查配置并准备任务
1. 扫描 `input/` 目录下的 EPUB 文件（也支持 MOBI、AZW3），确认输入文件。
2. 从 EPUB 元数据确认书名和源语言；在本地 `config_epub.yaml` 中填写 `title`、
   `input_epub`、源语言和目标语言。不要覆盖用户的真实配置文件，除非用户明确要求。
3. 配置至少应类似：
   ```yaml
   title: "提取到的书名"
   input_epub: "实际文件名.epub"
   translation:
     source_language: English # 依据元数据判断
     target_language: Chinese
   html_translation:
     epubcheck_mode: warn
   ```

#### Step 2: 本地离线结构拆分与压缩
1. Agent 执行命令：`uv run pdf2epub -c config_epub.yaml html-prepare`
2. 产物目录：`output/<title>/compressed_units/*.md`、对应 mapping，以及 `metadata_translation_source.json` 和 `metadata_translation_prompt.md`。

#### Step 3: 调度 `book_translator` Subagent 协同翻译
1. **检查与断点续传**：比对 `output/<title>/compressed_units/` 与 `output/<title>/translated_compressed/`，找出尚未完成或校验未通过的 `.md` 文件列表。
   - 只有上一次本地校验报告中、且源文件 SHA-256 未变化的文件才算已完成；仅凭目标文件非空不能跳过。
   - manifest 中的 `file_stats`、`recommended_batches` 和 `oversized_files` 用于安排任务；超过 token 上限的文件要按完整翻译单元拆分。
2. **分批与并发粒度**：
   - 对于长篇或学术大章节（>30KB），推荐**单章节派发一个独立 Subagent**，避免单会话因 Token 截断导致的拼接/换行错误；
   - 对于前后置元数据、短章节（<20KB），可 3~5 篇一组并发派发。
3. 在 Antigravity IDE 中使用工作区 Subagent，读取生成的
   `translate-html_subagent_prompt.md` 和 manifest；不要从 Python 创建模型客户端。
   - **Subagent 必须遵守的翻译铁律**：
     ```markdown
     请翻译以下 EPUB 压缩单元文件：
     - 源文件路径：output/<title>/compressed_units/<file>.md
     - 目标文件路径：output/<title>/translated_compressed/<file>.md
     - 翻译语言：从 <source_language> 翻译为 简体中文

     【翻译执行铁律】：
     1. 【非空翻译单元 1:1 对齐】：源文件有 N 个非空翻译单元，输出文件必须保持 N 个非空译文行（每行对应一个翻译单元）。严禁在段落内部插入任何多余换行符 \n。
     2. 【<div> 容器保全】：只有源文件该行本身被 <div>...</div> 包裹时，翻译后才保留同一组 <div>...</div>；源文件没有 <div> 时严禁自行添加。结构容器由 mapping 在重构阶段恢复。
     3. 【HTML 标签绝对保全与顺序一致】：严禁修改、删除或丢失任何 HTML 标签及属性（如 <span class="...">, <a>, <em>, <i>, <b>, <ruby>, <rt>, <img> 等），仅翻译标签包裹的文本内容。严禁合并相邻标签（例如 <span>A</span> (<span>B</span>) 必须翻译为 <span>甲</span> (<span>乙</span>)，保持两组独立标签）。<i> 标签必须保持数量、顺序和嵌套关系；例如 `<i>A, B</i>` 只能译为同一组 `<i>甲、乙</i>`，不能拆成两组或删除。
     4. 【实体与占位符保全】：源文本中的 `&amp;`、`&lt;`、`&gt;`、`<a/>` 和其他实体/占位符必须逐字保留，只翻译周围文本。
     5. 【直接写回文件】：将纯翻译内容直接写入 manifest 指定的目标文件。严禁在输出中添加 Markdown 代码块（```）包裹！
     6. 【写入后自检】：Subagent 完成每个文件后，应运行 `uv run pdf2epub -c config_epub.yaml html-validate --file <文件名>`；只有 exit code 为 0 才能报告该文件完成。全部文件完成后仍必须运行全量 `html-validate`。
     ```
4. **元数据交接**：Subagent 还必须按 `metadata_translation_prompt.md` 读取元数据输入，并写入 `output/<title>/translated_metadata.json`。
   - `original_title` 保持原英文书名不变；
   - 中文译名写入 `translated_title`；
   - 章节目录翻译写入 `toc[].translated`，`href`、`anchor`、`level` 和顺序原样保留；
   - `preserved_metadata.author` 与 `preserved_metadata.publisher` 必须逐字复制，禁止翻译或改写；
   - `translated_description` 和 `translated_rights` 始终保留为顶层字段；源字段为空时值可为空，源字段非空时必须翻译。
   - 版权声明写入顶层 `translated_rights`（如“保留所有权利”）。
5. 可以分批处理多个单元，但每次只处理 manifest 的 `pending_files`；不要覆盖
   `completed_files`，除非本地校验明确指出该文件无效。

#### Step 4: 本地离线全量质量校验
1. Agent 执行命令：`uv run pdf2epub -c config_epub.yaml html-validate`
   - 若出现 `Line count mismatch` 或 `Tag mismatch`，定位具体报错的单元（如 `bm01.md`）；
   - 将报错原因附带在 Prompt 中，重新调度 `book_translator` 仅重译并修复该单元；
   - 重新执行 `html-validate`，直到 100% 单元通过校验。

#### Step 5: 逆向重构与生成最终 EPUB
1. Agent 执行命令：`uv run pdf2epub -c config_epub.yaml build-html-epub`
   （校验失败时默认拒绝打包；仅预览时才显式使用 `--allow-partial`。）
2. 生成最终文件：`output/<title>/<title>_translated.epub`（或中文书名.epub）。
3. 向用户汇报完成并提供 EPUB 产物路径。

---

### 2. PDF 扫描件翻译与精修工作流 (PDF Pipeline SOP)

#### Step 1: 检查配置
1. 扫描 `input/` 目录下的 PDF 文件，确认输入文件和书名。
2. 在本地 `config.yaml` 中填写 `title`、`input_pdf`、语言和 OCR 后端；不要自动
   覆盖用户的真实配置。

#### Step 2: 页面 OCR 提取
1. Agent 执行命令：`uv run pdf2epub -c config.yaml ocr-pages --resume`
2. 产物目录：`output/<title>/pages/page_XXX.md` 与 OCR 布局信息。

#### Step 3: 结构分析与章节合并
1. Agent 执行命令：`uv run pdf2epub -c config.yaml refine-prepare`
2. 在 Antigravity IDE 中让 Subagent 阅读 `output/<title>/refine_subagent_prompt.md` 和 `pages/`，从书名页/版权页提取作者与出版社，并根据实际内容自动为注释、参考文献和索引节点标注 `type: notes`、`type: bibliography` 或 `type: index`；普通正文节点省略 `type`，最后写入 `output/<title>/toc_tree.json`。
   `refine-prepare` 同时生成 `pagination_map.json`；它只是 Roman/Arabic 书内页码的辅助映射，物理 OCR 页码仍是范围判断的权威。
3. Agent 执行命令：`uv run pdf2epub -c config.yaml refine-local --resume`
4. 产物：`output/<title>/toc_tree.json` 与 `output/<title>/ocr_markdown/chapter_XXX.md`。`toc_tree.json` 中的节点 `type` 是 Subagent 根据内容做出的语义分类；`refine.oversized_unit_split` 只负责本地按 token 阈值拆分，不要求用户手工填写内容类型。
5. **TOC 校验**：`refine-local` 本地检查章节重叠、父子范围和缺失页面；若失败，修正 `toc_tree.json` 后重新执行。节点 `type` 只使用 Prompt 约定的内容角色值，不要把 token 大小或拆分策略写进节点类型。
   `refine-local --resume` 会比较 `tree_progress.json` 中保存的 TOC/OCR SHA-256
   指纹；输入变化时自动废弃旧工作单元并重新生成。

#### Step 4: Subagent 润色或翻译
- **若为翻译需求**：
  1. 若执行过 `polish`，先运行 `polish-validate`；润色 Prompt 必须禁止把
     普通粗体、罗马数字或编号文字升级为 `#` 标题，并要求将已确认的 OCR
     `<sup>N</sup>` 注脚和章末数字注释规范化为 Markdown 注脚。数学、表格和
     序数上标不得误转为注脚。
  2. 在翻译前执行 `extract-entities`，让 Subagent 阅读实际翻译源稿并写入
     `translation_entities.json`，然后运行 `extract-entities-validate`。该词表
     是后续所有翻译 Subagent 的只读统一术语上下文；实体提取完成前不要执行
     `translate`。确实不需要术语表时，才显式使用 `translate --skip-entities`。
  3. 执行 `translate`，让 Subagent 按 `translate_subagent_prompt.md` 读取
     manifest 中实际选定的源目录（默认优先
     `polished_markdown/validated/`，也可由 `translation.source_stage` 指定
     `ocr` 或 `polished`），把同名译文写入 `translated/`；该命令同时准备
     TOC 翻译交接文件，也可用 `translate-toc` 单独重跑目录任务。
  4. 让 Subagent 按目录翻译 prompt 读取 `toc_tree.json`，写入
     `toc_tree_translated.json`；完成后可运行 `translate-toc-validate`。
  5. 保持 Markdown 标题层级（`#`, `##`）、公式（`$...$`）、脚注（`[^...]`）
     和图片链接原样不变；完成后运行 `translate-validate`。
  6. 若节点使用 `type: bibliography` 或 `type: index`，按 translate manifest
     中的 `file_roles` 和专用规则处理。参考文献保留书目身份字段、页码和引用
     标点；索引保留层级、页码、范围和交叉引用，不得省略条目。
  7. `translate-validate` 的 `bilingual_warnings` 仅是长英文原文未变化的预警，
     不是自动阻断条件；应人工检查后再决定是否让 Subagent 重写。
     报告中的 `diff_summary` 可用于定位行数、标题和代码围栏变化。
- **若仅为版式精修需求**：
  1. 执行 `polish`，让 Subagent 按 `polish_subagent_prompt.md` 修复 OCR 文本
     断行与格式，写入 `output/<title>/polished_markdown/`；完成后运行
     `polish-validate`。

#### Step 5: 离线打包生成 EPUB
- 生成中文翻译版：`uv run pdf2epub -c config.yaml build-epub --translated`
- 生成原版精修版：`uv run pdf2epub -c config.yaml build-epub`
- 使用 `build-epub --translated` 时，会同时从英文源稿生成
  `output/<title>/<safe-title>_en.epub`，然后再生成中文 EPUB；不再额外生成英文 Markdown。
- 没有独立 Markdown 文件的叶子子章节使用稳定锚点（如 `#toc-3-7-1`）链接到父章节正文，
  不因父子节点合并而产生失效目录项。

### PDF 翻译的术语与注脚约束

- `extract-entities` 读取 `translation.source_stage` 实际选中的源稿；默认语言来自
  `translation.source_language` 和 `translation.target_language`，不会假定日文。
- 当前 PDF 构建器生成的是 EPUB 2 兼容包，并提供可点击的正文—注脚双向链接；
  `[^N]` 规范化不会被表述为所有阅读器都支持的 EPUB3 弹窗。若要启用真正的
  EPUB3 `epub:type="noteref/footnote"` 语义，需要另行升级包格式。

### Windows 脚本规范

复杂的批量替换、JSON 写入和正则处理不得写成 PowerShell 内联的 `python -c`。
优先使用仓库中已有或可复用的 UTF-8 脚本，并显式指定文件编码；不要为一次性
翻译交接制造不可追踪的临时脚本。

---

### 3. 轻小说 EPUB 翻译

1. 执行 `translate-novel -i <input.epub>`，生成 `novel_units/`、manifest 和
   `novel_subagent_prompt.md`。
2. 让 Subagent 只处理 manifest 的 `pending_files`，将同名译文写入
   `translated_novel/`，并按 `metadata_translation_prompt.md` 写入
   `translated_metadata.json`。
3. 作者名和出版社原样保留；保留图片标记和段落边界，不添加说明或代码围栏。
4. 运行 `translate-novel-validate`，通过后运行 `build-novel-epub`。
5. 额度中断时先校验，再用原命令加 `--resume`，不要删除已有结果。

### 4. arXiv / LaTeX 翻译

1. 执行 `translate-arxiv <source>`，读取生成的
   `.pdf2epub/tex_subagent_prompt.md` 和 `tex_subagent_manifest.json`。
2. 让 Subagent 只处理 `pending_units`，将译文写入 `translated_tex_units/`。
   不要直接修改 `source/` 或把 `project/` 当作翻译交接目录。
3. 完成后运行 `translate-arxiv-validate --output-dir <run_dir>`；该命令从完整
   单元重建 `project/` 并进行本地编译。
4. 额度中断时用原命令加 `--resume`，只处理 `pending_units`。

### 5. 断点续传和错误处理

1. 不要因为额度耗尽删除输出目录或源文件。
2. 先运行对应的 `*-validate`，根据报告定位缺失、空白或结构错误文件。
3. 使用 `--resume` 重新生成 manifest；Subagent 只处理 `pending_files` 或
   `pending_units`。
4. 修复后重复校验，全部通过才允许打包。元数据 JSON 必须整体重写为合法 JSON。

### 6. 安全与合规红线 (Security Guardrails)
- **严禁逆向伪装**：严禁在代码中硬编码或向请求头注入未经授权的内部项目 ID（如 `project-8dcc0e99-48d6-44c4-b50`）或外部冒用 `vertex_adc.json`。
- **全流程安全**：翻译与精修统一在 Antigravity 内通过 Subagent 读写工作区文件进行，消耗 Antigravity / Gemini Pro 会话配额，兼顾零额外成本与 100% 官方合规安全。
