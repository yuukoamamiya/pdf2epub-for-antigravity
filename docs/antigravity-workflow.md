# Antigravity Subagent 工作流

本项目的推荐翻译路径不从 Python 调用 Google 或 Antigravity API。需要判断和翻译的工作交给 Antigravity IDE 中的 Subagent，Python 只做本地文件处理和确定性校验。

## Subagent 模型配置

在 `config.yaml` 或 `config_epub.yaml` 中设置：

```yaml
subagent:
  models:
    translation: gemini-2.5-pro
    default: gemini-2.5-flash
  # 可选：覆盖某个具体任务
  # task_models:
  #   refine: gemini-2.5-flash
```

默认规则是：正文、元数据、目录、小说和 TeX 翻译使用 `translation`；结构分析、OCR 润色和实体提取使用 `default`。每个生成的 `*_subagent_manifest.json` 和提示词都会明确写出推荐模型，供 Antigravity 中的 Subagent 选择。这里是任务合同，不是 Python 对模型 API 的调用或强制切换。

## 额度耗尽与断点续传

正文任务按文件拆分。使用 `--resume` 重新准备任务时，manifest 会根据目标目录写出 `completed_files` 和 `pending_files`；提示词要求 Subagent 只处理 `pending_files`。已经通过校验的输出不会被重新覆盖。恢复前建议先运行对应的 `*-validate`，这样可以先发现空文件、行数不一致或标签损坏。

元数据是单个 `translated_metadata.json`，必须整体是合法 JSON；如果额度中断留下半个文件，校验会拒绝它，下一次 Subagent 会完整重写。

TeX 使用独立的 `tex_units/` 和 `translated_tex_units/` 文件。校验时本地程序检查每个单元都存在，再从这些单元重建 `project/` 并编译，因此不会把初始原文工程误判为“已经翻完”。

## EPUB 高保真翻译

```text
html-prepare → Subagent(book_translator) → html-validate → build-html-epub
```

执行 `html-prepare` 后，输出目录会包含：

- `compressed_units/*.md`：正文翻译单元，每行对应一个结构单元；
- `translate-html_subagent_manifest.json`：正文翻译清单，包含已完成和待处理文件；
- `metadata_translation_source.json`：书名、目录、简介、版权说明等元数据输入；
- `metadata_translation_prompt.md`：元数据翻译说明。

Subagent 需要：

1. 将每个正文单元写入 `translated_compressed/<同名>.md`；
2. 保持正文行数、HTML 标签、属性和容器不变；
3. 阅读 `metadata_translation_prompt.md`，在输出目录写入 `translated_metadata.json`。

元数据规则：书名、目录、简介和版权说明可以翻译；作者名和出版社必须原样复制。`html-validate` 会检查元数据结构、目录顺序、链接锚点以及作者/出版社是否被修改。校验不通过时，`build-html-epub` 默认拒绝打包。

## PDF 结构精修

```text
ocr-pages → refine-prepare → Subagent → refine-local → polish/translate → build-epub
```

`refine-prepare` 会在 `output/<title>/` 生成 `refine_subagent_prompt.md` 和 `refine_subagent_manifest.json`。Subagent 阅读 `pages/page_*.md` 后，只负责写入 `toc_tree.json`。随后 `refine-local`：

- 校验页码范围、层级、父子包含关系和兄弟节点重叠；
- 用本地 tokenizer 估算单元大小；
- 用 `PageMerger` 合并页面并生成 `ocr_markdown/`；
- 不创建 LLM client、不发送 PDF、不消耗 API 配额。

`refine` 是 `refine-prepare` 的别名，不再存在 provider/API 实现。

## 安全边界

仓库不硬编码内部项目 ID，不伪造 IDE 请求头，也不自动导出或冒用 ADC 凭证。任何需要账号授权的模型调用都应由用户在 Antigravity IDE 会话中完成。
