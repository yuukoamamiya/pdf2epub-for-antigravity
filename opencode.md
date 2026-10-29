# 维护任务入口（OpenCode / Codex）

本文件是给编码 Agent 的短入口。完整规则见 [`docs/maintenance.md`](docs/maintenance.md)。

- 你在做**代码、测试、文档、CI、工具或重构**：不需要 Antigravity 工作区 Subagent。
  Codex 主代理按 [`codex.md`](codex.md) 调度 GPT-6 Luna Codex 子代理实施并独立验收；OpenCode
  等其他编码 Agent 可直接实施。
- `AGENTS.md` 的 **Subagent 总闸只适用于书稿内容任务**：翻译、润色、OCR 结构判断、TOC、
  术语、元数据翻译、脚注/插图决定。
- 在 `main` 上工作。不要创建 `codex/*`、`feature/*` 等临时分支。Codex 主代理按 `codex.md`
  自动选择性提交已验收的维护改动；OpenCode 等其他 Agent 未获用户要求时不提交。push 始终需要
  人类明确授权。
- 不要 `git add .`；不要提交 `config*.yaml`、`input/`、`output/`、日志和临时脚本。
- 不要为了跑测试修改用户配置，也不要在真实 `output/` 上运行 OCR、翻译或打包流水线。
- 按任务要求运行适用测试；文档专属任务不运行测试。提交前跑 `git diff --check`。
- 推送 GitHub 前跑 `deepsec shield scan <改动路径> --format json`，修复 findings 后再推。
- GitHub 推送使用本机代理：
  `git -c http.proxy=http://127.0.0.1:10809 -c https.proxy=http://127.0.0.1:10809 push fork main`
- 如果任务实际是处理书稿内容：停止维护流程，按 `AGENTS.md` 使用工作区 Subagent。
