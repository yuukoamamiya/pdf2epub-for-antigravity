# 维护任务入口（OpenCode / Codex）

本文件是给编码 Agent 的短入口。完整规则见 [`docs/maintenance.md`](docs/maintenance.md)。

## 任务分类

- **仓库维护**：代码、测试、文档、CI、工具或重构。不需要 Antigravity 工作区 Subagent。
  Codex 主代理按 [`codex.md`](codex.md) 调度 GPT-6 Luna Codex 子代理实施并独立验收；OpenCode
  等其他编码 Agent 可直接实施。
- **已授权的既有产物格式修复**：用户明确要求修现有 EPUB、旧译稿的纯排版/格式，或用现有译文
  重建 EPUB 时，同样按维护任务处理，不要求工作区 Subagent。可读取定位和验收所需的正文及旧
  产物；在 `.work/` staging 或独立副本中修复（`.work/` 仅用于 staging、证据和脚本）。验收
  完成的修复 EPUB 以明确的修复版文件名另存到对应 `output/<title>/`，交付链接指向该文件，保留
  原稿和原 EPUB。优先复用现有译文，最小化内容变化，不重译、不调用 OCR/翻译服务、不伪造
  checkpoint、不静默绕过门禁。此授权不扩展为任意修改 `output/`。
- **新书稿生产**：翻译、润色、OCR 结构判断、TOC、术语、元数据翻译、脚注/插图语义决定，仍
  按 `AGENTS.md` 使用工作区 Subagent。`AGENTS.md` 的 **Subagent 总闸只适用于这类书稿内容任务**。

## 执行顺序

1. 开工先运行 `git status --short` 和 `git branch --show-current`，确认在 `main`，记录已有
   修改与未跟踪文件；不清理、不覆盖、不纳入本次任务的改动。
2. 先复现问题并定位根因：代码缺陷用最小合成 fixture 复现；旧 EPUB 排版缺陷检查对应 DOM、
   链接和渲染页，并排查全书同类问题，不只修截图中的单页。
3. 跟随现有模块边界和命名风格。改配置、流程、产物合同或后端时，同步检查 `README.md`、
   `AGENTS.md` 和相关 `docs/`。只修改本次约定范围内的文件。
4. 完成前核对 `git status --short` 与实际 diff。按任务要求运行适用测试（文档专属任务不运行
   测试），始终运行 `git diff --check`。代码改动按 DeepSec skill 扫描改动范围并处理
   findings，汇报扫描范围和结果；未扫描时如实说明。
5. 收尾说明：改了哪些文件、为什么改、怎么验证、哪些工作区既有修改保持不动、是否有剩余限制。

## 路径、提交和推送

- Windows 普通命令使用 Git Bash，Shell 路径固定为
  `C:\Users\AmamiyaYuuko\scoop\apps\git\current\bin\bash.exe`；结构化转换和非简单文本逻辑
  用 Python。项目命令用 `uv run ...`。
- 在 `main` 上工作。不要创建 `codex/*`、`feature/*`、`fix/*`、`agent/*` 等临时分支。
- 不要 `git add .`；只暂存本次实际修改的文件。不提交 `config*.yaml`、`input/`、`output/`、
  `.secrets/`、日志、`.work/`、scratch 或临时脚本。
- 不要为了跑测试修改用户配置，也不要在真实 `output/<title>/` 上运行 `ocr-pages`、`refine-local`、
  `polish`、`translate`、`build-epub` 等生产流程；验证用 `tmp_path`、合成 fixture 或 `.work/`
  下的临时副本。默认只读诊断可读 `output/<title>/*.json` 报告。
- Codex 主代理按 `codex.md` 自动选择性提交已验收的维护改动；OpenCode 等其他 Agent 未获用户
  要求时不提交。push 始终需要人类明确授权，禁止自动推送、force push 或改写已推送历史。
- 推送默认只推 `fork`（上游为 `origin`），除非用户明确要求。GitHub 推送用本机代理，不改全局
  Git 配置：

```text
git -c http.proxy=http://127.0.0.1:10809 -c https.proxy=http://127.0.0.1:10809 push fork main
```

如果任务实际是处理书稿内容且不属于上述已授权格式修复：停止维护流程，按 `AGENTS.md` 使用工作区
Subagent。
