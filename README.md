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
流程不需要 OCR。`html-prepare` 会生成主 Agent 专用的 EPUB worker 派发清单，列出所有波次的
scoped prompt/manifest 入口；CLI 打印首波入口。该清单只描述计划，不会自动启动 Subagent。

PDF 的完整书稿结构树与读者阅读目录分开：封面、版权页等明确角色可默认从导航隐藏，前言等前置内容仍保留；
经结构复核的行内标题和原书标号会在正文与阅读目录中使用一致显示。显式可见绑定必须落到正文中的对应标题；结构容器可提供导航落点，但不会把可见绑定降级为文件或容器回退。
正文中已被 OCR 标为 Markdown ATX 标题的行必须进入结构树，或在结构审阅中写明不纳入导航的理由；
否则本地校验会阻断，避免正文存在多级标题而阅读目录被压成一级。`refine.unit_granularity`
可在指定 TOC 深度合并逻辑单元，子标题仍作为多级导航锚点；集中尾注的后代引用可继承父级
note pool，因此不需要为了注释作用域把目录拍平。

外部术语表使用 schema 2 多语言概念格式，按配置的源语言与目标语言直接匹配词条；缺少某一
语言时不会从其他语言推导译法。查看
[`docs/antigravity-workflow.md`](docs/antigravity-workflow.md) 了解字段和选择流程。
缺失语言词形的补充候选流程目前仍是提案，尚未实现；见
[`术语表补充候选设计`](docs/glossary-supplement-design.md)。

