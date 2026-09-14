---
name: sub-agents
description: Delegate investigation, implementation, and review to external CLI agents; manage persistent tasks, parallel worktrees, cancellation, and results from Codex or another host.
allowed-tools: Bash Read
---

# External Agent Runner

Use `{SKILL_DIR}/scripts/tasks.py` (absolute path relative to this file) for managed
work. The host owns task decomposition, semantic verification and integration.
Existing synchronous calls through `scripts/run_subagent.py` remain supported.

## Start

For installation, upgrades, backend login and custom roles, read
[install-and-configure.md](references/install-and-configure.md). Keep personal
role definitions outside the installed skill directory so upgrades retain them.

Run `python {SKILL_DIR}/scripts/tasks.py doctor` to check Cursor/Grok installations
and supported flags without sending a model request. This does not verify login.
List roles with `tasks.py agents --cwd <absolute-project-directory>`.

Built-ins: `researcher` (Grok, read-only), `implementer` (Cursor, safe-edit),
`reviewer` (Grok, read-only). Select the role matching the task unless the user
specifies a role/backend. Do not ask the user to choose among equivalent roles.
Custom role lookup and full commands are in [task-management.md](references/task-management.md).

## Dispatch

Write a self-contained task prompt to a file: goal, necessary context, exact
working directory, allowed edits, acceptance criteria, and required evidence.
Workers do not inherit this conversation. Do not put secrets in prompts.

Self-contained means the prompt carries the material. A worker that must first
locate the relevant files spends its budget searching and is killed at the
deadline having produced nothing, so supply the sources inline (numbered
excerpts of tens of KB are fine), say explicitly which tools or builds it should
not run, and keep one task to one narrow area. Give review tasks a fixed output
template; without one, backends return progress narration instead of findings.

`--timeout` is milliseconds. It also accepts a united value such as `600s` or
`10m`; a value under one second is rejected rather than expiring on arrival.

```bash
python {SKILL_DIR}/scripts/tasks.py submit --agent implementer \
  --cwd /absolute/project --prompt-file /absolute/task.txt --expect src/result.py
```

The command returns a task ID immediately. Use `status`, `logs`, and `result`
with that ID. Keep the same `--state-dir` if explicitly chosen. Read
[task-management.md](references/task-management.md) before parallel dispatch,
using dependencies, cancelling, or integrating results.

- Independent work may run concurrently (default capacity 2). Dependent work
  waits for its inputs. Use specific, bounded tasks rather than recursively
  delegating broad goals. Workers must not delegate again.
- Git write tasks run in separate worktrees from the recorded clean HEAD.
  Uncommitted input is rejected, never silently stashed or omitted. Do not
  commit user changes merely to satisfy this check; use a suitable baseline.
- Submit a review against the implementer's returned `working_directory` so
  the reviewer sees actual changes. Dependency IDs alone do not change cwd.
- Non-Git directories serialize overlapping tasks; concurrent writes there
  are not supported.
- Background jobs may outlive a chat. Monitor them to a terminal state or
  explicitly report outstanding IDs. Quiet output is not proof of a hang.

## Verify, then integrate

`completed` means the CLI returned a successful protocol result. It does NOT
mean the user's task is complete. Inspect `output_check`, the final report,
actual files/diff, source citations, and relevant test results. Progress-only
responses require a corrective follow-up, even if `has_body` is true.

A timed-out task reporting `stdout_chars: 0` produced nothing at all: backends
using a non-streaming output format buffer the whole reply and lose it at the
deadline. Raising the timeout alone rarely changes that outcome; narrow the
scope or inline the sources instead. A nonzero count means the reply was
genuinely cut off, which a longer deadline can fix.

Use `accept ID --verdict accepted|rejected --note "verification evidence"` to
record the host's decision. Default acceptance is pending. Missing expected
files or an empty result cannot be accepted. For a rejected deliverable,
submit a focused correction with `--retry-of ID` and the relevant evidence;
maximum two correction rounds by default. Do not retry login/config failures
until the configuration changes. No automatic session replay occurs.

The host integrates accepted Git changes and resolves conflicts. Runner does
not merge, push, or delete unintegrated results. `changes.patch` covers tracked
changes; untracked files are listed separately and remain in the worktree.
After integration, `cleanup ID` refuses dirty or unmerged worktrees.

## Permissions and compatibility

Use existing session authorization and host execution policy. Do not request
blanket escalation or bypass all permissions to get a nested CLI running.
Cursor plan mode and sandbox are distinct controls; read-only uses both plus
workspace trust. Other backends retain their own permission mappings.

For synchronous calls from Codex, read [codex.md](references/codex.md). The
synchronous JSON status is `success`, `partial`, or `error`; partial output and
metadata are evidence to inspect, never automatic success.
