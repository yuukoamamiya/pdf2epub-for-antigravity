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

新书目元数据识别与补全按证据合同交由 Antigravity 工作区 Subagent 复核；`auto` 默认尽力补全，校验后的字段可供 PDF/HTML EPUB 构建使用。身份不确定时 auto 跳过未核验补全并继续，最终状态会提示未完成；required 阻断等待复核；私人文稿或混合集合不会按缺 ISBN 或文件名猜测。命令、decision schema、恢复方式和边界见 [元数据识别与补全](docs/metadata-enrichment.md)。Calibre 自动入库不在当前版本范围内。

工作流支持恢复中断任务，并可使用术语表帮助统一译名。EPUB ZIP 在提取时会限制成员数量、
单文件展开大小和总展开大小，并在写入前预检；这些是库级安全预算，不是书籍配置项。
PDF 新运行都使用视觉 OCR；EPUB 和 TeX
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
填写书名、输入文件、源语言和目标语言；处理 PDF 时还要配置 OCR 服务。模板为翻译配置
`gemini-4-hydrogen`，TOC/结构判断和脚注准备配置 `gemini-4-hydrogen`，常规润色、实体初筛和
简单整页插图判断配置 `gemini-3.8-flash`。复杂脚注、多栏或段落边界润色可在该书配置中将
`task_models.polish` 设为 `gemini-4-hydrogen`，依据是操作员确认的书页结构证据。

### 在 Antigravity 中开始

在 Antigravity 中打开本仓库，让 Agent 按照 [AGENTS.md](AGENTS.md) 处理书稿。例如：

书内实体表会按上次成功校验的源快照增量复核。格式刷新可自动复用；内容变化只需复核变更单元，
并保留已接受的实体条目。准备命令不会把未校验的输出认证为完成。

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
PDF 润色会对有显式前后分片映射的源稿提供双向只读上下文，并以源内容和映射哈希绑定验收记录。脚注模式启用时，
润色验收还要求逐单元完成脚注语义复核；发现脚注问题会回到脚注视觉复核流程。恢复旧目标时运行正式的
`polish-validate` 更新校验记录和 validated 副本，再按报告处理失效文件。
PDF 目录绑定按源稿中 Markdown 渲染后的可见标题、标题/段落类别和出现位置定位；字面星号和标识符下划线
不会被当作强调标记。润色验收会按相同的 Markdown 可见语义保留标题层级、文字和出现顺序，重复标题也逐项校验。
PDF 结构复核还要求逐项处置独立数字，明确绑定标题的源行范围；无证据数字保留在正文，页边位置本身不会触发删除。恢复旧译文时，先刷新 handoff context，
校验现有译文，再按新的恢复报告派发失效单元；具体步骤见 [AGENTS.md](AGENTS.md)。规则版本变化本身
不要求重新 OCR 或重译全书。书目、索引、注释和作者注释可从明确标注的 TOC 节点沿普通章节结构继承；
角色或来源变化只会使对应文件的翻译校验失效，需重新校验后再恢复复用。

PDF 的页脚注抽取会复核疑似跨页断词缺口；需要时会把相邻两页图像交给工作区 Subagent，
由图像证据确认原文截断或恢复缺失续文。`footnotes.mode: disabled` 会关闭页脚注自动提取和链接创建；
若与 TOC 的脚注判断冲突，准备、校验和应用阶段都会显示警告。集中尾注的 Markdown 链接仍可能生成。
双 OCR 的人工页面复核可多次使用 `ocr-correct --file PAGE.md` 累积页面。`ocr-correct --resume` 恢复既有选择。
单页 `ocr-correct-validate --file PAGE.md` 只记录该页，完成整书复核后仍须运行无 `--file` 的全量 validate，
才会生成可供后续结构整理使用的 validated OCR。请求绑定源 PDF、数字排序的完整 OCR 页清单及逐页 Markdown/sidecar 哈希。
源变化后需更新复核。已有 handoff 记录人工页选择时，缺失或损坏请求合同也不会允许回退到未经验证的原始 OCR。
候选/决定报告在重流准备期间刷新时，polish 源选择可继续使用仍与 OCR、归一化稿及已记录结构输入匹配的成功基线；这仅用于准备修复，不表示脚注阶段当前有效。Readiness 与 EPUB 构建仍要求完整当前的脚注验证和 apply 收据。

