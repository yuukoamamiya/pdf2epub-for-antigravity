# 维护任务入口（OpenCode / Codex）

本文件是给编码 Agent 的短入口。完整规则见 [`docs/maintenance.md`](docs/maintenance.md)。

- 你在做**代码、测试、文档、CI、工具或重构**：直接修改仓库文件，不需要 Antigravity 工作区
  Subagent，也不需要让 Subagent 代写代码。
- `AGENTS.md` 的 **Subagent 总闸只适用于书稿内容任务**：翻译、润色、OCR 结构判断、TOC、
  术语、元数据翻译、脚注/插图决定。
- 在 `main` 上工作。不要创建 `codex/*`、`feature/*` 等临时分支；用户没要求就不要 commit
  或 push。
- 不要 `git add .`；不要提交 `config*.yaml`、`input/`、`output/`、日志和临时脚本。
- 不要为了跑测试修改用户配置，也不要在真实 `output/` 上运行 OCR、翻译或打包流水线。
- 测试用 `uv run pytest -q`，提交前跑 `git diff --check`。
- 推送 GitHub 前跑 `deepsec shield scan <改动路径> --format json`，修复 findings 后再推。
- GitHub 推送使用本机代理：
  `git -c http.proxy=http://127.0.0.1:10809 -c https.proxy=http://127.0.0.1:10809 push fork main`
- 如果任务实际是处理书稿内容：停止维护流程，按 `AGENTS.md` 使用工作区 Subagent。
