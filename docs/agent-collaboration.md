# Agent 协作指南

本文件给**不同编码 Agent**（Codex、OpenCode、Claude Code 等）交接仓库维护工作时使用。
它回答三个问题：现在仓库处于什么状态、动手前该看哪份权威文档、做完怎么交接。

执行规则本身仍以 [`AGENTS.md`](../AGENTS.md)（书稿流程）和
[`docs/maintenance.md`](maintenance.md)（仓库维护）为准；本文件只做导航和现状记录，
冲突时以那两份为准。

---

## 1. 三分钟上手

```text
git status --short          # 先看有没有别人的改动；不要清理、不要纳入本次提交
git branch --show-current   # 必须是 main；不要建临时分支
uv run pytest -q            # 基线：1630 个测试（4 skipped），约 40 秒
uv run pdf2epub --help      # 工作流顺序由 pdf2epub/workflow_stages.py 渲染
```

- 代码规模：`pdf2epub/` 约 74 k 行 / 220 模块，`tests/` 约 42 k 行。
- 全部本地命令用 `uv run ...`；不要调用真实 OCR / 模型服务做验证。
- 不要在真实 `output/<title>/` 上跑 `ocr-pages`、`refine-local`、`polish`、`translate`、
  `build-epub` 等生产命令；用 `tmp_path`、合成 fixture 或 `.work/` 副本。
- 只读诊断可以放心读：`uv run pdf2epub -c <config> status`、`doctor`、
  `footnote-review-summary`。

## 2. 文档地图（谁说什么算数）

| 文档 | 回答什么 | 权威性 |
|---|---|---|
| [`AGENTS.md`](../AGENTS.md) | 书稿流程怎么执行、Subagent 总闸、门禁顺序 | 执行规范最高优先级 |
| [`docs/maintenance.md`](maintenance.md) | 维护任务的边界、验收、发布规则 | 维护任务规范 |
| [`docs/architecture.md`](architecture.md) | 模块边界、产物合同、依赖方向 | 结构事实 |
| [`docs/antigravity-workflow.md`](antigravity-workflow.md) | 各阶段交接细节、恢复语义 | 合同细节 |
| `pdf2epub/workflow_stages.py` | PDF 阶段顺序、命令名、`--help` 渲染 | **机器可读的流程顺序** |
| `pdf2epub/pipeline_policy.py` | 流程能力/门禁开关 | 流程能力唯一来源 |
| 本文件 | 当前状态、契约→测试映射、遗留 backlog | 交接现状 |

流程顺序只允许改 `workflow_stages.py`；`--help`、`status` 的阶段名与命令由
`tests/workflow_stages_test.py` 强制对齐，手改其中一处会直接测试失败。

## 3. 契约 → 测试映射

改某块之前先跑对应测试，改完再跑一次全量。

| 区域 | 关键模块 | 守护测试 |
|---|---|---|
| PDF 阶段顺序 / `--help` | `workflow_stages.py`, `cli.py` | `tests/workflow_stages_test.py` |
| `status` / `doctor` | `commands/diagnostics.py` | `tests/diagnostics_test.py` |
| OCR 输入身份与类型 | `commands/sources.py`, `commands/ocr.py` | `tests/ocr_input_identity_test.py` |
| OCR 页面来源判定 | `ocr_source.py`, `ocr_consensus.py` | `tests/workflow_optimizations_test.py`, `tests/ocr_consensus_test.py` |
| 跨平台状态锁 | `state_lock.py` | `tests/state_lock_test.py` |
| PDF 装箱 / 派发波次 | `worker_grouping.py`, `worker_prompt.py`, `subagent_runtime.py` | `tests/worker_dispatch_plan_test.py`, `tests/subagent_dispatch_test.py` |
| 脚注 / 尾注合同 | `refine/footnote_*.py`, `refine/endnote_*.py` | `tests/endnote_normalization_test.py`, `tests/footnote_*_test.py` |
| 翻译/润色校验 | `markdown_validation*.py` | `tests/markdown_*_test.py` |
| EPUB 构建与渲染 | `build_epub.py`, `epub/`, `html_translation/` | `tests/epub_visual_regression_test.py`, `tests/html_epub_builder_test.py` |
| CI | `.github/workflows/tests.yml` | GitHub Actions（Windows + Linux，Python 3.12） |

