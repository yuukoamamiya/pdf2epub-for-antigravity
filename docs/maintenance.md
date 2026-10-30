# 仓库维护指南（面向 Codex / OpenCode 等编码 Agent）

本文件面向**维护仓库本身**的 Agent：改源码、修 bug、重构、补测试、改文档、改 CI、改配置
模板。它不面向书稿处理流程。

先记住一句话：

> `AGENTS.md` 的“Subagent 总闸”只约束**书稿内容任务**，不约束**代码/测试/文档维护任务**。

维护任务可以直接读取和修改 `pdf2epub/`、`tests/`、`docs/`、`README.md`、`AGENTS.md`、
`pyproject.toml` 和 CI 配置，不需要打开 Antigravity 工作区 Subagent。Codex 的实现委派遵循
[`codex.md`](../codex.md)；其他编码 Agent 可直接实施。

---

## 1. 先判断任务类型

### 1.1 书稿流程任务（必须遵守 AGENTS.md 的 Subagent 总闸）

出现以下目标时，属于书稿流程，不是维护：

- 翻译、润色、改写正文；
- OCR 纠错、版面结构判断；
- 生成或判断 `toc_tree.json`、章节层级、标题归属；
- 提取术语、决定术语译法、翻译元数据；
- 判断脚注、尾注、插图属于正文还是页面装饰；
- 为新书稿生产而直接写入 `translated/`、`polished_markdown/`、`translated_compressed/`、
  `translated_novel/`、`translated_tex_units/` 等译文目录。用户明确授权的既有 EPUB/旧译稿
  纯格式修复不属于新书稿生产；其范围和隔离要求见第 2.4、2.5 节及 `codex.md`。

以上书稿判断条目指新书稿生产；既有产物格式修复所需的局部核对按第 2.4 节处理。

上述新生产任务必须按 `AGENTS.md` 使用 Antigravity 工作区 Subagent。主 Agent 不阅读正文并
翻译，不把译文贴在聊天里，不代替 Subagent 写目标文件。

### 1.2 仓库维护任务（本文件适用范围）

出现以下目标时，属于维护：

- 修复 Python 代码、CLI、校验器、构建器、OCR 后端适配；
- 增加或修改测试、fixture、CI；
- 更新 README、AGENTS.md、docs/；
- 重构模块、拆分函数、调整报告字段；
- 修复 Windows 兼容性、路径处理、编码问题；
- 评估或实现某个工作流改进。

一般编码 Agent 维护任务不需要 Antigravity Subagent，也不需要运行真实书稿流水线。Codex
仓库维护遵循根目录 [`codex.md`](../codex.md)：主代理诊断并明确范围，调度 GPT-6 Luna Codex
子代理实施，随后由主代理独立验收并按规则提交；本指南的通用维护建议不取代该流程。

### 1.3 混合任务

如果用户同时要求“改代码”和“处理这本书”，把两件事分开：

1. 代码改动直接做，用测试验证；
2. 新翻译、语义润色及新生产中的 OCR/TOC/脚注/插图等结构判断按 `AGENTS.md` 交给工作区
   Subagent；经明确授权的既有产物纯格式修复按第 2.4、2.5 节隔离处理，不要求 Antigravity
   工作区 Subagent；Codex 的方案→Luna 实施→主代理验收流程见 `codex.md`。

无法判断任务类型时，先只读检查，再向用户确认。不要用“可能涉及书稿”为理由拒绝修改代码。

---

## 2. 绝对不要做的事

### 2.1 不要创建临时分支

本仓库直接在 `main` 上维护。不要因为 Codex/OpenCode 的默认习惯创建：

- `codex/*`
- `feature/*`
- `fix/*`
- `agent/*`

除非用户明确要求新建分支或 PR。提交前先确认当前分支：

```text
git status -sb
git branch -vv
```

### 2.2 提交授权与禁止自动推送

- Codex 仓库维护中，普通委派只授权子代理实施和自测，不授权暂存或提交。子代理应汇报精确文件/范围、
  diff 摘要和测试证据，并默认将改动留在工作区。主代理独立审查 diff、检查证据、运行适用测试
  和安全检查后，Codex 主代理自动选择性暂存并提交本次任务；用户明确要求不提交时照办。
- 子代理默认不得提交。主代理独立审查并批准精确范围后，若另行明确指示子代理提交，该子代理才
  可提交指定改动。子代理不得夹带任务开始前已有的其他改动。涉及交叠文件时按 hunk 核对并选择性
  暂存。
