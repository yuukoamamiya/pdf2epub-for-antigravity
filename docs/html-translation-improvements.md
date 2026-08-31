# EPUB 翻译流程说明

EPUB 翻译已经改为 Antigravity 工作区 Subagent 文件交接，Python 不再提供
进程内翻译、自动修复或 provider fallback。

```text
html-prepare
  ↓
Subagent 翻译 compressed_units/* 和 metadata_translation_source.json
  ↓
html-validate
  ↓
build-html-epub
```

`html-prepare` 负责解析 XHTML、压缩结构并生成映射文件。Subagent 必须保持
每个单元的行数、HTML 标签和属性不变，并将结果写入
`translated_compressed/`。元数据单独写入 `translated_metadata.json`：书名、
简介、版权说明和目录可以翻译；作者名和出版社由输入文件提供，必须逐字复制。

`html-validate` 只做本地检查。它会拒绝缺失单元、空文件、行数不一致、标签
结构变化以及元数据保护字段变化。只有校验通过后，`build-html-epub` 才会
恢复压缩结构并重新打包；`--allow-partial` 仅用于明确的预览需求。
