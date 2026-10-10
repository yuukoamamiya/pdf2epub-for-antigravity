# pdf2epub

一个面向 Antigravity 的图书整理与翻译工作流。它把扫描 PDF、原生文字 PDF、EPUB、MOBI
和 AZW3 处理成结构可靠、可校验、可继续执行的 EPUB；需要翻译或判断版面结构的部分由
Antigravity 工作区 Subagent 完成，本地程序负责文件整理、校验和打包。

这是一个“文件交接 + 本地门禁”的工具，不是聊天窗口里的即时翻译器：Subagent 直接写入
工作区文件，Python 只负责确定性处理和验证。完整操作规程见 [AGENTS.md](AGENTS.md)。

文档按读者分工：

- `README.md`：给使用者看的能力介绍、安装和快速开始；
- `AGENTS.md`：给 Agent 看的可执行操作手册；
- `docs/`：给维护者看的架构、交接合同、产物格式和恢复细节。

## 能做什么

- 扫描 PDF OCR：使用主 OCR 生成带页面坐标的 Markdown 和 sidecar。
- PDF 结构整理：识别目录和章节边界，合并跨页正文，保留图片、表格和公式。
- 脚注处理：区分脚注、正文引用、参考文献和索引；把确认的脚注归并到实际章节单元末尾，
  处理“正文 → 脚注续文 → 新脚注”的跨页顺序。
- 彩页和整页插图：识别疑似整页插页，恢复被插图打断的句子；普通插图不会自动移位，
  插图说明文字也会保留。
- PDF 翻译：术语表、目录翻译、按 TOC 顺序装箱的章节翻译任务、断点续传和逐层校验。
- EPUB 高保真翻译：尽量保持原有 XHTML、图片、目录、元数据和导航结构。
- 轻小说 EPUB 和 arXiv/TeX：提供独立的文本交接、校验和构建流程。
- 安全恢复：源文件不覆盖，任务通过 manifest、哈希和 validation report 恢复；失败时只重试
  未完成或未通过的部分。

## OCR 工作模式

同一个配置开关控制 OCR、脚注和整页插图的证据来源：

| 配置 | 行为 |
|---|---|
| `ocr.secondary.enabled: false` | 单 OCR。直接使用主 OCR，忽略旧的双 OCR 共识文件。 |
| `ocr.secondary.enabled: true` | 使用配置的第二套 OCR 做差异筛查，差异交给 Subagent 复核。 |

当前默认主 OCR 是 Chandra。脚注候选使用 OCR sidecar 的 `bbox[3]`、页底相交比例、数字开头
和跨页上下文；`Footnote` 标签不能单独绕过几何条件，纯数字页码会排除，连续数字开头块会
形成向上回溯的复核区域。最终角色仍由 Subagent 复核；配置中的未注册或退役后端会被
`doctor` 阻断。脚注报告还会给出疑似漏检数量，并在有源 PDF 时为复核页附带页面 PNG。
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

Chandra 是当前主 OCR。原生文字 PDF 会保留版面信息；
扫描或混合 PDF 使用视觉 OCR。两种来源都不会把普通上标、序数或行内数字引用自动判成脚注，
脚注归属仍由 Subagent 复核。

原生文字 PDF 即使是稳定双栏，也不会仅因栏布局而强制 OCR；提取器会利用 PDF 文本 span 的
坐标按栏重排，并在页面 sidecar 中记录 `layout_mode: multi_column`。只有扫描图、隐藏 OCR
文字层或其他未通过原生文字置信度门禁的 PDF 才进入视觉 OCR。

## 当前输出和公式策略

- PDF 页面结果保存在 `pages/`，双 OCR 结果保存在 `ocr_secondary/`，比较记录保存在
  `ocr_consensus.json`。
- 目录、页面合并、脚注和插图决定分别有 manifest、prompt、decision 和 validation report，
  可以中断后从 pending 项恢复。
- EPUB 公式采用 Unicode 优先、复杂公式使用 MathML 的方案；目前不要求安装 XeLaTeX、
  `dvisvgm` 或额外 SVG 工具链。MathML 已够用时不会引入更重的系统依赖。
- 表格会在 EPUB 中使用滚动容器，宽表才启用不换行策略；代码块和公式会在 Markdown 预处理
  时受到保护，避免被误识别成强调或 HTML。

## 按输入选择流程

| 输入 | 适合的流程 | 是否翻译 | 主要入口 |
| --- | --- | --- | --- |
| 扫描 PDF / 原生文字 PDF | PDF 精修 | 可选 | `ocr-pages` → `refine-*` → `polish` → `build-epub` |
| PDF，只想转成 EPUB | `pipeline: epub_conversion` | 否 | `ocr-pages` → `refine-*` → `polish` → `build-epub` |
| EPUB / MOBI / AZW3 | 高保真 HTML | 是 | `html-prepare` → `html-validate` → `build-html-epub` |
| 轻小说 EPUB | 小说文本流程 | 是 | `translate-novel` → `translate-novel-validate` → `build-novel-epub` |
| arXiv / 本地 TeX | TeX 单元流程 | 是 | `translate-arxiv` → `translate-arxiv-validate` |

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
OCR → OCR 纠错（双 OCR 时）→ 目录/章节识别
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

### 3. 处理 EPUB

在 `config_epub.yaml` 中填写输入文件、源语言和目标语言，然后告诉 Antigravity：

```text
请按照 AGENTS.md 翻译 input/我的书.epub，尽量保留原书的 HTML、图片、目录和元数据，
完成校验后生成 EPUB。
```

EPUB 流程不使用 PDF OCR，也不经过 PDF 的 polish 阶段。

## 支持的工作流

| 输入 | 主要命令 | 产物 |
|---|---|---|
| 扫描/原生 PDF | `ocr-pages`、`refine-*`、`polish`、`translate`、`build-epub` | 翻译 EPUB 或原语言 EPUB |
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
→ build-epub --translated
```

方括号中的阶段只表示是否需要工作区 Subagent。`illustration-validate`、
`illustration-apply`、`footnote-validate` 和 `footnote-apply` 是确定性检查点，即使没有候选或
没有待复核项也必须运行；它们会生成空的、可供后续阶段消费的绑定/归一化产物。任何
`*_subagent_prompt.md` 或 manifest 出现后，都要先交给工作区 Subagent，再运行后续校验；
不要跳过中间门禁。

## 结果在哪里

最终文件通常位于：

```text
output/<书名>/<书名>.epub
```

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
