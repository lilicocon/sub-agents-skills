# 本机验证记录 · 2026-09-12

## 环境与范围

- macOS arm64；执行器验证使用本机 Python 3.14 和 Python 3.9。
- Codex CLI 0.154.0；主模型配置保持 gpt-6-astra / medium。
- Cursor CLI 2026.09.10-fd3934a；Grok CLI 1.0.30。
- 本地仓库原有 254 项测试；审查跟进后 291 项在 Python 3.14 上通过。本机当前没有 Python 3.9。
- macOS 上实际运行；Linux/Windows 未在本次会话执行。CI 已加入
  ubuntu-latest、macos-latest、windows-latest 和 Python 3.9/3.12 矩阵。
- mypy 默认平台及 `--platform win32` 均通过。类型检查不是 Windows 运行实测。
- ruff check、ruff format --check、插件同步、技能及插件结构校验通过。

## 不调用模型的验证

真实子进程验证了大量 stderr、超长单行、终态后持续输出、正常收尾、
超时、取消、外部 SIGTERM、后代清理和 owner 崩溃清理。保留并更新原有
多后端解析和同步调用测试。

任务测试覆盖 SQLite schema、跨进程锁、容量限制、非 Git 目录重叠、
依赖失败、排队取消、重启时保留存活 worker、失联 worker 标记 interrupted、
实际 Git worktree 隔离、未提交输入拒绝、缺失产物拒绝验收、增量日志和截断。

另有不调用模型的真实后台集成测试：submit → detached supervisor → worker
→ fixture CLI → result → shutdown。Windows CI 将运行同一测试及 Job Object
清理测试；不依赖 CI 的模型账户。

## 真实 Cursor / Grok 闭环

在独立临时 Git 项目中设置一个错误的 `clamp` 实现和现有 unittest：

| 阶段 | 实际结果 | 任务耗时 |
| --- | --- | --- |
| Grok 调查 | 发现只返回原值的问题，给出边界裁剪修复 | 17.17 秒 |
| Cursor 实现 | 独立 worktree 修改一行，运行 unittest 通过 | 34.86 秒 |
| Grok 审查 | 审查真实未提交差异和测试，明确通过 | 43.50 秒 |
| Codex 验收 | 独立读取 diff、复跑 unittest、记录验收 | 已完成 |
| 集成与清理 | 在临时项目提交并 fast-forward 集成；复跑通过；清理 worktree | 已完成 |

首次 Grok 调查暴露新版 stopReason 为 `end_turn` 的兼容问题。修复后重新
执行调查通过；原失败任务保留，未篡改历史结果。

Cursor 实现阶段返回的用量：inputTokens 21023、outputTokens 892、
cacheReadTokens 100864、cacheWriteTokens 0。Grok 此次最终载荷未提供用量，
不推算费用，也不据这一个小任务断言节省成本。

实际运行记录曾保存在：
`/private/var/folders/7m/d7nsdbz55pd_9yb8qbttr1nw0000gn/T/runner-validation-8l57uhd6/state`。
这是临时目录，长期依据以本报告为准。后台 supervisor 已请求关闭。
本次示例项目的提交与合并没有影响插件仓库的 Git 历史。

## 使用与限制

插件注册在本地 marketplace `sub-agents-skills`，名称 `runner`。
新开 Codex 会话后输入 `$runner:sub-agents` 并描述任务。
开发更新后，先完成/取消活动任务，再同步插件、更新 cachebuster 并重新安装。

`completed` 仅表示 CLI 协议成功；输出存在性和人工/主 Agent 验收独立。
进度描述也可能是非空字符串，语义正确性仍由 Codex 审查。

## 审查跟进（同日）

全面审查复现了现有 283 项测试未覆盖的问题，并已修回归：

- Git 检测失败不再降级到源目录写入。
- POSIX 超时会清理脱离原会话的后代进程。
- 多行/pretty JSON 完整结果不再被报成超时。
- 已完成任务不会被调度器旧状态覆盖成 `interrupted`。
- CRLF 补丁按字节导出，`git apply --check` 可通过。
- 每次 submit 快照调用方环境，不沿用 supervisor 启动时的环境。
- cleanup 在其他任务仍使用该 worktree 时拒绝删除。

Git 写任务要求已提交的基线；结果在独立 worktree，Runner 不自动集成。
会话恢复只保留任务/产物，不自动重放模型任务。后台管理不运行网络服务。
