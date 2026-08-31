# pdf2epub-for-antigravity

> 🌟 **专为 Google Antigravity & Windows 生态优化的高精度学术/轻小说 PDF 转 EPUB 转换器。**  
> 本项目是 [ShenSheiBot/pdf2epub](https://github.com/ShenSheiBot/pdf2epub) 的优化增强 Fork，针对 Antigravity 运行环境、大模型大上下文 Refine 与 Windows 跨平台兼容进行了深度适配。

将外语学术书籍、扫描件 PDF 或纵排日语轻小说转换成结构完备的 EPUB 格式，保留目录层级、振假名注音、双向脚注跳转、高清插图与表格，使其达到或超越出版社原版排版质感。

---

## 🚀 Antigravity Edition 专属增强特性

1. 🔑 **Antigravity IDE / Subagent 协同**
   * 书籍翻译与 PDF 结构判断通过 Antigravity IDE 中的 Subagent 读写工作区，使用 Gemini Pro 订阅额度。
   * 本地程序只负责拆分、文件交接、校验、合并和打包，不伪装 IDE 登录，也不注入内部项目 ID 或请求头。

2. 🛡️ **自适应超大 PDF 压缩与 Refine Payload 防爆（Adaptive PDF Compression）**
   * Refine 目录与边界分析阶段针对超长超大扫描件（数百页/几百兆 PDF）自动进行多级自适应二值化压缩（`120 -> 90 -> 72 -> 50 DPI`）。
   * 彻底根治超长学术书籍在向多模态 LLM 发送 PDF Part 时触发 `413 Payload Too Large` 或上下文超限崩溃的问题。

3. 🪟 **原生 Windows 深度跨平台兼容（Native Windows Compatibility）**
   * 修复了 Windows 默认 GBK 编码引发的 `UnicodeDecodeError`，强制 UTF-8 跨平台一致。
   * 基于 Windows 原生 `msvcrt.locking` 实现了跨进程安全互斥锁，解决无 `fcntl` 时的并发冲突。
   * 统一生成 POSIX 相对路径（`pages/page_001.html`），保证打包出的 EPUB 无论在 Calibre、Apple Books 还是各平台阅读器均能完美解析。
   * 自动支持从 `input/` 子目录解析 PDF 文件，增强文件查找与路径容错。

4. ⚡ **Antigravity 原生 Subagent 协同流水线（Native Subagent Architecture）**
   * **EPUB 高保真翻译**：`pdf2epub html-prepare` ➔ `book_translator` 子 Agent 协同翻译 ➔ `pdf2epub html-validate` 校验 ➔ `pdf2epub build-html-epub` 逆向重构打包。
   * **PDF 学术转换与精修**：`ocr-pages` 页面提取 ➔ `refine-prepare` ➔ Subagent 生成 `toc_tree.json` ➔ `refine-local` 本地合并 ➔ 翻译/润色 ➔ `build-epub`。
   * 翻译阶段不需要额外 API；OCR 是否联网由所选 OCR 后端决定。

### Subagent 模型配置

在 `config.yaml` 或 `config_epub.yaml` 中统一配置 Subagent 使用的模型：

```yaml
subagent:
  models:
    translation: gemini-2.5-pro
    default: gemini-2.5-flash
  # 可选：按任务覆盖，例如 refine、polish、translate-html
  # task_models:
  #   refine: gemini-2.5-flash
```

默认情况下，正文、元数据、目录、轻小说和 TeX 翻译使用 Pro；结构分析、OCR
润色和实体提取使用 Flash。每个 Subagent manifest 和提示词都会写明最终模型。
这些字段只是 Antigravity IDE 子 Agent 的任务约定，Python 不会根据它们调用或
切换任何翻译 API。

---

## 技术优势
经测试，本项目效果远强于各大商业软件的直接转换效果，同时因为其基于OCR的特性，不会因出版社更新DRM机制而失效。

### Demo
转换前：http://biopolitics.kom.uni.st/Michel%20Foucault/The%20Foucault%20Reader%20(149)/The%20Foucault%20Reader%20-%20Michel%20Foucault.pdf

转换后：https://raw.githubusercontent.com/ShenSheiBot/pdf2epub/refs/heads/main/example.epub

## 局限性
因为其复杂性和对 OCR/Subagent 的依赖，转换速度较慢。Subagent 任务需要在
Antigravity IDE 中执行；OCR 是否联网取决于所选后端。

对于纵排日语，需要扫描文件的质量较高且为*白底*。（并非白底会导致插图识别错误）

因为逐页进行转换，要求书的新章节*新起一页*，否则章节的最后部分可能会被顺延到下一章节。如果扫描件是每两页一扫描的pdf，建议拆分成单页pdf再操作。

## 工作流程 (Workflow)

第一次使用时，只需记住这条原则：Python 命令负责准备文件和本地校验，
Antigravity Subagent 负责阅读源文件并把结果写回指定目录。每个阶段完成后，
先运行对应的 `*-validate`，再进入下一阶段。

### PDF 工作流 (Recommended - uses toc_tree.json)

适用于 PDF 扫描件转换：

1. **ocr-pages**：逐页进行 OCR，保存 Markdown、HTML 和可审计的原始版式信息
2. **refine-prepare**：生成 Subagent 任务说明和页清单
3. **Antigravity Subagent**：阅读 `pages/`，在输出目录写入 `toc_tree.json`
4. **refine-local**：本地校验 TOC、估算 token 并合并章节工作单元
5. **polish**：生成润色任务，由 Antigravity Subagent 消除 OCR 错误、页眉页脚等
6. **polish-validate**：本地校验并暂存润色结果
7. **translate**：（可选）生成翻译任务，由 Antigravity Subagent 翻译成目标语言
8. **translate-validate**：本地校验翻译结果和译后目录
9. **build-epub**：基于 toc_tree.json 生成 EPUB

### EPUB 高保真翻译工作流 (Antigravity Subagent 驱动)

适用于已有 EPUB 文件的翻译，完整保留原书的 CSS 样式、字体与排版：

书名、简介、版权信息和目录会提交给 Subagent 翻译；作者名和出版社名称
会原样保留，并由本地校验强制检查。

1. **html-prepare**：本地解析 EPUB 并无损压缩 XHTML，生成单行映射单元
2. **Subagent 协同翻译**：由 Antigravity `book_translator` 子 Agent 批量翻译正文，并单独填写 `translated_metadata.json`
3. **html-validate**：纯本地全量校验正文标签/行数和元数据结构；作者名、出版社强制保持原文
4. **build-html-epub**：逆向重构完整 XHTML 并重新打包生成高质量 EPUB

```bash
# 1. 本地结构拆分与无损压缩（不调用翻译 API）
uv run pdf2epub -c config_epub.yaml html-prepare

# 2. 在 Antigravity 对话框中调度 book_translator 翻译各单元，
#    同时按 output/<title>/metadata_translation_prompt.md 填写元数据
#    如果额度中断，重新生成任务时加 --resume，只处理清单中的 pending_files
# uv run pdf2epub -c config_epub.yaml html-prepare --resume

# 3. 本地离线 100% 质量与标签校验
uv run pdf2epub -c config_epub.yaml html-validate

# 4. 逆向重构并打包最终 EPUB
uv run pdf2epub -c config_epub.yaml build-html-epub
```

优势：
- 完整保留原书的 CSS 样式、字体、封面、目录结构
- 由 Antigravity 官方会话与 Gemini Pro 订阅配额完成翻译
- 100% 本地快速校验防幻觉、防标签丢失与行数错位

### 轻小说翻译工作流 (Novel - 文本模式 + 术语表)

适用于轻小说 EPUB 的日→中翻译，由工作区 Subagent 负责正文和元数据：

1. **translate-novel**：提取章节和元数据，生成 Subagent 文件任务
2. **translate-novel-validate**：本地检查章节和元数据
3. **build-novel-epub**：从已校验的翻译文本重建 EPUB

```bash
# 轻小说翻译示例
uv run pdf2epub translate-novel -i novel.epub -c config.yaml
uv run pdf2epub translate-novel-validate -c config.yaml
uv run pdf2epub build-novel-epub -c config.yaml
```

额度耗尽时不需要从头翻译：已写入目标目录的单元会保留。先运行对应的
`*-validate` 查看缺失/错误单元，再用 `polish --resume`、`translate --resume`、
`html-prepare --resume` 或 `translate-novel --resume` 重新生成任务。manifest
会记录 `completed_files` 和 `pending_files`，Subagent 只处理后者。TeX 使用
`translate-arxiv ... --resume`，对应清单字段为 `completed_units` 和 `pending_units`。

优势：
- Subagent 可在同一工作区维护术语一致性
- 本地结构校验避免不完整译文进入打包阶段
- 作者名、出版社始终由本地校验强制保持原文

### arXiv / LaTeX 翻译工作流

适用于 arXiv 论文源码或本地 TeX 工程。它不经过 PDF OCR，而是准备 TeX
正文翻译任务，并将“完整工程可以用 XeLaTeX 编译”作为本地验收条件。

```bash
# 下载 arXiv 源码并准备 Subagent 翻译任务
uv run pdf2epub translate-arxiv 2503.01800

# 翻译本地工程；入口也可以自动识别
uv run pdf2epub translate-arxiv ./paper-source --main-tex main.tex

# Subagent 完成后，在同一 run 目录运行本地编译校验
uv run pdf2epub translate-arxiv-validate --output-dir output/arxiv/2503.01800
```

TeX 翻译使用 `tex_units/` 与 `translated_tex_units/` 作为独立单元交接目录；
额度中断后运行 `translate-arxiv ... --resume`，只会把未完成单元放入
`pending_units`。校验命令会根据完整单元集合重新构建 `project/`，再执行本地编译。

该工作流会递归跟踪正文中的 `\input` / `\include` / `\subfile`，并在
`output/arxiv/<source-id>/tex_units` 和 `translated_tex_units` 中留下独立交接
单元；校验时再生成可独立编译的 `project` 工程。任务清单和编译日志保存在同一运行目录的 `.pdf2epub` 下；本地阶段只
负责准备工程、校验并调用 XeLaTeX 编译，不会调用翻译模型或 repair agent。

需要本机安装包含 XeLaTeX、`latexmk`、`ctex` 和 Fandol 字体的 TeX Live。

## 关于模型与授权

本项目不保留翻译 API 路径：在 Antigravity IDE 中使用 Subagent 和 Gemini Pro
订阅额度完成结构判断、正文翻译及元数据翻译。本地 Python 只做文件准备、
离线校验、合并和打包；OCR 后端仍按配置运行。

## 日语OCR架构

### OCR后端支持

本项目支持以下 OCR 后端。对于日语纵排、英语、复杂表格和混合版型，当前
推荐使用 Chandra 2；传统后端继续保留用于兼容和对照。

#### 1. **Chandra 2** (`chandra`)
- 通过本地 vLLM 服务运行，单张 24 GB NVIDIA GPU 即可
- 同时保存 Markdown、保留 bbox/label 的 HTML、原始 HTML、ordered blocks 和裁图
- 支持日语纵排、英语、表格、公式、脚注、页眉页脚和混合版型
- 服务部署与重启说明见 [`deploy/chandra`](deploy/chandra/README.md)

#### 2. **Azure Document Intelligence** (`azure`)
- 支持振假名(furigana)检测和重组
- 需要Azure账户和API密钥

#### 3. **Google Cloud Vision** (`vision`)
- 正文偶有漏字，振假名偶有错漏
- 需要Google Cloud账户和服务账户密钥

#### 4. **通用 Vision Language Models** (`vllm`)
- Gemini 识别效果较佳，但经常“自由发挥”，添加不存在的振假名，且审核严格，不推荐
- Anthropic 识别效果较差，虽然审核宽松，更不推荐
- VLLM 整体识别速度缓慢且费用较高，胜在输出文本连贯，但仍不能完全摆脱后处理需求，故整体仅作为备用方案

## 安装
 
### 依赖要求
- Python 3.11+
- UV (包管理器)
- 至少一个OCR后端的API账户（如本地/远程 Chandra 2、Google Cloud Vision 或 Azure）

### 安装步骤

1. 克隆仓库
```bash
git clone https://github.com/yuukoamamiya/pdf2epub-for-antigravity.git
cd pdf2epub-for-antigravity
```

2. 安装 UV（如果未安装）
```bash
# Windows (PowerShell)
irm https://astral.sh/uv/install.ps1 | iex

# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

3. 安装项目依赖
```bash
uv sync
```

4. 配置环境与凭据
```bash
cp config.yaml.example config.yaml
# 编辑 config.yaml 调整书名与配置
```

### 配置示例 (Antigravity 极简配置)

#### A. EPUB 翻译配置 (`config_epub.yaml`)
```yaml
title: "我的电子书"
input_epub: "input/mybook.epub"  # 放入 input/ 目录后 Agent 会自动识别填充

translation:
  source_language: English
  target_language: Chinese

html_translation:
  epubcheck_mode: warn
```

#### B. PDF 转换与精修配置 (`config.yaml`)
```yaml
title: "我的学术书籍"
input_pdf: "input/mybook.pdf"

translation:
  source_language: English
  target_language: Chinese

ocr:
  backend: chandra
  furigana_mode: attach # 日语振假名: attach / remove / ruby
  backends:
    chandra:
      base_url: https://chandra.shenshei.fans/v1
      model: chandra
      max_workers: 4
      dpi: 192
      min_dimension: 1024

refine:
  adaptive_page_limit:
    initial_pages: 150
    min_pages: 30
  pdf_compression:
    payload_limit_mb: 18
    compress_if_exceeds: true
    dpi: 50
    quality: 40
    grayscale: true
```

#### C. 轻小说专用配置（可选）

```yaml
novel:
  glossary_max_tokens: 1000
```

### 2. 推荐工作流程（统一CLI）

所有功能通过统一的CLI入口访问：

#### 步骤 1: 页级 OCR
```bash
uv run pdf2epub ocr-pages -i input.pdf
```
生成 `output/{book_title}/pages/page_*.md`

参数说明：
- `--resume`: 从上次中断处继续
- `--start-page`: 起始页码
- `--end-page`: 结束页码
- `--max-workers`: 并发数

#### 步骤 2: Subagent 精细化拆分
```bash
uv run pdf2epub refine-prepare
# 在 Antigravity 中生成 output/{book_title}/toc_tree.json
uv run pdf2epub refine-local
```
Subagent 分析 TOC；本地命令验证章节边界并生成工作单元（支持无限层级嵌套）。

参数说明：
- `--resume`: 从上次中断处继续
- `--max-tokens`: 每个单元的最大 token 数

#### 步骤 3: 内容润色
```bash
uv run pdf2epub polish
# 在 Antigravity 中按 polish_subagent_prompt.md 处理并写入 polished_markdown/
uv run pdf2epub polish-validate
```

针对不同内容类型：
```bash
# 学术书籍（带脚注和引用）
uv run pdf2epub polish --content-type academic

# 日语书籍（保留振假名）
uv run pdf2epub polish --content-type japanese

# 自动检测内容类型
uv run pdf2epub polish --content-type auto
```

#### 步骤 4: 翻译（可选）
```bash
# 先确保已执行 polish-validate
uv run pdf2epub translate --target-language Chinese
# 在 Antigravity 中按 translate_subagent_prompt.md 写入 translated/
uv run pdf2epub translate-validate
```

#### 步骤 5: 生成 EPUB
```bash
uv run pdf2epub build-epub
```
不翻译时基于润色结果生成；如需从翻译结果生成，使用 `--translated`。
最终 EPUB 文件保存在 `output/{book_title}/` 下，文件名会进行安全清理。

如需从翻译后的内容生成：
```bash
uv run pdf2epub build-epub --translated
```

### 3. 翻译功能

#### 实体提取（可选，用于保持翻译一致性）

对于包含大量专有名词的书籍（如日语轻小说），可以先提取实体：

```bash
# 提取人物、地点、术语等实体
uv run pdf2epub extract-entities -i input.pdf --source-lang Japanese --target-lang Chinese
```

生成 `output/{book_title}/translation_entities.json`，包含：
- **人物名称**：包含性别、描述、关系
- **地点名称**：城市、建筑、幻想世界
- **组织机构**：公会、学校、公司
- **专有术语**：魔法、技能、道具
- **种族物种**：包含单复数形式

#### 翻译处理

```bash
# 基本翻译（自动检测并使用实体文件，如果存在）
uv run pdf2epub translate --target-language Chinese

```

**注意**：如果 `translation_entities.json` 文件存在，Subagent 会将其作为术语参考；
当前 CLI 没有单独关闭实体参考的参数。

### 4. 完整工作流程示例

#### 日语轻小说翻译流程
```bash
# 1. 页级OCR
uv run pdf2epub ocr-pages -i manga.pdf

# 2. 准备 Subagent 结构分析任务
uv run pdf2epub refine-prepare

# 3. 在 Antigravity 中让 Subagent 读取
#    output/<book_title>/refine_subagent_prompt.md，写入 toc_tree.json

# 4. 本地校验并生成工作单元
uv run pdf2epub refine-local

# 5. 准备翻译实体任务（可选，用于一致性）
uv run pdf2epub extract-entities
# Subagent 写入 translation_entities.json 后进行本地校验
uv run pdf2epub extract-entities-validate

# 6. 日语内容润色
uv run pdf2epub polish --content-type japanese

# 7. 准备中文翻译任务（Subagent 可读取已校验实体）
uv run pdf2epub translate --target-language Chinese

# 8. 生成EPUB
uv run pdf2epub build-epub --translated
```

#### 学术书籍翻译流程
```bash
# 1. 页级OCR
uv run pdf2epub ocr-pages -i thesis.pdf

# 2. 准备 Subagent 结构分析任务
uv run pdf2epub refine-prepare
# 在 Antigravity 中生成 output/<book_title>/toc_tree.json
uv run pdf2epub refine-local

# 3. 学术内容润色（保留脚注）
uv run pdf2epub polish --content-type academic

# 4. 翻译
uv run pdf2epub translate --target-language Chinese

# 5. 生成EPUB
uv run pdf2epub build-epub --translated
```

#### 已有 EPUB 翻译（保留原格式）
```bash
# 本地准备压缩单元和元数据翻译协议
uv run pdf2epub -c config_epub.yaml html-prepare
# 在 Antigravity 中让 Subagent 按 manifest 的 pending_files 翻译正文，
# 并按 metadata_translation_prompt.md 写入 translated_metadata.json
uv run pdf2epub -c config_epub.yaml html-validate
uv run pdf2epub -c config_epub.yaml build-html-epub
```

EPUB 翻译统一使用 Subagent 文件交接：
```bash
uv run pdf2epub html-prepare -i book.epub --target-language Chinese
# 在 Antigravity 中按 manifest 的 pending_files 翻译 compressed_units/*，
# 写入 translated_compressed/*，并完成 translated_metadata.json
uv run pdf2epub html-validate
uv run pdf2epub build-html-epub
```

#### 轻小说翻译（术语表 + 退化防护）
```bash
uv run pdf2epub -c config.yaml translate-novel -i novel.epub
# 中断后继续
uv run pdf2epub -c config.yaml translate-novel -i novel.epub --resume
# 单独重建 EPUB（不重翻）
uv run pdf2epub -c config.yaml build-novel-epub
```

#### 英文书籍（无需翻译）
```bash
# 1. 页级OCR
uv run pdf2epub ocr-pages -i book.pdf

# 2. 准备并完成 Subagent 结构分析
uv run pdf2epub refine-prepare
# 在 Antigravity 中生成 output/<book_title>/toc_tree.json
uv run pdf2epub refine-local

# 3. 内容润色
uv run pdf2epub polish

# 4. 生成EPUB
uv run pdf2epub build-epub
```

### 5. 高级配置

PDF 的结构分析和 EPUB/PDF/TeX 的翻译都在 Antigravity Subagent 中完成；本地
命令不需要配置 provider 或 API 凭证。Subagent 模型按上面的 `subagent.models`
配置，未配置时翻译默认 `gemini-2.5-pro`，其他任务默认 `gemini-2.5-flash`。

### 6. 故障排除

#### OCR 失败
- 检查 API 配额和密钥配置
- 降低 `max_workers` 减少并发
- 使用 `--resume` 从失败处继续

#### Subagent 工作流问题
- 确认 Subagent 直接读写当前工作区，而不是把内容复制到外部脚本
- EPUB 翻译检查 `metadata_translation_prompt.md`，并确保生成 `translated_metadata.json`
- PDF 检查 `refine_subagent_prompt.md`，并确保生成 `toc_tree.json`

#### 内存不足
- 减少 `max_workers`
- 降低 `zoom_factor`
- 分批处理章节

### 7. 输出结构
```
output/
└── {book_title}/
    ├── input.pdf              # 处理后的PDF
    ├── input_original.pdf     # 原始PDF副本
    ├── toc_tree.json          # TOC结构（支持无限层级）
    ├── pages/                 # 页级OCR结果
    │   ├── page_001.md
    │   ├── page_002.md
    │   └── ...
    ├── ocr_markdown/          # 聚合后的章节内容
    │   ├── chapter_1.md
    │   ├── chapter_1.1.md     # 支持层级嵌套
    │   └── ...
    ├── polished_markdown/     # 润色后内容
    │   ├── chapter_1.md
    │   └── ...
    ├── images/                # 提取的插图
    │   ├── ch001_p010_illustration.png
    │   └── ...
    ├── translated/            # 翻译后内容（如果执行了翻译）
    │   ├── chapter_1.md
    │   └── ...
    ├── translation_entities.json  # 翻译实体参考（如果提取了）
    ├── translation_reference.txt  # 人类可读的翻译参考
    ├── pages/ocr_progress.json           # 页级OCR进度
    ├── ocr_markdown/tree_progress.json   # refine进度
    ├── refine_subagent_prompt.md          # PDF结构分析Subagent提示词
    ├── toc_tree.json                      # Subagent生成、由本地校验
    ├── metadata_translation_source.json  # EPUB元数据翻译输入
    ├── metadata_translation_prompt.md    # EPUB元数据Subagent提示词
    ├── translated_metadata.json          # EPUB元数据译文
    ├── polished_markdown/processing_tracker.json   # 润色进度
    ├── translated/processing_tracker.json          # 翻译进度
    └── {book_title}.epub      # 最终 EPUB（翻译版可能使用译后书名）
```


## 致谢与上游项目

本项目是 [ShenSheiBot/pdf2epub](https://github.com/ShenSheiBot/pdf2epub) 的优化增强版本。非常感谢原作者 [ShenSheiBot](https://github.com/ShenSheiBot)（bot）开源如此优秀的 PDF 到 EPUB 结构化转换引擎！

## 贡献

欢迎提交 Issue 和 Pull Request！
也可以去关注一下[甚谁](https://www.zhihu.com/people/sakuraayane_justice)谢谢喵！

## 许可

MIT License
