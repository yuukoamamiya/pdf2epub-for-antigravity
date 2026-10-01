# pdf2epub Agent 工作规范

本文件是本仓库翻译工作的唯一执行规范源。`docs/antigravity-workflow.md` 仅作补充说明；
执行任务时以本文件为准。

仓库模块职责和依赖方向见 `docs/architecture.md`；该文件仅作架构说明，不改变本文件的
执行规范和优先级。

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
  `unit_contexts/`。PDF 和 EPUB 翻译都必须优先读取 worker handoff 为当前顶层章节
  列出的稀疏上下文；普通章节把该章节命中的条目聚合为一次 `entries` 注入，超大章节
  拆分时使用跨多个分片的 `shared_entries` 加当前分片的 `local_entries`。不得把不同
  顶层章节的术语上下文混用。完整快照只用于审计和冲突复核，不能修改。没有外部表时也要尊重记录的
  `explicit_none`/`unconfigured` 状态，不得自行加载目录中的术语表。参考术语表快照
  使用 `reference_glossary_*` 名称，不能覆盖权威术语表，也不得反向写回原文件。
- `translate`、`polish`、`refine`、`extract-entities`、`translate-toc` 只准备交接或
  执行本地处理；命令成功不代表正文已经完成。
- `polish` 会按 `subagent.batching.max_concurrency` 生成
  `polish_worker_handoffs/`；`translate` 使用 `worker_handoffs/`。每个 worker
  只能处理自己 manifest 中的 `assigned_files`。PDF `translate` 按顶层章节生成
  handoff；超出文件/字节/token 限制的章节才在章节内部拆分，不能与其他章节合并。
  `max_files` 默认值为 5，配置值的有效上限为 8；文件数、字节数和 token 数任一达到
  限制都必须拆批。
- 父级 manifest 保留全书文件、统计、章节映射和上下文哈希，供审计与恢复使用；worker
  manifest 只能是当前 worker 的最小投影（当前文件、当前批次、当前文件统计/层级/术语哈希
  和必要的章节元数据），不得复制全书 `file_stats`、`chapter_groups`、推荐队列或审计快照路径。
  章节术语上下文只记录 `chapter_file_count`，不得重复写入完整 `chapter_files` 列表。
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

对所有 PDF，`polish` 都是翻译前的必经质量闸门，不是可选的版式优化；只有
`polish-validate` 通过后，才能提取实体表或准备正文翻译。高置信度原生矢量文本 PDF
只跳过视觉 OCR，仍必须用 `polish` 判断视觉换行与真实段落边界。原生文字稿的 polish
不得进行无依据的拼写或字形改写，重点是合并软换行并保留真实段落、标题和块级结构。
`polish-validate` 还会报告高置信度的残留页码/页边行；这些候选默认进入
`review_required`；首次出现时必须先让 Subagent 重新判断，复核后仍存在则升级为
`human_review_required`，不授权本地脚本自动删除或继续。仅在明确确认合法保留后使用
`--allow-review-warnings`。
EPUB、轻小说和 TeX 流程不使用这一 PDF 润色阶段。

PDF 的具体循环为：`ocr-pages → refine-prepare → refine-local → polish →
polish-validate`。
随后执行 `extract-entities → translate-toc → translate-toc-validate → translate →
translate-validate → build-epub`。可搜索但由扫描图像叠加 OCR 文字层的 PDF 仍必须重新
视觉 OCR。

PDF 纯转换模式使用 `pipeline: epub_conversion`（兼容别名
`mode: ocr_to_epub`），循环为：
`ocr-pages → refine-prepare → refine-local → polish → polish-validate → build-epub`。
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

## 2. PDF 扫描件翻译流程

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
   扫描 PDF 和可搜索 OCR PDF 都生成视觉 OCR 的 `pages/page_XXX.md`。
4. 执行 `refine-prepare`。然后打开工作区 Subagent，读取
   `output/<title>/refine_subagent_prompt.md`，结合 `pages/` 写入 `toc_tree.json`。
   Subagent 应从书名页/版权页提取作者和出版社，并按内容标注 `notes`、`bibliography`、
   `index`；普通正文节点不写 `type`。
5. 执行：

   ```text
   uv run pdf2epub -c config.yaml refine-local --resume
   ```

   本地程序校验页码范围、父子关系、兄弟节点重叠，并生成 `ocr_markdown/`。
   `tree_progress.json` 会锁定 TOC/OCR 指纹；输入变化后必须重新生成受影响单元。
6. 所有 PDF 都必须执行 `polish`，打开工作区 Subagent 读取
   `polish_subagent_prompt.md`，并按 `polish_worker_handoffs/` 中各 manifest 的
   `assigned_files` 写入 `polished_markdown/`，然后运行 `polish-validate`。
   对 OCR/混合型 PDF，该步骤用于修复 OCR 换行和明显 OCR 错字；对原生矢量文本 PDF，
   该步骤用于从视觉行重建语义段落，同时保留原文字符和块级结构。未通过
   `polish-validate` 不得继续实体提取或翻译。
   polish 还必须清除已确认的页眉、页脚、独立印刷页码和人工 OCR 页码标记；
   诸如 `Preface XII` 的短标题加页码组合在确认属于页边装饰后应整行删除。
   正文数字、标题编号、日期、引用、脚注、参考文献和索引中的页码不得删除。
   润色不得把普通粗体、罗马数字、编号或序数上标升级成 Markdown 标题；已确认的
   `<sup>N</sup>` 注脚才可规范化为 `[^N]`。

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

5. 执行 `translate`。该命令会生成 `translate_subagent_prompt.md`、manifest 和按
   顶层章节划分的 `worker_handoffs/`。立即打开工作区 Subagent：每个 Subagent 只处理
   自己 handoff 的 `assigned_files`，同名译文写入 `translated/`；超过 30,000 字节的大
   单元仍必须独立派发，但不得跨章节合并。Prompt 会同时提供一次按预算稀释的全书 TOC
   轮廓，以及当前章节精确的已翻译 TOC 标题/子标题上下文；后者对输出标题具有最高权威。
   术语上下文按章节 worker 聚合，完整快照只用于审计，不能修改。标题绑定允许安全的
   格式规范化，但不允许近义词替换。
6. 每完成一个单元可运行：

   ```text
   uv run pdf2epub -c config.yaml translate-validate --file <文件名>.md
   ```

   单文件结果只作为 checkpoint，不能替代最终全量校验。
7. 所有单元完成后运行 `translate-validate`。`retry_required` 中的明确错误直接重新交给
   Subagent；首次 `review_required` 也重新派发。若报告出现 `human_review_required`，必须暂停
   并询问人工，不能继续自动重试。只有完成复核后才可显式使用
   `translate-validate --allow-review-warnings` 放行。
8. 执行打包：

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
5. 读取第二次生成的 `translate-html_subagent_prompt.md` 和 manifest，打开工作区
   Subagent，将同名译文写入 `translated_compressed/`。只处理 `pending_files`。
   正文 worker 按顶层 TOC 分支隔离稀疏术语上下文；大分支拆分时使用
   `shared_entries` 和 `local_entries`，不得读取或混用其他分支的上下文。
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
