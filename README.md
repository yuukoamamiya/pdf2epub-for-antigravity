# pdf2epub-for-antigravity

把 PDF、EPUB 或 LaTeX 源码整理成 EPUB 或翻译后的文档。

> 本项目的翻译、润色和结构判断依赖 Antigravity IDE 中的工作区 Subagent，使用你的 Gemini 订阅额度完成。Python 程序只负责本地文件准备、校验、合并和打包，不会从程序内部调用翻译模型。

## 先看这里：你应该使用哪条流程

| 输入 | 目标 | 使用的命令 |
| --- | --- | --- |
| 扫描版 PDF | OCR 后整理成 EPUB，可选润色和翻译 | ocr-pages → refine-prepare → refine-local |
| 已有 EPUB / AZW3 / MOBI | 保留原版排版并翻译 | html-prepare → Subagent → html-validate → build-html-epub |
| 轻小说 EPUB | 文本模式翻译，保留段落和图片标记 | translate-novel → Subagent → translate-novel-validate → build-novel-epub |
| arXiv 或本地 LaTeX 工程 | 翻译源码并用 XeLaTeX 验收 | translate-arxiv → Subagent → translate-arxiv-validate |

如果要翻译已有 EPUB，优先使用“EPUB 高保真翻译”流程。它不会经过 PDF OCR，能够最大限度保留原书 CSS、字体、图片和排版。

## 工作方式

每个需要 Subagent 处理的阶段都遵循相同的交接方式：

1. Python 命令生成源文件、任务说明和 manifest。
2. 在 Antigravity IDE 中让工作区 Subagent 阅读生成的 prompt。
3. Subagent 直接把结果写入 manifest 指定的目标目录。
4. Python 命令在本地校验结果。
5. 校验全部通过后，Python 命令负责合并和打包。

不要只把译文返回在聊天窗口中。如果没有写回目标文件，程序就无法继续。

## 安装

需要：

- Python 3.11 或更高版本；
- uv；
- Antigravity IDE；
- 一个可用的 OCR 后端。已有 EPUB 的翻译不需要 OCR；扫描版 PDF 需要配置 Chandra、Azure 或 Google Cloud Vision 等后端。

克隆并安装依赖：

~~~bash
git clone https://github.com/yuukoamamiya/pdf2epub-for-antigravity.git
cd pdf2epub-for-antigravity
uv sync
~~~

复制对应的配置模板：

~~~bash
# 扫描版 PDF 使用
cp config.yaml.example config.yaml

# 已有 EPUB 使用
cp config_epub.yaml.example config_epub.yaml
~~~

Windows 用户也可以直接复制文件，不必使用 cp。真实配置、凭证、输入书籍和输出目录都只保存在本地，不要提交到 Git。

## 配置 Subagent 模型

在 config.yaml 或 config_epub.yaml 中设置：

~~~yaml
subagent:
  models:
    translation: gemini-3.1-pro-preview
    default: gemini-3.6-flash
  batching:
    max_files: 5
    max_source_tokens: 12000
    max_concurrency: 3
  # 可选：覆盖某个任务
  # task_models:
  #   refine: gemini-3.6-flash
~~~

默认规则：

- 正文、元数据、目录、轻小说和 TeX 翻译使用 translation；
- 结构分析、OCR 润色和实体提取使用 default。

这里的模型名是写给 Antigravity Subagent 的推荐值，不是 Python API 配置。每次任务生成的 manifest 和 prompt 会记录最终推荐模型。

`batching` 只生成分批建议，不会从 Python 启动模型调用。manifest 会为每个源文件记录字节数、物理行数、非空翻译单元行数和估算 token 数，并给出 `recommended_batches`。超过单批 token 上限的文件会单独列出，需按完整翻译单元拆分后再交给 Subagent。默认最多同时进行 3 个 Subagent 任务。

## 流程一：扫描版 PDF 转 EPUB

### 1. 配置和 OCR

编辑 config.yaml：

~~~yaml
title: "我的书"
input_pdf: "input/mybook.pdf"

translation:
  source_language: English
  target_language: Chinese
  # auto 优先使用精修稿；也可指定 ocr 或 polished
  source_stage: auto

ocr:
  backend: chandra
  furigana_mode: attach
  backends:
    chandra:
      base_url: https://chandra.shenshei.fans/v1
      model: chandra
      max_workers: 4
