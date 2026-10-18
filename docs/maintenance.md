# 仓库维护指南（面向 Codex / OpenCode 等编码 Agent）

本文件面向**维护仓库本身**的 Agent：改源码、修 bug、重构、补测试、改文档、改 CI、改配置
模板。它不面向书稿处理流程。

先记住一句话：

> `AGENTS.md` 的“Subagent 总闸”只约束**书稿内容任务**，不约束**代码/测试/文档维护任务**。

维护任务可以直接读取和修改 `pdf2epub/`、`tests/`、`docs/`、`README.md`、`AGENTS.md`、
`pyproject.toml` 和 CI 配置，不需要打开 Antigravity 工作区 Subagent，也不需要让 Subagent
代写代码。

---

## 1. 先判断任务类型

### 1.1 书稿流程任务（必须遵守 AGENTS.md 的 Subagent 总闸）

出现以下目标时，属于书稿流程，不是维护：

- 翻译、润色、改写正文；
- OCR 纠错、版面结构判断；
- 生成或判断 `toc_tree.json`、章节层级、标题归属；
- 提取术语、决定术语译法、翻译元数据；
- 判断脚注、尾注、插图属于正文还是页面装饰；
- 直接写入 `translated/`、`polished_markdown/`、`translated_compressed/`、
  `translated_novel/`、`translated_tex_units/` 等译文目录。

这类任务必须按 `AGENTS.md` 使用 Antigravity 工作区 Subagent。主 Agent 不阅读正文并翻译，
不把译文贴在聊天里，不代替 Subagent 写目标文件。

### 1.2 仓库维护任务（本文件适用范围）

出现以下目标时，属于维护：

- 修复 Python 代码、CLI、校验器、构建器、OCR 后端适配；
- 增加或修改测试、fixture、CI；
- 更新 README、AGENTS.md、docs/；
- 重构模块、拆分函数、调整报告字段；
- 修复 Windows 兼容性、路径处理、编码问题；
- 评估或实现某个工作流改进。

维护任务**不需要** Antigravity Subagent，**不需要**运行真实书稿流水线，也不应该为了“验证”
去跑 OCR、翻译或打包真实书籍。

### 1.3 混合任务

如果用户同时要求“改代码”和“处理这本书”，把两件事分开：

1. 代码改动直接做，用测试验证；
2. 书稿内容处理按 `AGENTS.md` 交给工作区 Subagent。

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

### 2.2 不要自动提交或推送

- 用户没有明确说“提交”时，不要 commit。
- 用户没有明确说“推送”时，不要 push。
- 推送时默认只推 `fork`，不要推 `origin`（上游 `ShenSheiBot/pdf2epub`），除非用户明确要求。
- 不要 force push，不要改写已推送历史，不要 amend 已推送提交。

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
- `.secrets/`、`*.env`、凭据文件；
- `logs/`、`.work/`、`scratch/`、`.venv*/`；
- 一次性验证脚本、临时报告、真实书稿快照。

推送前确认忽略规则仍然命中：

```text
git check-ignore -v .secrets config.yaml output
```

### 2.4 不要修改用户配置和书稿目录

维护任务默认不碰：

- `config*.yaml`
- `input/`
- `output/`
- `.secrets/`
- `pages/`、`ocr_markdown/`、`translated/` 等运行产物

除非用户明确要求修某个配置或检查某个产物。为了跑测试而改 `config.yaml` 是错误做法；
测试应使用 `tmp_path` 和合成 fixture。

### 2.5 不要用真实书稿做测试

不要在 `output/<title>/` 上运行：

- `ocr-pages`
- `refine-local`
- `polish`
- `translate`
- `build-epub`

这些命令会写文件、消耗额度、改变 checkpoint，并且可能覆盖用户正在进行的任务。需要验证
代码时，用 `tmp_path`、最小 Markdown/JSON fixture 或单独复制的临时目录（放在 `.work/`，
不要提交）。

只读诊断可以读 `output/<title>/*.json` 报告；不要阅读或翻译其中的正文内容。

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

CI 配置在 `.github/workflows/ci.yml`，本地应能跑通等价命令。

### 4.5 文档

行为、配置、CLI、产物或错误处理发生变化时，更新对应文档。至少检查：

- `README.md`（用户可见能力/用法）；
- `AGENTS.md`（执行流程规则）；
- `docs/architecture.md`、`docs/antigravity-workflow.md`（合同和维护细节）。

文档中的工作区相对路径统一使用 `/`，不要生成 Windows 反斜杠路径。

### 4.6 提交

只有用户明确要求提交时才执行：

```text
git status --short
git diff --check
uv run pytest -q
git add <明确文件...>
git commit -m "<符合仓库风格的说明>"
```

提交信息使用现有风格，例如：

- `fix(toc): ...`
- `feat(footnotes): ...`
- `test(ocr): ...`
- `docs: ...`

不要 `git add .`，不要把 `config.yaml`、`output/`、`.secrets/` 带进提交。

---

## 5. 常见维护场景

### 5.1 “修一个 bug”

直接改源码和测试，不需要 Subagent。先写失败测试或最小复现，再修实现。修完跑目标测试和
全量测试。

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

### 5.5 “用户要求翻译/润色一本书”

停止维护模式，转到 `AGENTS.md`：

1. 判断 PDF / EPUB / 轻小说 / TeX 流程；
2. 运行准备命令生成 prompt 和 manifest；
3. 打开 Antigravity 工作区 Subagent，让它直接写目标文件；
4. 主 Agent 只做校验、调度、恢复和打包。

这部分不是编码 Agent 的职责，也不要由编码 Agent 代写译文。

---

## 6. 安全与数据边界

- 不硬编码密钥、token、内部项目 ID 或伪造请求头。
- 不读取、打印、提交 `.secrets/`。
- 不把真实书稿正文粘贴到聊天或提交到仓库。
- 测试不得访问真实 OCR/翻译 API；用 mock 或纯本地 fixture。
- 需要网络时先确认是否真的必要；项目测试应默认离线可跑。
- 不删除用户的 `input/`、`output/`、`.work/` 或已有 checkpoint。

---

## 7. 冲突与升级

- 安全、Git、书稿流程的正确性规则以 `AGENTS.md` 为准。
- “谁可以改代码”的边界以本文件为准：维护任务不受 Subagent 总闸约束。
- 如果两者看起来冲突，先做只读检查；仍无法判断时询问用户，不要自行扩大解释。
- 如果维护改动会影响书稿流程的行为，必须在回复中说明影响范围，并提示需要重跑哪些阶段。
