# pdf2epub

一个面向 Antigravity 的图书整理与翻译工作流。它把扫描或可搜索 PDF、EPUB、MOBI 和
AZW3 处理成结构可靠、可校验、可继续执行的 EPUB；需要翻译或判断版面结构的部分由
Antigravity 工作区 Subagent 完成，本地程序负责文件整理、校验和打包。所有 PDF 新运行
一律使用视觉 OCR。

这是一个“文件交接 + 本地门禁”的工具，不是聊天窗口里的即时翻译器：Subagent 直接写入
工作区文件，Python 只负责确定性处理和验证。完整操作规程见 [AGENTS.md](AGENTS.md)。

文档按读者分工：

- `README.md`：给使用者看的能力介绍、安装和快速开始；
- `AGENTS.md`：给书稿流程 Agent 看的可执行操作手册；
- `docs/maintenance.md`：给编码 Agent 看的维护规则，明确与书稿流程的边界
  （根目录 `opencode.md` / `codex.md` 是短入口）；
- `docs/`：给维护者看的架构、交接合同、产物格式和恢复细节。

## 能做什么

- 扫描 PDF OCR：使用主 OCR 生成带页面坐标的 Markdown 和 sidecar。
- PDF 结构整理：识别目录和章节边界，保守合并跨页续句，保留真实段落、图片、表格和公式；
  对有充分页码与正文标题证据的 OCR 目录页码残留做可审计清洗。
- 脚注处理：区分脚注、正文引用、参考文献和索引；把确认的脚注归并到实际章节单元末尾，
  处理“正文 → 脚注续文 → 新脚注”的跨页顺序。
- 彩页和整页插图：识别疑似整页插页，恢复被插图打断的句子；普通插图不会自动移位，
  插图说明文字也会保留。
- PDF 翻译：术语表、目录翻译、按 TOC 顺序装箱的章节翻译任务、断点续传和逐层校验。
- 目录导航锚点优先绑定精确标题；只容忍编号标点格式差异或唯一的缺编号标题匹配。
- EPUB 高保真翻译：尽量保持原有 XHTML、图片、目录、元数据和导航结构。
- EPUB 构建保持编号论述段落与实际列表的区分；脚注支持明确缩进的续段和多块 Markdown 内容。
- 轻小说 EPUB 和 arXiv/TeX：提供独立的文本交接、校验和构建流程。
- 安全恢复：源文件不覆盖，任务通过 manifest、哈希和 validation report 恢复；失败时只重试
  未完成或未通过的部分。

## OCR 工作模式

同一个配置开关控制 OCR、脚注和整页插图的证据来源：

| 配置 | 行为 |
|---|---|
| `ocr.secondary.enabled: false` | 单 OCR。直接使用主 OCR，忽略旧的双 OCR 共识文件。 |
| `ocr.secondary.enabled: true` | 使用配置的第二套 OCR 做差异筛查，差异交给 Subagent 复核。 |

所有 PDF 新运行都使用视觉 OCR，不存在页面来源模式开关。`pdf_text` 配置已删除；配置中
残留该字段时，配置校验和 `doctor` 会明确报错，不会静默忽略。`pdf_text_probe.json` 现在
只做分类审计：`classification`/`detected_recommendation` 记录 PDF 的文字层特征，
`recommendation` 始终是 `ocr_required`。旧版本生成的 `source_kind: native_text`
sidecar 和 `use_text_layer` probe 仅作为旧产物兼容读取，用于恢复旧输出，不再由新运行
产生。

单 OCR 仍是默认模式。页面扫描会识别疑似 OCR 模型解说（例如声称文字太淡、无法转录），并将
异常页及其源哈希写入 `ocr_anomaly_report.json`。只有异常页交给 `ocr-correct` 做页图复核；其他
页作为未复核直通副本。该启发式只发复核信号，无法证明页面有错，最终以页图为准；不确定结果会阻断后续流程。