~~~

运行页级 OCR：

~~~bash
uv run pdf2epub -c config.yaml ocr-pages -i input/mybook.pdf --resume
~~~

结果会写入 output/<title>/pages/。--resume 会保留已完成页面，适合 OCR 中断后继续。

### 2. 让 Subagent 分析目录结构

~~~bash
uv run pdf2epub -c config.yaml refine-prepare
~~~

该命令只生成任务文件，不调用模型。然后在 Antigravity IDE 中让 Subagent：

- 阅读 output/<title>/refine_subagent_prompt.md 和 pages/；
- 按 prompt 中的格式分析章节层级和页码；
- 同时从书名页、版权页或前言页提取作者和出版社，原样写入 toc_tree.json；
- 将结果写入 output/<title>/toc_tree.json。

确认 toc_tree.json 已写入后，运行：

~~~bash
uv run pdf2epub -c config.yaml refine-local --resume
~~~

`refine-prepare` 还会生成 `pagination_map.json`。它从 OCR 页脚中提取可能的
罗马数字和阿拉伯数字，并给出物理页与书内页的 offset 提示。这个文件只供
Subagent 对照目录判断范围，OCR 文件名代表的物理页始终是最终权威，不会被
程序自动按 offset 改写。

这个阶段在本地校验目录范围、父子关系、章节重叠和缺失页面，然后生成 ocr_markdown/ 工作单元。它不会调用模型。

### 3. 可选：润色 OCR 结果

~~~bash
uv run pdf2epub -c config.yaml polish --content-type auto
~~~

让 Subagent 阅读生成的 output/<title>/polish_subagent_prompt.md，读取 ocr_markdown/，并把同名结果写入 polished_markdown/。完成后必须校验：

~~~bash
uv run pdf2epub -c config.yaml polish-validate
~~~

--content-type 可以使用 academic、japanese、general 或 auto。

### 4. 可选：翻译

先确保润色校验通过，然后运行：

~~~bash
uv run pdf2epub -c config.yaml translate --target-language Chinese
~~~

让 Subagent：

- 阅读 translate_subagent_prompt.md；
- 读取 polished_markdown/validated/；
- 将同名译文写入 translated/；
- 按目录翻译 prompt 将译后目录写入 toc_tree_translated.json。

然后运行：

~~~bash
uv run pdf2epub -c config.yaml translate-validate
~~~

如果 TOC 节点包含 `type: bibliography` 或 `type: index`，`translate` 生成的
manifest 会在 `file_roles` 中标记对应单元，并把专用规则写入 prompt：参考文献
保留作者、书名、年份、DOI、URL、ISBN、页码和引用标点；索引保留层级、页码、
范围和交叉引用，同时翻译索引词。两类内容仍会完整交给 Subagent，不会被静默
跳过。

`translate-validate` 还会在 JSON 报告的 `bilingual_warnings` 中记录疑似双语
污染（例如连续长英文原文未发生变化）。这是预警而不是硬失败；人名、公式、URL
和 Bibliography/Index 单元会避免按此启发式误报。

### 5. 打包

只做 OCR、润色而不翻译：

~~~bash
uv run pdf2epub -c config.yaml build-epub
~~~

从翻译结果生成中文 EPUB：

~~~bash
uv run pdf2epub -c config.yaml build-epub --translated
~~~

当使用 `--translated` 时，程序会在生成中文 EPUB 前，自动用同一套英文源稿
（优先使用通过 `polish-validate` 的精修稿）生成英文 EPUB 伴随产物：

- `output/<title>/<安全书名>_en.epub`：英文精修版 EPUB。

随后再生成中文 EPUB。英文 EPUB 和中文 EPUB 使用同一套英文源稿。若配置 `translation.source_stage: ocr`，伴随产物会使用 OCR
稿；默认 `auto` 会在精修稿可用时使用精修稿，否则回退到 OCR 稿。

构建命令只接受通过本地校验的文件。输出在 output/<title>/ 下，文件名会根据书名安全清理。

所有 EPUB/PDF 输入入口统一按以下顺序解析：命令行参数、配置文件中的路径、
`input/` 中唯一匹配的 EPUB/PDF/MOBI/AZW3 文件，最后才尝试输出目录中的标准
缓存文件。发现多个候选时会明确要求使用 `-i`，避免误处理错误书籍。

