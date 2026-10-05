# EPUB 翻译流程与维护说明

EPUB 翻译已经改为 Antigravity 工作区 Subagent 文件交接，Python 不再提供
进程内翻译、自动修复或 provider fallback。

本文是 EPUB 路径的维护文档：`AGENTS.md` 规定必须怎么执行，`README.md` 面向项目使用者，
[`architecture.md`](architecture.md) 记录模块边界；这里记录 HTML 交接合同、freshness、
导航构建和回归测试。修改 EPUB 流程时，必须同时检查这四处文档是否仍然一致。

本文只覆盖 EPUB/MOBI/AZW3 的“保留原 HTML 结构”路径，不覆盖 PDF OCR、PDF polish、轻小说
文本模式或 TeX 项目。维护时首先确认输入类型，避免把 `build-html-epub` 当成 PDF 的
`build-epub` 使用。HTML 路径不需要 OCR，也不使用 PDF 的双 OCR 共识开关。

```text
html-prepare
  ↓
Subagent 提取当前书实体术语（默认）
  ↓
再次运行 html-prepare，生成带术语上下文的正文/元数据翻译任务
  ↓
Subagent 翻译 compressed_units/* 和 metadata_translation_source.json
  ↓
html-validate
  ↓
build-html-epub
```

## 命令、目录和所有权

| 阶段 | 本地命令负责 | Subagent 负责 | 关键产物 |
| --- | --- | --- | --- |
| 准备 | 解包、压缩、建立映射、生成 prompt/manifest | 不参与 | `compressed_units/`、`mapping.json`、实体 handoff |
| 实体 | 校验实体 JSON | 从当前书提取实体和术语 | `translation_entities.json` |
| 正文/元数据 | 生成带上下文的 handoff | 写入译文单元和元数据 JSON | `translated_compressed/`、`translated_metadata.json` |
| 校验 | 检查 1:1 单元、HTML 骨架、保护字段、语言审计 | 不参与 | `translate-html_validation.json` |
| 构建 | 恢复 XHTML、更新导航、打包 | 不参与 | `*_translated.epub` |

本地命令不应修改 `compressed_units/` 的源单元来“修复”译文；复杂单元应走
`html-skeleton-retry`/`html-skeleton-restore` 的受保护 token 流程。Subagent 也不能修改
原始 EPUB、映射文件或元数据保护字段。

`html-prepare` 负责解析 XHTML、压缩结构并生成映射文件。默认情况下，它还会生成
`entity_subagent_prompt.md`，要求 Subagent 从整本书的压缩单元提取
`translation_entities.json`。实体文件完成后再次运行 `html-prepare`，正文和元数据
任务才会挂载这份书内术语上下文。确实不需要书内术语表时，可以使用
`html-prepare --skip-entities`，但只建议用于预览或特殊任务。

外部领域术语表通过配置中的 `translation.glossaries` 按书选择。它们不会默认全局
生效，可以选择零个、一个或多个 YAML/JSON 文件。原文件只读，程序会把规范化快照
放入当前书的 `output/<title>/translation_glossaries/` 并记录 SHA-256。Subagent
正文 worker 读取按顶层 TOC 分支裁剪并聚合的稀疏上下文；外部领域术语表中的 `fixed`
译法优先级更高。普通分支使用一次章节级条目，大分支拆分时使用跨分片的共享条目和
当前分片的局部条目，不把其他分支的术语表重复注入。
跨语言的学派术语表应配置在 `translation.reference_glossaries`，只作为只读概念参考，
不参与正式术语优先级，也不能覆盖或修改 `translation.glossaries`。

`html-prepare` 生成正文和元数据任务后，Subagent 必须保持
每个单元的行数、HTML 标签和属性不变，并将结果写入
`translated_compressed/`。元数据单独写入 `translated_metadata.json`：书名、
简介、版权说明和目录可以翻译；作者名和出版社由输入文件提供，必须逐字复制。

