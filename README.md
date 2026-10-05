# pdf2epub

一个面向 Antigravity 的图书整理与翻译工作流。它把扫描 PDF、原生文字 PDF、EPUB、MOBI
和 AZW3 处理成结构可靠、可校验、可继续执行的 EPUB；需要翻译或判断版面结构的部分由
Antigravity 工作区 Subagent 完成，本地程序负责文件整理、校验和打包。

## 能做什么

- 扫描 PDF OCR：支持主 OCR，并可选用 PaddleOCR 做第二套 OCR 交叉筛查。
- PDF 结构整理：识别目录和章节边界，合并跨页正文，保留图片、表格和公式。
- 脚注处理：区分脚注、正文引用、参考文献和索引；把确认的脚注归并到实际章节单元末尾，
  处理“正文 → 脚注续文 → 新脚注”的跨页顺序。
- 彩页和整页插图：识别疑似整页插页，恢复被插图打断的句子；普通插图不会自动移位，
  插图说明文字也会保留。
- PDF 翻译：术语表、目录翻译、章节级翻译任务、断点续传和逐层校验。
- EPUB 高保真翻译：尽量保持原有 XHTML、图片、目录、元数据和导航结构。
- 轻小说 EPUB 和 arXiv/TeX：提供独立的文本交接、校验和构建流程。
- 安全恢复：源文件不覆盖，任务通过 manifest、哈希和 validation report 恢复；失败时只重试
  未完成或未通过的部分。

## OCR 有两种工作模式

同一个配置开关控制 OCR、脚注和整页插图的证据来源：

| 配置 | 行为 |
|---|---|
| `ocr.secondary.enabled: false` | 单 OCR。直接使用主 OCR，忽略旧的双 OCR 共识文件。 |
| `ocr.secondary.enabled: true` | 双 OCR。主 OCR 与 PaddleOCR 交叉比较，差异交给 Subagent 复核。 |

双 OCR 模式需要安装本地依赖：

```text
uv sync --extra ocr-local
```

示例配置：

```yaml
ocr:
  backend: chandra
  secondary:
    enabled: true
    backend: paddle
  backends:
    paddle:
      lang: en
      device: cpu
      enable_mkldnn: false
```

打开双 OCR 后，脚注和整页插图阶段也要求当前的共识检查点；切换开关后必须重新生成相关
检查点，旧的脚注决定和插图绑定不会被静默复用。两套 OCR 一致只代表通过筛查，不代表一定
正确；整页插图、脚注归属和引用/参考文献区分仍由 Subagent 复核。

## 快速开始

### 1. 准备项目

需要 Windows、Python/`uv`、Antigravity，以及一本你有权处理的图书。把原书放进项目的
`input/`，建议一次只放一本。不要把原书放进 `output/`，不要把密钥写入配置或提交到 Git。

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

## 结果在哪里

最终文件通常位于：

```text
output/<书名>/<书名>.epub
```

中间目录会保存 OCR 页面、Subagent prompt、manifest、校验报告和断点信息。不要为了“重新开始”
删除整个 `output/`；先让 Antigravity 检查进度并使用 `--resume`。只有“目标文件存在”不代表
任务完成，必须以对应的 validation report 为准。

## 重要边界

- 正文翻译、润色、目录判断、术语提取，以及脚注/整页插图的语义和疑难结构判断必须交给工作区 Subagent；
  本地程序只做明确规则筛选和校验。
- 主 Agent 只负责准备任务、运行本地处理、调度 Subagent、读取校验结果和打包。
- Subagent 不可用时应暂停，不能把正文译文直接贴在聊天里代替写文件。
- 不要把外部术语表仅凭文件名自动加载；使用前先生成候选报告并明确选择。
- 发现 `human_review_required`、拒答、免责声明或校验失败时，不能继续打包。

更详细的执行规则见 [AGENTS.md](AGENTS.md)；实现和产物契约见
[`docs/architecture.md`](docs/architecture.md)，流程示例和维护说明见
[`docs/antigravity-workflow.md`](docs/antigravity-workflow.md)。

本项目基于 MIT License 发布。请只处理你有权使用和翻译的内容。