- push 必须由人类另行明确授权，禁止自动推送。子代理不得自行 push。
- 推送时默认只推 `fork`，不要推 `origin`（上游 `ShenSheiBot/pdf2epub`），除非用户明确要求。
- 不要 force push，不要改写已推送历史，不要 amend 已推送提交。
- GitHub 代理和推送命令见 4.7。

### 2.3 不要 `git add .`

只暂存本次实际修改的文件。提交前检查：

```text
git status --short
git diff
git diff --check
```

禁止提交：

- `config.yaml`、`config_*.yaml`（用户本地配置）；
- `input/`、`output/`（书稿和运行产物）；
- `logs/`、`.work/`、`scratch/`、`.venv*/`；
- 一次性验证脚本、临时报告、真实书稿快照。

推送前按第 6 节运行 DeepSec 扫描。

### 2.4 默认不修改用户配置和书稿目录

维护任务默认不碰：

- `config*.yaml`
- `input/`
- `output/`
- `.secrets/`
- `pages/`、`ocr_markdown/`、`translated/` 等运行产物

除非用户明确要求修某个配置或检查某个产物。经用户明确授权的既有 EPUB/旧译稿纯格式修复，
可以读取完成定位和验收所需的相关正文及产物；应保留原件，在 `.work/` staging 或独立副本
中修复，并另存结果。该授权不包括新翻译、语义润色、OCR 或翻译服务调用，也不等于允许
覆盖 `output/`。为了跑测试而改 `config.yaml` 是错误做法；测试应使用 `tmp_path` 和合成
fixture。

### 2.5 不要在真实书稿目录运行生产流程

维护验证默认不要在真实 `output/<title>/` 上运行：

- `ocr-pages`
- `refine-local`
- `polish`
- `translate`
- `build-epub`

这些命令会写文件、消耗额度、改变 checkpoint，并且可能覆盖用户正在进行的任务。需要验证
代码时，用 `tmp_path`、最小 Markdown/JSON fixture 或单独复制的临时目录（放在 `.work/`，
不要提交）。对明确授权的既有产物修复，如确需重建 EPUB，只能在 `.work/` staging 或独立
副本上复用已有译文，不调用 OCR/翻译服务；保留原件，并说明构建方式及 checkpoint 是否刷新。

默认只读诊断可读 `output/<title>/*.json` 报告，不阅读正文。上述明确授权的产物格式修复可
按授权范围读取必要正文以定位和验收，但不得借机翻译、润色或改写内容。

### 2.6 不要把书稿规则套到代码上

以下说法都是错误的：

- “我必须打开 Antigravity Subagent 才能改代码。”
- “我不能直接写 `tests/`，因为 AGENTS.md 说主 Agent 不能写译文目录。”
- “我应该先运行 `refine-prepare` 再改这个格式化函数。”
- “为了验证这个 CLI，我要先翻译一章。”

代码维护与书稿翻译是两条流水线，不要混用。

---

## 3. 仓库结构速查

```text
pdf2epub/
  cli.py                     CLI 入口和命令注册
  commands/                  各子命令的编排、上下文加载和输出
  ocr/                       OCR 后端、页面产物、consensus、纠错
  refine/                    TOC、页面合并、脚注、尾注、插图、refine-local
  epub/                      EPUB 构建底层（footnotes、builder 等）
  build_epub.py              PDF Markdown -> EPUB 的结构化构建
  markdown_to_html.py        Markdown -> XHTML、脚注预处理
  html_translation/          EPUB/MOBI/AZW3 高保真翻译流程
  tex_translation/           arXiv/TeX 流程
  subagent_runtime.py        worker handoff、scoped manifest、prompt 生成
  markdown_handoff.py        Markdown 交接合同
  *_validation.py            内容、结构、安全校验
  pipeline_policy.py         pipeline/mode 能力与门禁的唯一策略边界
  workflow_contracts.py      原子写入、哈希、报告等公共合同
tests/                       pytest 测试和合成 fixture
docs/
  architecture.md            模块边界、产物合同、维护细节
  antigravity-workflow.md    Subagent 交接、校验和恢复细节
  maintenance.md             本文件
```

文档读者分工：

- `README.md`：使用者；
- `AGENTS.md`：书稿流程执行者（主 Agent / 工作区 Subagent）；
- `docs/maintenance.md`：编码维护 Agent；
- `docs/architecture.md`、`docs/antigravity-workflow.md`：维护者深入细节。