## 4. 2026-11-20 审计批次已完成项

这些改动已经落地并各自独立提交；接手时不要重复修，也不要回退它们的测试。

| 提交 | 内容 |
|---|---|
| `b7885bd` | `ocr-pages -i` 现在校验 PDF 类型/魔数，并拒绝把与本书记录不符的源写进同一 output（`--allow-source-change` 显式放行） |
| `ee05f14` | `status` 增加 `current` / `reachable`；下游 `passed` 变 `stale`；不再给出无法执行的 `next_command`；`STATUS_SCHEMA_VERSION` 2 |
| `1ce8b15` | 阶段表 `workflow_stages.py` 成为唯一来源；`--help` 修正 `build-epub --only-translated`，补齐 `status`/`check-ready`/`dispatch-*`/`special-unit-*`/`repair-page-furniture` |
| `c816161` | `pages/ocr_progress.json` 损坏时 fail-closed（`ValueError`），不再静默当作"没有记录" |
| `48662d8` | `state_lock` 的 `timeout` 在 POSIX 与进程内互斥上都生效 |
| `7fb70f8` | 删除零引用模块 `utils/zip_utils.py`、`utils/html_utils.py` 与 `get_backend()` |
| `9622096` | 新增 CI：Windows + Linux 跑 `uv run pytest -q` |
| `1a59cdf` | `.gitignore` 忽略本地 `archive/`（约 700 MB） |
| `485de4b` | `web/` 演示代理改为白名单主机 + 来源白名单 + 10 MB 上限，不再是无认证中继 |
| `056a043` | `status` 增加 `review_gates`：各阶段待复核文件数、候选数、worker 派发状态与波次数 |
| `fab9f15` | `commands/registry.py` 的 837 行 `register_command_parsers()` 拆成 12 个 `_register_*` 分组函数（命令顺序、help 文案、handler 路径逐字未变） |
| `8db3489` | CLI 统一错误边界：未预期异常打印一行并返回 1，`--debug` / `PDF2EPUB_DEBUG=1` 才展开 traceback；`Ctrl-C` 返回 130 |
| `cc7724c` | dynamic terminology 的 manifest 盖章抽成 `manifest_dynamic_terminology_stamp()`，PDF 与 EPUB 共用 |
| `f4aa8ac` | resume 现在按文件说明 pending 原因（`source_changed` / `context_version_bumped` / `context_changed` / `receipt_missing` / `target_missing` / `review_reopened` / `checkpoint_invalid` / `fresh_run`），父 manifest 写 `pending_reasons` + `pending_reason_counts`，`status.review_gates` 同步显示；`unit_context_mismatch_reason()` 是判定分量的唯一来源 |
| `df1f0e0` | 新增本文件并同步 AGENTS/architecture/maintenance/opencode |
| `3d97c7a` | `prepare_markdown_subagent()` 改为只接收 `MarkdownHandoffRequest`，删除 29 参适配段；125 个调用点（4 生产 / 121 测试）同步改为构造 dataclass，行为与默认值不变 |
| `f39520c` | `status` 增加 `ocr_evidence`（`two_ocr` / `single_ocr` / `legacy_native` / `indeterminate`）并升到 schema 3，单 OCR 不再读成已复核；handler 捕获的失败复用同一个 `--debug` 判定，保留异常类型与可选 traceback |

## 5. 已知遗留 backlog

按价值排序。动手前先读对应模块和测试，并按第 3 节跑守护测试。

1. **receipt 迁移命令**（P1，`markdown_handoff_resume.py`、`markdown_validation*.py`）
   pending 原因分类已完成（见 `f4aa8ac`），`translate/polish --resume --dry-run` 现在可只读
   预览原因与 worker 批次（见 `feat(handoff)` 提交）。仍缺一次性的
   `refresh-checkpoints`：只在源哈希一致时重盖章旧 receipt，免去
   `prepare → validate → prepare` 三步走。实现前必须先证明旧快照与当前快照除版本戳外完全
   相同，否则宁可不刷新；relax 到"源哈希一致即可"会接受在旧投影下验证过的译文。