`html-validate` 只做本地检查。它会拒绝缺失单元、空文件、行数不一致、标签
结构变化以及元数据保护字段变化。它还会锁定 `compressed_units` 的文件集合、
源文件 SHA-256 和准备阶段的 `input.epub` 快照；输入发生变化时必须重新运行
`html-prepare`。中文目标语言的长段落若仍保持原文，也会进入失败报告。只有校验
通过后，`build-html-epub` 才会恢复压缩结构并重新打包；构建前会清空旧的
`final_xhtml/`，避免旧章节混入新构建。若已有 NCX 或 nav 文档的更新发生异常，
构建会默认阻断，并在 `translation_report.json` 中记录具体导航文件和错误；只有
人工检查报告后，才可显式使用 `--allow-navigation-warnings` 放行。EPUB 只存在
其中一种导航格式时，缺少另一种会记录为 `not_found`，不会被误判为失败。
`--allow-partial` 和 `--allow-navigation-warnings` 都仅用于明确审查过的预览或兼容性
场景。测试还会用 PyMuPDF 打开最终 EPUB，检查页数、文本位置、非空渲染和像素稳定性，
覆盖“打包结果能否被阅读器渲染”这一结构校验之外的环节。

## HTML 结构合同

正文翻译单元的非空行必须保持 1:1 对齐。Subagent 可以翻译文本节点，但不得改变：

- 标签数量、顺序、嵌套和容器边界；
- 属性、链接 href、锚点 id、实体和占位符；
- `<i>`、图片、表格、脚注标记和目录导航所依赖的结构；
- 受保护的作者、出版社、原书名和版权字段。

校验器会在单元级报告明确指出缺失、额外、结构损坏或保护字段变化；不要用
`--allow-partial` 把这些错误隐藏后交付正式 EPUB。

## 一致性和成品门禁

实体表 handoff 的 freshness 由两部分共同决定：实体 manifest 中的源文件哈希，以及当前
源目录的完整 `*.md` 文件集合。只要新增、删除或改动源文件，就必须重新执行实体提取，
不能因为旧 manifest 中已有文件仍然匹配就复用实体表。

OCR 配置在进入流程前统一校验。`ocr.secondary.enabled: true` 必须同时提供
`ocr.secondary.backend`；配置不完整时直接失败，不进入一个看似需要纠错、实际无法执行的
中间状态。CLI 中的 `ocr-correct` 只在第二套 OCR 开启时执行，关闭时跳过整个纠错子流程。

构建导航时，NCX 和 nav 的更新异常会进入 `translation_report.json` 的导航报告，并阻断
默认打包；缺少某一种导航格式本身不是错误。人工确认报告后，才允许使用
`--allow-navigation-warnings`。这和 `epubcheck` 的严格程度是两条独立门禁：导航 warning
的显式确认不能代替 EPUB 结构校验。

最终视觉测试使用固定 EPUB 夹具生成成品，再由 PyMuPDF 打开成品检查页数、页面尺寸、目标
文本、文本版心位置、非空像素和与预期渲染的像素签名；同一个成品重复渲染也必须保持一致。
它覆盖本地“最终压缩包能否被阅读器引擎打开”的回归，但不声称替代 Kindle、Apple Books
等具体阅读器和设备的兼容性测试。

## 维护检查清单

### 修改准备或交接合同

- 同时更新 `pdf2epub/commands/html.py`、对应的 handoff/validation 模块和本文件中的数据流。
- 新增或修改 JSON 字段时，更新 manifest、校验器、freshness 哈希和失败报告；不能只让
  Subagent “大致输出正确格式”。
- 任何源文件集合、单元顺序、HTML 标签数量或元数据保护字段的变化，都应增加一个“旧检查点
  被拒绝、新准备可恢复”的测试。

### 修改 HTML 校验或构建

- 保持 `compressed_units/` 与 `translated_compressed/` 的非空单元 1:1 对齐。
- 维持标签、属性、实体、容器和嵌套关系不变；不要用构建阶段脚本替代 Subagent 翻译。
- 导航更新 warning 必须进入 `translation_report.json` 并默认阻断；只有人工检查后才能使用
  `--allow-navigation-warnings`。
- 构建前后的 `final_xhtml/`、输入快照和源哈希必须保持 freshness 关系，不能依赖旧目录残留。

### 测试与交付

```text
uv run pytest -q
git diff --check
```

涉及安全边界、外部输入、路径或依赖时，再运行项目约定的 DeepSec 扫描。测试必须覆盖：

- 首次 `html-prepare`、实体表完成后的第二次 `html-prepare` 和 `--skip-entities`；
- 单文件校验、全量校验、源集合变化和断点恢复；
- 作者/出版社保护、目录 href/anchor 保留、NCX/nav 更新报告；
- 最终 EPUB 可被 PyMuPDF 打开、渲染非空且重复渲染稳定。