PDF 的作者和出版社元数据优先来自 `toc_tree.json` 中 Agent 从书名页/版权页提取的
信息；也可以在配置中用 `metadata.author` 或 `metadata.publisher` 显式覆盖。中文 EPUB
会回退读取原始目录中的原作者和出版社，不会翻译作者名。

## 流程二：已有 EPUB 的高保真翻译

这条流程适用于 EPUB、AZW3 和 MOBI。输入为 EPUB 时，会保留原书的 CSS、字体、图片、封面和 XHTML 结构。

### 1. 配置并准备

编辑 config_epub.yaml：

~~~yaml
title: "我的电子书"
input_epub: "input/mybook.epub"

translation:
  source_language: English
  target_language: Chinese

subagent:
  models:
    translation: gemini-3.1-pro-preview
    default: gemini-3.6-flash
  batching:
    max_files: 5
    max_source_tokens: 12000
    max_concurrency: 3

html_translation:
  epubcheck_mode: warn
~~~

运行：

~~~bash
uv run pdf2epub -c config_epub.yaml html-prepare
~~~

准备结果位于 output/<title>/：

- compressed_units/*.md：正文翻译单元，每行对应一个结构单元；
- translate-html_subagent_manifest.json：正文文件清单；
- translate-html_subagent_prompt.md：正文翻译说明；
- metadata_translation_source.json：元数据翻译输入；
- metadata_translation_prompt.md：元数据翻译说明。

### 2. 让 Subagent 翻译并写回文件

在 Antigravity IDE 中让 Subagent 按 translate-html_subagent_prompt.md 和 manifest 执行：

- 只处理 manifest 中的 pending_files；
- 从 compressed_units/ 读取源文件；
- 将同名完整译文写入 translated_compressed/；
- 保持行数、HTML 标签、属性、实体、占位符和容器不变；
- 不要添加 Markdown 代码围栏或解释文字。

同时让 Subagent 按 metadata_translation_prompt.md 写入 translated_metadata.json。

元数据规则：

- 书名、简介、版权说明和目录需要翻译；
- 作者名和出版社名称必须从 preserved_metadata 原样复制；
- 禁止翻译、音译、规范化、改写或省略作者名和出版社。

### 3. 校验和打包

~~~bash
uv run pdf2epub -c config_epub.yaml html-validate
uv run pdf2epub -c config_epub.yaml build-html-epub
~~~

校验失败时，只修复报告指出的单元，然后重新校验。默认禁止用不完整结果打包；--allow-partial 仅用于明确需要的预览。

## 流程三：轻小说 EPUB 翻译

轻小说使用文本模式，适合需要术语一致性、段落边界和图片标记的 EPUB。

~~~bash
uv run pdf2epub -c config.yaml translate-novel -i input/novel.epub
~~~

命令会生成 novel_units/、novel_subagent_manifest.json 和 novel_subagent_prompt.md。在 Antigravity IDE 中让 Subagent：

- 只处理 manifest 的 pending_files；
- 读取 novel_units/；
- 将同名译文写入 translated_novel/；
- 按 metadata_translation_prompt.md 写入 translated_metadata.json；
- 保留图片标记和段落边界，不添加说明或代码围栏。

完成后运行：

~~~bash
uv run pdf2epub -c config.yaml translate-novel-validate
uv run pdf2epub -c config.yaml build-novel-epub
~~~

## 流程四：arXiv / LaTeX 翻译

支持 arXiv ID、URL、本地源码目录、压缩包或 .tex 文件。

~~~bash
uv run pdf2epub translate-arxiv 2503.01800

# 本地工程也可以这样运行
uv run pdf2epub translate-arxiv ./paper-source --main-tex main.tex
~~~

在 Antigravity IDE 中让 Subagent 阅读运行目录下 .pdf2epub/tex_subagent_prompt.md，只处理 manifest 的 pending_units，并把译文写入 translated_tex_units/。

不要直接修改 source/，也不要把 project/ 当作翻译交接目录。完成后运行：

~~~bash
uv run pdf2epub translate-arxiv-validate --output-dir output/arxiv/2503.01800
~~~

校验命令会从完整翻译单元重建 project/，再使用本地 XeLaTeX 编译。需要安装包含 XeLaTeX、latexmk、ctex 和中文字体的 TeX Live。

## 额度耗尽时如何继续

额度耗尽不会破坏已经写入的文件。不要删除输出目录，也不要从头覆盖已完成结果。

通用步骤：

1. 先运行对应的 *-validate，找出缺失、空白或校验失败的文件；
2. 使用原来的准备命令加 --resume；
3. 查看新的 manifest，确认 pending_files 或 pending_units；
4. 让 Subagent 只处理 pending 项；
5. 再次运行校验，全部通过后才打包。

如果 Subagent 没有写入文件，或写入的是“无法翻译”“I can’t assist…”等高置信度拒答/免责声明，校验会将对应单元列入 `safety_blocked`，不会进入 validated，也不会影响其他已完成单元。不要无限重复同一提示词；如果确认是误判，可将内容拆成更小的合法上下文后人工复核。流程不会绕过模型的安全限制。

示例：

~~~bash
# PDF 润色或翻译
uv run pdf2epub -c config.yaml polish --resume
uv run pdf2epub -c config.yaml translate --resume

# EPUB 高保真翻译
uv run pdf2epub -c config_epub.yaml html-prepare --resume

# 轻小说
uv run pdf2epub -c config.yaml translate-novel -i input/novel.epub --resume

# TeX
uv run pdf2epub translate-arxiv ./paper-source --resume
~~~

正文单元的断点依据是 completed_files / pending_files；TeX 单元的断点依据是 completed_units / pending_units。元数据是一个完整 JSON 文件，如果中断后文件不完整，下一次必须整体重写。

## OCR 后端和 API 边界

翻译、润色、目录分析、实体提取和元数据处理不需要程序 API；这些任务由 Antigravity Subagent 完成。

ocr-pages 是唯一可以联网的处理阶段，是否联网取决于 OCR 配置：

- chandra：推荐，可使用本地或远程 Chandra 服务；
- azure：Azure Document Intelligence；
- vision：Google Cloud Vision；
- vllm：使用配置的视觉语言模型进行 OCR。

选择远程 OCR 后端时，凭证只放在本地配置或环境变量中，不要写入模板或提交到 GitHub。

## 输出目录大致结构

~~~text
output/<title>/
├── pages/                         # 页级 OCR
├── ocr_markdown/                  # PDF 合并后的源单元
├── polished_markdown/             # Subagent 润色结果
├── translated/                    # PDF 翻译结果
├── <safe-title>_en.epub           # 翻译时自动生成的英文 EPUB
├── compressed_units/              # EPUB 压缩正文单元
├── translated_compressed/         # EPUB 正文译文
├── novel_units/                   # 轻小说源单元
├── translated_novel/              # 轻小说译文
├── metadata_translation_prompt.md # 元数据翻译说明
├── translated_metadata.json       # 元数据译文
└── *.epub                         # 最终 EPUB
~~~

## 常见问题

### Subagent 翻译了但程序说缺文件

检查文件是否写入 manifest 指定的目录，文件名是否与源文件完全相同，以及文件中是否误加了 Markdown 代码围栏。

### html-validate 报行数或标签错误

只修复报告中的对应单元。每个正文单元必须保持源文件行数，HTML 标签和属性必须原样保留。

### build-* 拒绝打包

先运行对应的校验命令并修复所有错误。不要使用 --allow-partial 代替校验；它只适合临时预览。

### OCR 失败或速度太慢

检查 OCR 后端的服务地址和凭证，降低 max_workers，或使用 --resume 从已完成页面继续。

## 开发检查

修改代码后运行：

~~~bash
python -m compileall -q pdf2epub scripts
uv run pytest -q
uv lock --check
git diff --check
~~~

不要提交 input/、output/、真实 config.yaml、真实 config_epub.yaml、.env、凭证、日志、虚拟环境或缓存文件。

## 致谢与上游项目

本项目是 [ShenSheiBot/pdf2epub](https://github.com/ShenSheiBot/pdf2epub) 的优化增强版本。非常感谢原作者 [ShenSheiBot](https://github.com/ShenSheiBot)（bot）开源如此优秀的 PDF 到 EPUB 结构化转换引擎！

## 贡献

欢迎提交 Issue 和 Pull Request！
也可以去关注一下[甚谁](https://www.zhihu.com/people/sakuraayane_justice)谢谢喵！

## 许可

MIT License
