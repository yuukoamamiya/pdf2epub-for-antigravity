# pdf2epub

pdf2epub 将 PDF 转换为便于阅读的 EPUB，并支持图书翻译；也支持 EPUB、MOBI、AZW3 和 TeX 书稿。
Antigravity 工作区 Agent 负责翻译和需要语义判断的内容，本地 Python 工具负责整理、校验和打包。

## 能做什么

| 输入 | 能力 | 主要结果 |
| --- | --- | --- |
| PDF | 视觉 OCR、目录/章节与脚注整理、插图复核；可生成原语言 EPUB，也可翻译 | 原语言 EPUB、译文 EPUB |
| EPUB、MOBI、AZW3 | 翻译正文和元数据，尽量保留原有 HTML、图片和目录 | 翻译后的 EPUB |
| 轻小说 EPUB | 使用独立文本流程翻译与构建 | 轻小说 EPUB |
| arXiv 或本地 TeX | 翻译 TeX 源文件并通过编译校验 | 校验后的 TeX 工程 |

工作流支持恢复中断任务，并可使用术语表帮助统一译名。PDF 新运行都使用视觉 OCR；EPUB 和 TeX
流程不需要 OCR。

## 快速开始

### 准备环境

- Python 3.11 或更新版本、[`uv`](https://docs.astral.sh/uv/) 和 Antigravity 工作区 Subagent。
- 获取本仓库，在项目目录安装依赖：

  ```bash
  uv sync
  ```

- 把要处理的书放到 `input/`。不要把密钥写入配置文件。
- PDF 默认使用 Chandra 进行视觉 OCR。项目包含客户端依赖，但你还需要配置可用的 OCR 服务地址和凭据；
  按需选用其他已支持的 OCR 服务。EPUB 翻译不需要配置 OCR。

### 选择配置

首次使用时，按输入类型从模板创建配置：PDF 将 [config.yaml.example](config.yaml.example) 复制为 `config.yaml`；
EPUB、MOBI、AZW3 将 [config_epub.yaml.example](config_epub.yaml.example) 复制为 `config_epub.yaml`。
已有配置请直接编辑并保留现有设置。
填写书名、输入文件、源语言和目标语言；处理 PDF 时还要配置 OCR 服务。

### 在 Antigravity 中开始

在 Antigravity 中打开本仓库，让 Agent 按照 [AGENTS.md](AGENTS.md) 处理书稿。例如：

```text
请按 AGENTS.md 的流程翻译 input/我的书.pdf，完成原文 EPUB 检查、翻译和本地校验。
```

```text
请按 AGENTS.md 的流程翻译 input/我的书.epub，尽量保留原有 HTML、图片和目录，并完成校验。
```

## 常见流程

- **PDF 翻译：** OCR 和结构整理 → 润色并验收 → 构建、检查原语言 EPUB → 翻译 → 验收并构建译文 EPUB。
  如果只想把 PDF 转成 EPUB，在配置中设置 `pipeline: epub_conversion`；此模式不做实体提取、目录或正文翻译。
- **EPUB 翻译：** 准备正文和元数据任务 → 翻译 → 校验 → 构建 EPUB。
- **轻小说和 TeX：** 按各自的独立流程准备、翻译和校验。

PDF 原语言版和译文版都以通过润色验收的 Markdown 为源。原稿版会先生成，便于检查目录、段落、脚注和插图；
发现源稿问题时，按操作手册修复并重新验收，再继续翻译。

PDF 和 EPUB 的结果通常位于 `output/<书名>/`：PDF 翻译生成原语言 EPUB 和译文 EPUB，PDF 只转换时生成原语言 EPUB，
EPUB 翻译生成译文 EPUB。

## 文档

- [AGENTS.md](AGENTS.md)：完整操作步骤与限制。
- [Antigravity 工作流说明](docs/antigravity-workflow.md)：各流程细节、配置和恢复方法。
- [架构说明](docs/architecture.md)：模块边界和产物说明。
- [维护指南](docs/maintenance.md)：源码、测试与文档维护规则；维护入口见 [codex.md](codex.md)。