2. **巨型函数拆分**（P1）
   `commands/registry.py` 已完成（见 `fab9f15`）。剩余：
   `refine/footnote_handoff.py:211 prepare_footnote_subagent()`（1126 行）、
   `refine/footnote_result_audit.py:299`（534 行）、`refine/footnote_regions.py:33`（509 行）、
   `commands/markdown_readiness.py:30`（449 行）、`refine/footnote_apply.py:81`（413 行）。
   拆分必须保持 manifest/report schema 与哈希合同不变，并跑全量测试。
3. **Azure / Vision 后端的重复实现**（P2）
   `ocr/backends/azure.py` 与 `vision.py` 有 219 行逐字相同（约占较小文件的 34%）：
   图片落盘、坐标转换、插图提取、纵向栏处理各写了一遍。两者都是可选 legacy 后端，
   合并前先确认还要不要保留这两个后端；若要保留，抽 `_image_page_backend_common.py`。
4. **Notes 解析的重复实现**（P2）
   `footnote_normalization.py`（polish 校验）与 `refine/endnote_normalization.py`（apply）
   各自维护一套定义正则。`endnote_definition_keys()` 已经共享，但
   `_MARKDOWN_DEF_RE` / `_RAW_NOTE_DEF_RE` / `_RAW_SUP_NOTE_DEF_RE` 仍是两份且语义有细微差异；
   合并前必须逐条比对（apply 侧要求行尾有内容，校验侧只匹配前缀）。不要为了去重改变行为。
5. **`.gitattributes` 与工作区换行**（P2）
   仓库索引是 LF、工作区是 CRLF，`git status` 会列出约 200 个 stat-only 改动。
   建议在**工作区干净时**单独做一次 `git add --renormalize .` 并作为独立提交；不要在
   有未提交改动的批次里做。
6. **`status` 的 `package` 阶段**（P2）
   目前只要存在 `*.epub` 就视为有产物；结合 `current/reachable` 已不会误报，但更严格的
   做法是校验 EPUB 的构建 receipt 是否对应当前 polish/translate 检查点。

### 已评估但决定不做

- **worker prompt 瘦身**：每个 scoped prompt 约 20 KB，含整书装箱与派发规则。经核查这是
  **有意共享的操作员合同**（`docs/architecture.md` §2.4、`tests/workflow_optimizations_test.py`
  与 `tests/worker_dispatch_plan_test.py` 都断言它存在）。删减属于合同变更，不是 bug 修复；
  若要改，先改文档和这两处测试，再改 `worker_prompt.py`。
- **`pages_fingerprint` 复用哈希缓存**：实测 1330 个页面文件 0.040 s（直接读）vs 0.118 s
  （走 `sha256_file` 的 mtime 缓存，多出 resolve/stat 开销）。当前实现更快，不要"优化"。

## 6. 协作约定

- **提交粒度**：一个可独立验证的功能/修复一个提交；提交信息写"为什么"而不只是"改了什么"。
  不把无关的既有改动夹带进来，不 `git add .`。
- **验收**：改动后至少跑受影响的测试，收尾跑一次 `uv run pytest -q`；始终跑
  `git diff --check`。代码改动按 DeepSec skill 扫描改动范围
  （`deepsec shield scan <改动文件>`；注意该 CLI 一次只接受一个路径）。
  2026-11-20 批次改动的 13 个文件扫描结果为 0 findings。
- **禁止**：`push`（需人类明确授权）、force push、改写已推送历史、提交
  `config*.yaml` / `input/` / `output/` / `.secrets/` / `.work/` / 日志。
- **不要伪造 checkpoint**：校验不过就修内容或修代码，不要手改 JSON 或加
  `--allow-review-warnings` 绕过门禁。
- **不要碰书稿**：本文件的读者默认在做仓库维护；新翻译/润色/OCR 结构判断必须回到
  `AGENTS.md` 的工作区 Subagent 流程。

## 7. 交接模板

收尾时按这个结构汇报，下一个 Agent 才能无缝接手：

```text
改动：<文件:行> …（每个文件一句话为什么）
验证：<实际运行的命令与真实输出摘要>；未运行的检查要写明
契约影响：是否改了 schema/版本/产物路径；对应测试是否同步
工作区：哪些既有改动我没碰；是否有未提交残留
遗留：本次没解决的问题与原因（指向 backlog 编号）
```
