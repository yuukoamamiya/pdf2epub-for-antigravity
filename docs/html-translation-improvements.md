# EPUB 翻译流程说明

EPUB 翻译已经改为 Antigravity 工作区 Subagent 文件交接，Python 不再提供
进程内翻译、自动修复或 provider fallback。

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

`html-prepare` 负责解析 XHTML、压缩结构并生成映射文件。默认情况下，它还会生成
`entity_subagent_prompt.md`，要求 Subagent 从整本书的压缩单元提取
`translation_entities.json`。实体文件完成后再次运行 `html-prepare`，正文和元数据
任务才会挂载这份书内术语上下文。确实不需要书内术语表时，可以使用
`html-prepare --skip-entities`，但只建议用于预览或特殊任务。

外部领域术语表通过配置中的 `translation.glossaries` 按书选择。它们不会默认全局
生效，可以选择零个、一个或多个 YAML/JSON 文件。原文件只读，程序会把规范化快照
放入当前书的 `output/<title>/translation_glossaries/` 并记录 SHA-256。Subagent
同时读取外部领域术语表和当前书实体表；外部术语表中的 `fixed` 译法优先级更高。
跨语言的学派术语表应配置在 `translation.reference_glossaries`，只作为只读概念参考，
不参与正式术语优先级，也不能覆盖或修改 `translation.glossaries`。

`html-prepare` 生成正文和元数据任务后，Subagent 必须保持
每个单元的行数、HTML 标签和属性不变，并将结果写入
`translated_compressed/`。元数据单独写入 `translated_metadata.json`：书名、
简介、版权说明和目录可以翻译；作者名和出版社由输入文件提供，必须逐字复制。

`html-validate` 只做本地检查。它会拒绝缺失单元、空文件、行数不一致、标签
结构变化以及元数据保护字段变化。只有校验通过后，`build-html-epub` 才会
恢复压缩结构并重新打包；`--allow-partial` 仅用于明确的预览需求。