页脚注应用后还会生成 `footnote_result_audit.json`，复核残留上标、疑似编号注释、适用范围内的编号缺口和注释边界。需要判断的发现会回到既有视觉复核、校验和应用流程；结果审计未通过时，不会生成有效脚注检查点或继续到 polish/EPUB 构建。Polish 语义 issue 回流时，过期哈希/合同与当前 issue 无法唯一映射到 primary OCR block 会分别记录；后一种情况保留其他可映射任务，但阻断脚注校验。原 Polish Reviewer 修正现有 semantic sidecar 的精确 source_excerpt 后，重跑 `footnote-prepare`。 相同 source/target/context 和 issue 身份的重复校验不会消耗额外回流轮次；只有修订后仍存在问题才计为新一轮，计数记录在 `review_escalation.json` 中并跨 manifest 重建保留。每次 `footnote-apply` 保留最近成功版本；失败会另存诊断和 staged 文件证据，再恢复旧稿。成功版本变化形成逐文件哈希链；`polish --resume` 只同步唯一匹配的完整 Markdown 块，保留旧润色备份，并将模糊项作为 assigned-file scoped hints 交给 worker。PDF `translate --resume` 可将文件级脚注变更摘要作为建议传给对应 worker；提示绑定当前源稿和已有译文哈希，不会自动修改译文。`endnotes_only` 跳过页脚注候选扫描，但仍审计适用的尾注定义、截断和物化完整性；`disabled` 会记录为不适用。审计输出复核信号，语义决定由既有审阅流程完成。
结果审计现用独立 version 4，`footnote_normalization.json` 使用 schema 10，脚注决定以 effective decision version 1 统一记录候选来源和复核决定。旧 normalization schema 1–9、结果审计 version 1–3 或缺少有效决定集的 receipt，需通过 `footnote-prepare`、待复核项处理、`footnote-validate` 和 `footnote-apply` 重建，之后用 `polish --resume` 刷新润色语义复核。
上一页脚注以断词连字符结束、下一页 OCR 将续词误标为普通 `Text` 时，行内上标通常不会让候选静默消失，与脚注列对齐且有独立脚注证据的块会带精确页/块地址进入视觉复核和 unit context。视觉复核仍决定该块是否真为续注。
维护者可运行 `uv run pdf2epub -c config.yaml footnote-review-summary --json` 汇总既有脚注复核的页面、区域、候选块和决定覆盖；该命令纯只读，输出是成本诊断，不代表生产校验通过。
脚注 worker 的精简 unit context 由候选报告、精确页侧车映射和当前 refined unit mapping 重算校验。侧车哈希变化或旧 projection 版本会要求重跑 `footnote-prepare` 与 `footnote-validate`，既有语义决定文件会保留。polish 邻片边界上下文 projection version 为 2，旧 receipt 会按受影响单元重新 prepare、validate。
PDF 润色/翻译校验规则升级可能使旧验证记录或脚注归一化 checkpoint 失效；恢复时按 [AGENTS.md](AGENTS.md)
重跑对应流程，不需要仅因规则版本变化而重新 OCR。polish schema 6 恢复要求先用 `polish --resume` 完成新的语义复核，再以正式 `polish-validate` 建立 receipt 和 validated 副本；原有润色目标会保留供复核。脚注结果审计合同升级后，旧 normalization receipt 也须重建；原始 OCR 与原稿保留，apply 阶段重建归一化派生稿。

PDF 和 EPUB 的结果通常位于 `output/<书名>/`：PDF 翻译生成原语言 EPUB 和译文 EPUB，PDF 只转换时生成原语言 EPUB，
EPUB 翻译生成译文 EPUB。

`build-epub` 默认行为保持为只构建原语言 EPUB；`--translated` 构建原语言与译文两版，
`--only-translated` 只构建译文版。`status` 按配置的 pipeline 目标检查：翻译 pipeline 要求
译文 EPUB 当前，原语言 EPUB 不能代替它；转换 pipeline 要求原语言 EPUB。每个 receipt target
会单独显示状态和文件路径。无 receipt 的旧 EPUB 仍标为历史产物并按兼容规则通过，但不会根据文件名猜测语言。

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

如果旧工作区会话已结束，或其中保存的模型标识在当前执行环境不可用，先核对真实会话和 lease，
随后对现有输出运行对应的单文件校验及适用的全量校验，确认哪些工作已完成；再运行对应任务的 `--resume`
准备命令刷新 pending/invalid 列表，并用 `dispatch-plan --task <task>`
查看最新 scoped prompt 和 assigned files。已通过当前校验的输出保留并复用；只把仍 pending 的 handoff
交给新的工作区会话，选择当前环境可用的继承/默认模型。不要在失效会话或旧模型标识上反复重试；
只有确认会话结束后才释放对应 lease，不能自动释放仍活跃的 lease。上述状态命令不能读取 IDE 会话本身。

## 文档

- [AGENTS.md](AGENTS.md)：完整操作步骤与限制。
- [Antigravity 工作流说明](docs/antigravity-workflow.md)：各流程细节、配置和恢复方法。
- [架构说明](docs/architecture.md)：模块边界和产物说明。
- [维护指南](docs/maintenance.md)：源码、测试与文档维护规则；维护入口见 [codex.md](codex.md)。