当前默认主 OCR 是 Chandra。脚注候选综合 OCR layout、页面位置和文字线索；版面只用于召回与
组织复核范围，最终归属仍按流程合同审阅。详见下方“脚注与尾注”及
[`docs/antigravity-workflow.md`](docs/antigravity-workflow.md#pdf-footnote-and-endnote-contract)。
Chandra 的 Cloudflare Access 凭据放在 `.secrets/chandra-access.json`。YAML 中只配置本地凭据
目录，不写入密钥本身。

示例配置：

```yaml
ocr:
  backend: chandra
  secondary:
    enabled: false
    performance:
      max_estimated_seconds: 3600
```

页面 OCR 的主后端默认是 Chandra；兼容性页面后端还包括 VLLM、Azure Document Intelligence
和 Google Cloud Vision，按需安装对应可选依赖。

安装可选 OCR 依赖：

```text
uv sync --extra ocr-chandra
uv sync --extra ocr-vllm
uv sync --extra ocr-azure
uv sync --extra ocr-vision
```

启用双 OCR 后，`ocr-pages` 会在主 OCR 开始前写入 `ocr_secondary_preflight.json`，按页数和配置的
保守页速估算次 OCR 时长。超过 `max_estimated_seconds` 时命令会暂停；确认确实接受耗时后，使用
`ocr-pages --allow-slow-secondary` 继续。若要退回单 OCR，应明确将
`ocr.secondary.enabled` 改为 `false` 后重新运行；旧的双 OCR 共识不会被复用，也不会自动降级。

打开双 OCR 后，脚注和整页插图阶段也要求当前的共识检查点；切换开关后必须重新生成相关
检查点，旧的脚注决定和插图绑定不会被静默复用。两套 OCR 一致只代表通过筛查，不代表一定
正确；整页插图、脚注归属和引用/参考文献区分仍由 Subagent 复核。

Chandra 是当前主 OCR，扫描、可搜索和混合 PDF 都使用视觉 OCR。视觉 OCR 不会把普通上标、
序数或行内数字引用自动判成脚注，
清晰且证据充分的脚注区域可由本地规则直接处理，边界不清、跨页续文或 OCR 证据冲突的区域
才交由 Subagent 复核。

旧的原生文本路径已经删除，不再有配置入口。旧运行留下的 `source_kind: native_text`
页面 sidecar 仍可被 refine、脚注和插图阶段只读读取，以便恢复旧输出；新运行不会生成这类
页面，也不会因为目录中存在旧 sidecar 而改变 OCR 路径。
若恢复时旧 `ocr_progress.json` 的 `native_text` 与同目录 probe 信息冲突，应核对 PDF 哈希、
运行标识/时间及逐页 sidecar 来源；不能确认它们属于同一运行时先人工核对恢复元数据，不要
仅因冲突就重新 OCR。

脚注标记识别和审计会记录印刷 label，同时使用稳定的 ASCII 内部 key：全角数字、阿拉伯数字和
上标数字按十进制值归一为同一数字 key；圈码 `①` 至 `㊿` 映射到对应的数字 key（1 至 50）。
圈码和符号按既有显示合同呈现，数字 key 用于链接、锚点和重复项区分。星号、匕首号和大小写字母
使用独立 key，避免链接锚点依赖非 ASCII 标记或误合并不同印刷符号。

## 当前输出和公式策略

- PDF 页面结果保存在 `pages/`，双 OCR 结果保存在 `ocr_secondary/`，比较记录保存在
  `ocr_consensus.json`。
- 目录、页面合并、脚注和插图决定分别有 manifest、prompt、decision 和 validation report；
  `refine-local` 还会写入 `toc_title_normalization.json`，记录目录标题页码清洗的证据，
  可以中断后从 pending 项恢复。
- PDF 的已审阅 TOC 标题区域会在 `refine-local` 中物化为 Markdown 标题，再进入 polish；
  行内格式和脚注引用保留，原始页面文件保持只读。
- EPUB 公式采用 Unicode 优先、复杂公式使用 MathML 的方案；目前不要求安装 XeLaTeX、
  `dvisvgm` 或额外 SVG 工具链。MathML 已够用时不会引入更重的系统依赖。
- EPUB 正文遵循标准 Markdown 换行语义：普通单换行不会变成额外硬换行，只有空行或显式
  `<br />` 才形成段落/硬换行。表格会在 EPUB 中使用滚动容器，宽表才启用不换行策略；代码块和公式会在 Markdown 预处理
  时受到保护，避免被误识别成强调或 HTML。

## 按输入选择流程

| 输入 | 适合的流程 | 是否翻译 | 主要入口 |
| --- | --- | --- | --- |
| 扫描或可搜索 PDF | PDF 精修 | 可选 | `ocr-pages` → `refine-*` → `polish` → `build-epub` |
| PDF，只想转成 EPUB | `pipeline: epub_conversion` | 否 | `ocr-pages` → `refine-*` → `polish` → `build-epub` |
| EPUB / MOBI / AZW3 | 高保真 HTML | 是 | `html-prepare` → `html-validate` → `build-html-epub` |
| 轻小说 EPUB | 小说文本流程 | 是 | `translate-novel` → `translate-novel-validate` → `build-novel-epub` |
| arXiv / 本地 TeX | TeX 单元流程 | 是 | `translate-arxiv` → `translate-arxiv-validate` |

TeX 运行目录会核验源快照的输入身份、输入内容指纹和文件树摘要后再恢复；输入切换或旧快照无法验证时会明确失败并保留原目录。

PDF 纯转换模式仍必须经过 OCR、结构整理和 polish；它只跳过实体、目录翻译和正文翻译，
不会把“未整理的 OCR 文本”直接打包。

## 快速开始

### 1. 准备项目

需要 Windows、Python/`uv`、Antigravity，以及一本你有权处理的图书。把原书放进项目的
`input/`，建议一次只放一本。不要把原书放进 `output/`，不要把密钥写入配置或提交到 Git。

安装基础依赖：

```text
uv sync
```

首次打开项目后，让 Antigravity 先执行：

```text
请先阅读 AGENTS.md 和 README.md，检查运行条件，保留现有可用 OCR 配置，帮我完成配置。
不要删除或覆盖原始图书，也不要在聊天中直接翻译正文。
```

### 2. 处理 PDF

把书名、输入文件、OCR 后端和语言写入 `config.yaml`。翻译 PDF 的主流程是：

```text
OCR → OCR 纠错（双 OCR 或检测到单 OCR 异常时）→ 目录/章节识别
→ 整页插图复核 → 页面合并
→ 脚注复核与章末归并 → Markdown 润色
→ 术语/目录/正文翻译 → 校验 → EPUB
```

直接告诉 Antigravity：

```text
请按照 AGENTS.md 翻译 input/我的书.pdf。
完成 OCR、目录和章节整理、整页插图与脚注处理、润色、术语统一、翻译、校验和 EPUB 打包。
所有结果写入项目文件；只有通过本地校验后才算完成。
```

PDF TOC 节点使用显式唯一 `node_id`；结构位置和标题文字都不能替代节点身份。构建时根据章节进度
与源 Markdown 确认标题属于哪个物理文件。HTML 目录与 NCX 共用同一份渲染后导航结果，并在打包前
检查链接文件和标题锚点；缺失可见标题会阻止构建，纯容器节点则链接到其内容文件或首个内容后代。

### 3. 处理 EPUB

在 `config_epub.yaml` 中填写输入文件、源语言和目标语言，然后告诉 Antigravity：

```text
请按照 AGENTS.md 翻译 input/我的书.epub，尽量保留原书的 HTML、图片、目录和元数据，
完成校验后生成 EPUB。
```

EPUB 流程不使用 PDF OCR，也不经过 PDF 的 polish 阶段。
重复运行 `html-prepare` 会复用未变化的压缩缓存；密集的索引/参考文献会按行数预算拆成
可恢复的任务片段，并在打包前恢复为原始 HTML 单元。
EPUB 内部链接会按 URI 规则解码并在压缩包内解析；缺失的 spine 正文会使准备失败，远程非正文资源只保留引用、不由本地流程下载。
每个翻译单元按 EPUB 内解码后的完整成员路径确定身份，因此不同目录中的同名页面和同 stem
的不同扩展名会分别保留并准确替换。旧的扁平译文只有在文件名唯一对应一个 EPUB 页面时才兼容；
如果同名页面有多个候选，构建会在替换任何页面前明确报错。

## 支持的工作流

| 输入 | 主要命令 | 产物 |
|---|---|---|
| 扫描或可搜索 PDF | `ocr-pages`、`refine-*`、`polish`、`translate`、`build-epub` | 翻译 EPUB 或原语言 EPUB |
| EPUB/MOBI/AZW3 | `html-prepare`、`html-validate`、`build-html-epub` | 保留结构的 EPUB |
| 轻小说 EPUB | `translate-novel`、`translate-novel-validate`、`build-novel-epub` | 轻小说 EPUB |
| arXiv/TeX | `translate-arxiv`、`translate-arxiv-validate` | 通过编译校验的 TeX 产物 |

PDF 只转换、不翻译时，在配置中写：

```yaml
title: "Your Book Title"
input_pdf: "input/your_book.pdf"
pipeline: epub_conversion
```

## 常用命令顺序

开始或恢复任务前，可以先查看只读状态：

```text
uv run pdf2epub -c config.yaml status
uv run pdf2epub -c config.yaml status --json
uv run pdf2epub -c config.yaml doctor
```

`status` 展示每个阶段的 `passed`、`pending`、`blocked` 或 `skipped`，不会修改任何产物；
`doctor` 检查配置、输入文件、选中的 OCR 依赖和当前工作区状态。

PDF 翻译或精修的完整顺序是：

```text
ocr-pages --resume
→ [双 OCR] ocr-correct → ocr-correct-validate
→ refine-prepare → Subagent 写 toc_tree.json
→ illustration-prepare → [有候选时 Subagent 写 illustration_decisions.json]
→ illustration-validate → illustration-apply
→ refine-local --resume
→ footnote-prepare → [有待复核时 Subagent 写 footnote_decisions.json]
→ footnote-validate → footnote-apply
→ polish → Subagent 写 polished_markdown/ → polish-validate
→ extract-entities → extract-entities-validate
→ translate-toc → translate-toc-validate
→ check-ready --stage translate → translate → translate-validate
→ bash scripts/build_all_epubs.sh config.yaml
```

`refine-prepare` 会输出页面/标题候选清单与结构审阅 manifest。工作区 Subagent 必须逐个处置候选、
逐页记录视觉或文本证据覆盖，再提交 TOC 树和决定文件；漏审或证据过期会阻止 `refine-local`。
当前 TOC 树使用 schema 2，每个节点都有唯一 `node_id` 和明确绑定状态。更新 TOC 后需重新准备
并审阅整个结构。

PDF Markdown translation can opt into controlled handling for strictly structured bibliography and index units:

```yaml
translation:
  special_units:
    bibliography: controlled
    index: controlled
```

After the normal `uv run pdf2epub -c config.yaml translate` preparation, eligible files are listed in `special_pending_files` and excluded from ordinary workers. For each one, run `uv run pdf2epub -c config.yaml special-unit-prepare --kind bibliography --file <unit.md>` (or `--kind index`), have the workspace Subagent complete its JSON result, then run `special-unit-apply` with the same arguments. Run `translate-validate --file <unit.md>` and the full `translate-validate` afterward. Mixed or ambiguous units stay on the ordinary translation path.

翻译流程必须同时构建两个产物：正式书名的原稿英文 EPUB 和使用译名的译文 EPUB。推荐一次运行：

```text
bash scripts/build_all_epubs.sh config.yaml
```

该脚本等价于依次执行 `build-epub` 和 `build-epub --only-translated`。前者生成使用正式
书名的原稿英文 EPUB；后者只生成使用译名的译文 EPUB，不生成英文 `_en.epub`。需要英文
`_en.epub` 时可改用 `build-epub --translated`，这是可选的三产物模式：正式书名的原稿
英文 EPUB、英文 `_en.epub` 和使用译名的译文 EPUB；英文 `_en.epub` 失败时会记录
`[English Build Failed]` 并继续尝试译文版。任一请求的构建失败时最终返回非 0，已成功
的产物保留。`build-epub --only-translated`（别名 `--target-only`）不触发英文
`_en.epub`；与 `--translated` 同时出现时以 `--only-translated` 为准，但仍要求当前有效
的 polish 和 translate checkpoint。只运行第一个 `build-epub` 会缺少译文版。
`pipeline: epub_conversion` 不得使用 `--translated` 或 `--only-translated`，也不要使用本脚本。

`translate-toc` 的工作区 Subagent 只写扁平的 `toc_translation_result.json`；本地
`translate-toc-validate` 会从源 `toc_tree.json` 合并生成 `toc_tree_translated.json`。 TOC 树为 schema 2，要求唯一的显式 `node_id`；翻译合同为 contract 3。旧格式必须重新准备并审阅，不能直接恢复旧树或旧译文 TOC。

若当前 TOC 明确标记第 1 页为封面，且该页是几乎无文字的全页图，
`illustration-prepare` 可生成 `auto_resolved` 的封面决定；仍必须运行后续的
`illustration-validate` 和 `illustration-apply`。

方括号中的阶段只表示是否需要工作区 Subagent。只把 manifest 中实际列出的 pending 任务交给
工作区 Subagent；`illustration-validate`、`illustration-apply`、`footnote-validate` 和
`footnote-apply` 是确定性检查点，即使没有待复核项也必须运行。不要因空任务跳过校验或 apply；
不要跳过中间门禁。polish 的普通章节按 `subagent.batching.polish_max_files`（默认 4）打包；
index、bibliography、notes 和版权/前置页等特殊单元始终使用独立 worker，不与普通章节混排。

### 脚注与尾注

| 模式 | 行为 |
|---|---|
| `auto`（默认） | 由 TOC 结构审阅选择 `footnotes` 或 `endnotes_only`。 |
| `footnotes` | 处理页面脚注，也支持集中 `type: notes` 单元的尾注归一化。 |
| `endnotes_only` | 跳过页面脚注检测，仍处理集中 Notes 尾注。 |
| `disabled` | 关闭页面脚注和尾注处理；只在明确不需要两者时使用，不用于绕过校验。 |

版面位置只用于候选召回和组织复核；边栏、表格邻接、跨栏歧义仍需审阅。数字/圈号、星号、匕首号
和显式上标字母可按各自标记规则识别；裸 `a)`、星号、字母或公式符号不能仅凭字形认作注释，
公式上下文识别也有限。
段落、列表、引文、表格等脚注块会保留结构；普通注释续句跨页时保留软换行，不因此另起段落。旧的
当前 prepare/decision validation 使用 schema 2，normalization 使用 schema 5；尾注边界歧义复核另用 schema 1。`footnote_normalization.json` schema 1–4 及 prepare/decision schema 1 checkpoint 需按[升级与恢复步骤](docs/antigravity-workflow.md#pdf-footnote-and-endnote-contract)重新执行 `footnote-prepare`，只复核新 manifest 中的 pending 项，再运行 validate/apply。无需重新 OCR；恢复会刷新准备合同，并重建 `footnote_normalized/` 与成功 checkpoint。本次维护不会自动修改书稿产物；若需保留旧派生结果，请在运行恢复前备份。

## 结果在哪里

最终文件通常位于：

```text
output/<配置书名>/<安全化书名>.epub          # 原稿，使用正式书名
output/<配置书名>/<安全化译名>.epub          # 译文，使用译名
output/<配置书名>/<安全化书名>_en.epub       # 英文版（可选，仅 --translated 生成）
```

翻译流程运行 `bash scripts/build_all_epubs.sh config.yaml` 后会得到前两个版本：第一条
`build-epub` 生成正式书名的原稿英文 EPUB；第二条 `build-epub --only-translated` 只生成
译文 EPUB，不生成英文 `_en.epub`。两个命令都需要执行，否则会缺少其中一个产物。若还需
英文 `_en.epub`，使用三产物模式 `build-epub --translated`；`--only-translated`（别名
`--target-only`）只构建译文版。

PDF 的 `build-epub` 默认使用书名生成 EPUB 文件名：Windows 禁止的半角字符、控制字符
和路径分隔符会替换为 `_`，首尾空格/句点会去除，保留设备名会加前缀，文件名最长为
200 个字符。普通 Unicode 字符（例如中文和全角标点）会保留，因此输出文件名可能与
配置书名略有不同。翻译模式下，文件名使用已验证译后 TOC 中的书名；输出目录仍由配置
书名决定。显式配置 `output_epub` 时则使用该路径，不再自动改名。

中间目录会保存 OCR 页面、Subagent prompt、manifest、校验报告和断点信息。不要为了“重新开始”
删除整个 `output/`；先让 Antigravity 检查进度并使用 `--resume`，并确认当前没有相同文件分配的
活动 Subagent。manifest 中的 assignment 哈希只用于恢复和去重提示，不代表 IDE 进程锁。只有
“目标文件存在”不代表任务完成，必须以对应的 validation report 为准。

## 使用时注意

- 需要翻译、润色、目录判断、术语提取，以及脚注/整页插图的疑难结构判断时，按照
  `AGENTS.md` 使用工作区 Subagent；本地程序只做确定性处理和校验。
- 不要在配置中保留未注册或退役的 OCR backend；`doctor` 会将这类配置标为阻断。
- 不要把外部术语表仅凭文件名自动加载；使用前先生成候选报告并明确选择。
- 发现 `human_review_required`、拒答、免责声明或校验失败时，不能继续打包。

更详细的执行规则见 [AGENTS.md](AGENTS.md)；实现和产物契约见
[`docs/architecture.md`](docs/architecture.md)，流程示例和维护说明见
[`docs/antigravity-workflow.md`](docs/antigravity-workflow.md)。

维护 EPUB HTML 交接时另读 [`docs/html-translation-improvements.md`](docs/html-translation-improvements.md)；
它记录压缩单元、元数据、导航和视觉回归测试的维护合同。

本项目基于 MIT License 发布。请只处理你有权使用和翻译的内容。