修改流程、配置、产物字段或 CLI 行为时，至少同步检查 `README.md`、`AGENTS.md` 和相关
`docs/` 段落。

---

## 4. 标准维护流程

### 4.1 开工前

```text
git status --short
git log --oneline -8
```

确认工作区已有改动，不要删除或覆盖不属于本次任务的文件。

### 4.2 阅读范围

按任务读取：

- 目标模块及其直接调用方；
- 对应测试；
- 相关文档段落；
- 如果改 CLI，读 `pdf2epub/commands/registry.py` 和命令实现。

不要一开始就通读整个仓库，也不要把 `output/` 当源码阅读。

### 4.3 修改

- 跟随现有模块边界和命名风格。
- 不新增第三方依赖，除非用户明确同意并同步 `pyproject.toml`/`uv.lock`。
- 新增流程能力时，先更新 `pipeline_policy.py`，不要在命令模块里重新实现 `pipeline`/`mode`
  判断。
- 改报告或产物字段时，检查 freshness/checkpoint 逻辑和 schema 版本。
- 不要为了让测试通过而削弱 fail-closed 校验。
- 不为一次性验证新增脚本；临时脚本放 `.work/<task>/`，不提交。

### 4.4 测试

先跑目标测试，再跑全量：

```text
uv run pytest tests/<相关测试>.py -q
uv run pytest -q
git diff --check
```

基线以当前提交为准；如果全量测试数量变化，确认是新增测试而不是跳过/删除测试。

仓库当前不配置自动 CI。维护任务按改动范围在本地运行验收命令；常用命令如下：

```text
uv run pytest -q
uv run python scripts/check_workflow_contract.py
uvx --from ruff==0.14.1 ruff check pdf2epub --select F821,F822,F823
git diff --check
```

可用一个本地入口按顺序运行以上整套检查，任何命令失败或缺失时都会停止并报告失败命令：

```text
uv run python scripts/check_maintenance.py
```

该脚本只使用标准库编排 pytest、workflow contract、固定版本 Ruff 和 `git diff --check`；
它不启用 CI，也不增加项目依赖。视觉回归测试默认只使用 pytest 临时目录。若需保存合成
EPUB 和页面/联系表 PNG 供人工检查，可显式设置 `PDF2EPUB_VISUAL_QA_DIR` 后运行对应测试；
项目验收入口不设置该变量。

文档专属任务通常只需运行 `git diff --check`；维护代码时按改动范围运行相关测试，必要时再运行
全量测试。`scripts/check_workflow_contract.py` 和 Ruff 检查仍可手动运行，不代表启用了自动 CI。

### 4.5 文档

行为、配置、CLI、产物或错误处理发生变化时，更新对应文档。至少检查：

- `README.md`（用户可见能力/用法）；
- `AGENTS.md`（执行流程规则）；
- `docs/architecture.md`、`docs/antigravity-workflow.md`（合同和维护细节）。

文档中的工作区相对路径统一使用 `/`，不要生成 Windows 反斜杠路径。

### 4.6 提交

Codex 主代理独立审查改动、检查验收证据并运行适用测试和安全检查后，自动选择性暂存并提交本次任务；
用户明确要求不提交时照办。普通委派的 Codex 子代理默认不暂存或提交。主代理审查并批准精确范围
后，若另行明确指示子代理提交，该子代理才可提交指定改动。提交前核对暂存区，只纳入本次明确
范围；交叠文件按 hunk 核查并选择性暂存。push 仍须人类另行明确授权，子代理不得自行 push。

代码维护任务可按改动范围运行测试；文档专属任务不要求运行测试。基本提交流程：

```text
git status --short
git diff --check
git add <明确文件或经核对的 hunk...>
git commit -m "<符合仓库风格的说明>"
```

代码改动按范围运行相关测试；文档专属任务不运行测试。

提交信息使用现有风格，例如：

- `fix(toc): ...`
- `feat(footnotes): ...`
- `test(ocr): ...`
- `docs: ...`

不要 `git add .`，不要把 `config.yaml`、`output/` 等本地文件带进提交。

### 4.7 推送到 GitHub

推送前先完成第 6 节的 DeepSec 扫描。本机直连 GitHub 可能失败（连接 reset），推送时给单次
命令加 v2rayN 的本地 HTTP 代理，不要修改全局 git 配置：

```text
git -c http.proxy=http://127.0.0.1:10809 -c https.proxy=http://127.0.0.1:10809 push fork main
```