可选的动态术语记忆默认关闭。需要跨章节复用经 Reviewer 确认的新译名时，可在配置中设
`translation.dynamic_terminology.enabled: true`；worker 只提交带源/目标证据的候选，由独立 Antigravity Reviewer
审阅后通过 `term-memory-review-prepare` 和 `term-memory-publish` 生成不可变快照。启用后准备翻译前需确认
没有相关活跃 Subagent 会话。`--review` 路径按当前工作目录解析，并必须位于书籍 `output/<title>/` 工作区内。
详细顺序和分波命令见
[`Antigravity 工作流说明`](docs/antigravity-workflow.md#动态术语记忆可选)。

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
填写书名、输入文件、源语言和目标语言；处理 PDF 时还要配置 OCR 服务。Subagent 模型未配置时
默认推荐 `inherit`（继承主会话已验证的模型）。

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
PDF 目录绑定按源稿中 Markdown 渲染后的可见标题、标题/段落类别和出现位置定位；字面星号和标识符下划线
不会被当作强调标记。润色验收会按相同的 Markdown 可见语义保留标题层级、文字和出现顺序，重复标题也逐项校验。
PDF 结构复核还要求逐项处置独立数字，明确绑定标题的源行范围；无证据数字保留在正文，页边位置本身不会触发删除。恢复旧译文时，先刷新 handoff context，
校验现有译文，再按新的恢复报告派发失效单元；具体步骤见 [AGENTS.md](AGENTS.md)。规则版本变化本身
不要求重新 OCR 或重译全书。书目、索引、注释和作者注释可从明确标注的 TOC 节点沿普通章节结构继承；
角色或来源变化只会使对应文件的翻译校验失效，需重新校验后再恢复复用。

PDF 的页脚注抽取会复核疑似跨页断词缺口；需要时会把相邻两页图像交给工作区 Subagent，
由图像证据确认原文截断或恢复缺失续文。`footnotes.mode: disabled` 会关闭页脚注自动提取和链接创建；
若与 TOC 的脚注判断冲突，准备、校验和应用阶段都会显示警告。集中尾注的 Markdown 链接仍可能生成。
页脚注应用后还会生成 `footnote_result_audit.json`，复核残留上标、疑似编号注释、适用范围内的编号缺口和注释边界。需要判断的发现会回到既有视觉复核、校验和应用流程；结果审计未通过时，不会生成有效脚注检查点或继续到 polish/EPUB 构建。`endnotes_only` 跳过页脚注候选扫描，但仍审计适用的尾注定义、截断和物化完整性；`disabled` 会记录为不适用。审计输出复核信号，语义决定由既有审阅流程完成。
维护者可运行 `uv run pdf2epub -c config.yaml footnote-review-summary --json` 汇总既有脚注复核的页面、区域、候选块和决定覆盖；该命令纯只读，输出是成本诊断，不代表生产校验通过。
脚注 worker 的精简 unit context 由候选报告、精确页侧车映射和当前 refined unit mapping 重算校验；侧车哈希变化或旧 projection 版本会要求重跑 `footnote-prepare` 与 `footnote-validate`，既有语义决定文件会保留。
PDF 润色/翻译校验规则升级可能使旧验证记录或脚注归一化 checkpoint 失效；恢复时按 [AGENTS.md](AGENTS.md)
重跑对应校验或脚注准备/校验/应用，不需要仅因规则版本变化而重新 OCR。脚注结果审计合同升级后，旧 normalization receipt 也须按该流程重建；该合同升级本身不要求重新 OCR。原始 OCR 与原稿保留，应用阶段重建归一化派生稿；之后按当前性报告恢复受影响的 polish、翻译或构建检查点。

PDF 和 EPUB 的结果通常位于 `output/<书名>/`：PDF 翻译生成原语言 EPUB 和译文 EPUB，PDF 只转换时生成原语言 EPUB，
EPUB 翻译生成译文 EPUB。

高保真 EPUB 重建可配置外部 EPUBCheck。需安装官方 W3C EPUBCheck Java 发布版本，并通过 PATH 或
`html_translation.epubcheck_path` 指定；warn 模式缺少工具时会记录跳过，不代表检查通过，strict 模式会失败。

### 工作派发遇到 429 时恢复

遇到服务商 429 后，先记录错误，再读取下一次可派发时间和有效并发上限：

```text
uv run pdf2epub -c config.yaml dispatch-record-failure --task translate --worker-id worker_001 --assignment-sha256 <current-digest> --kind rate_limit --retry-after 120 --actual-peak-concurrency 2 --temporary-cap 2
uv run pdf2epub -c config.yaml dispatch-status --task translate --wave-reason ordinary --available-cap 2 --active-workers 0 --json
```

`--available-cap`、`--active-workers` 和 `--wave-reason` 由操作员按真实 IDE 空闲槽位、当前任务正在运行的 worker 数（含 metadata）及当前波次填写。`concurrency_cap` 是总并发上限，取配置、波次和临时上限中的最小值；`new_worker_slots` 才是最多可新开的数量，按空闲槽位与 `concurrency_cap - active_workers` 两者取小。活动数未知时新开数显示为 unknown；空闲槽位为 0 时显示 0。它只是操作员派发合同，不读取或控制 IDE 会话，也不会中止已运行的 worker。

短时请求限流用 `rate_limit`，已知额度重置时间用带 `--reset-at` 的 `quota_exhausted`，服务商未说明原因时用 `unknown_429`。缺少额度重置时间或有限重试次数耗尽后会阻断新派发。恢复时先核对真实 IDE 会话、lease 和验证结果；到达 Retry-After/reset 时间后，再只恢复 pending/invalid 单元。额度错误还需明确确认额度可用，例如：

```text
uv run pdf2epub -c config.yaml dispatch-resume --task translate --reason "session status reconciled and provider quota reset confirmed" --confirm-sessions-reconciled --confirm-quota-available
```

`dispatch-resume` 只写入操作员审计，不会释放 lease、验证译文、重试网络请求或切换模型。降低并发不保证解决小时额度耗尽。

## 文档

- [AGENTS.md](AGENTS.md)：完整操作步骤与限制。
- [Antigravity 工作流说明](docs/antigravity-workflow.md)：各流程细节、配置和恢复方法。
- [架构说明](docs/architecture.md)：模块边界和产物说明。
- [维护指南](docs/maintenance.md)：源码、测试与文档维护规则；维护入口见 [codex.md](codex.md)。