- HTTP 代理：`http://127.0.0.1:10809`
- SOCKS5 代理：`socks5://127.0.0.1:10808`
- 代理未启动时先启动 v2rayN，或告知用户处理。
- 默认推 `fork`；只有用户明确要求时才推 `origin`（上游）。

---

## 5. 常见维护场景

### 5.1 “修一个 bug”

先写失败测试或最小复现，再修实现并按任务范围验证。Codex 主/子代理分工及提交边界遵循
[`codex.md`](../codex.md)；其他编码 Agent 可直接实施，不需要 Antigravity 工作区 Subagent。

### 5.2 “加一个新配置/新模式”

1. 在 `pipeline_policy.py` 或对应 options 模块定义能力；
2. 更新命令解析和校验；
3. 更新 freshness/checkpoint；
4. 加合法路径和阻断路径测试；
5. 同步 README、AGENTS.md、docs。

### 5.3 “改报告字段”

检查：

- 写入方；
- 读取方；
- schema/版本；
- 旧 checkpoint 是否应失效；
- 文档中的字段说明；
- 测试中的精确断言。

### 5.4 “处理 Windows 路径/编码问题”

- 优先使用 `pathlib.Path`；
- JSON/manifest/Prompt 中的相对路径使用 `/`；
- 读写文本显式 `encoding="utf-8"`；
- 命令使用 Git Bash，不把复杂 Bash 语法丢给 PowerShell；
- 复杂文本/JSON 操作用 Python，不用一次性 shell 拼接。

### 5.5 “生成可分享的 sanitized export”

使用 `scripts/sanitize_output.sh` 从现有输出目录创建独立副本；该工具不会修改源目录或调用者当前目录中的文件：

```text
scripts/sanitize_output.sh [source_dir [destination_dir]]
```

默认源目录是当前目录下的 `output/`。省略目标目录时，工具会在源目录旁的
`.work/sanitized-exports/` 下生成唯一的新目录。指定目标必须尚不存在，且与源目录不能相同或互为祖先；
工具不会删除任何目录。源树内出现 symlink/junction 时会在写出前失败，避免把源树外的数据复制进导出。
导出副本会排除 `.DS_Store`、`config.yaml`、`config_*.yaml`、`input.epub` 和
`input_original.pdf`；其余二进制文件按原字节复制，指定 UTF-8 文本扩展名中的 API token、Bearer
token、代理 URL 和本地路径会做脱敏。JSON/YAML 会解析后递归脱敏并重新序列化，保留结构和值类型；
格式、空白和 YAML 注释可能规范化。已知敏感字段（如 API key、authorization、token、secret）的字符串值
会整体替换为 `[REDACTED]`。调用者应分享生成的目标目录，并保留原始输出不变。

### 5.6 “用户要求翻译/润色一本书”

停止维护模式，转到 `AGENTS.md`：

1. 判断 PDF / EPUB / 轻小说 / TeX 流程；
2. 运行准备命令生成 prompt 和 manifest；
3. 打开 Antigravity 工作区 Subagent，让它直接写目标文件；
4. 主 Agent 只做校验、调度、恢复和打包。

这部分不是编码 Agent 的职责，也不要由编码 Agent 代写译文。

---

## 6. 上传前安全扫描（DeepSec）

推送 GitHub 前，对本次改动范围运行 DeepSec：

```text
deepsec shield scan <改动文件或目录> --format json
```

读取仓库根目录的 `deepsec-report.json`，按 `severity`（critical/high/medium/low）逐条修复
`findings`，修完重跑，确认 findings 清零后再推送。

DeepSec 已覆盖硬编码密钥、供应链/幻觉包、注入、XSS、路径穿越、不安全配置等安全限制，
本文件不再重复列举。需要更深的语义分析时可加 `--layer l3`（需配置 LLM Key）；日常提交
使用默认的 L1+L2 即可，L1/L2 完全离线。

数据边界：

- 测试默认离线，不访问真实 OCR/翻译 API；用 mock 或纯本地 fixture。
- 不删除用户的 `input/`、`output/`、`.work/` 或已有 checkpoint。
- 需要网络时先确认是否真的必要。

---

## 7. 冲突与升级

- Git 和书稿流程的正确性规则以 `AGENTS.md` 为准；安全扫描按第 6 节执行。
- “谁可以改代码”的边界以本文件为准：维护任务不受 Subagent 总闸约束。
- 如果两者看起来冲突，先做只读检查；仍无法判断时询问用户，不要自行扩大解释。
- 如果维护改动会影响书稿流程的行为，必须在回复中说明影响范围，并提示需要重跑哪些阶段。
