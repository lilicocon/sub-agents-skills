# Managed tasks

Run commands with `python {SKILL_DIR}/scripts/tasks.py`. No third-party Python
runtime dependencies, web service, API gateway, or extra credentials are needed.
Use the local CLI login or its supported environment variables.

See [installation and custom-agent configuration](install-and-configure.md) for
local installation, backend login, persistent roles and upgrade instructions.

## Commands

The global `--state-dir PATH` goes **before** the command. Default state is
`~/.sub-agents`, overridable by `SUB_AGENTS_STATE_DIR`. Keep state outside the
repository and on a local filesystem (SQLite/locks are not designed for network shares).

| Command | Arguments / behavior |
| --- | --- |
| `doctor` | Versions and supported flags; no model call or auth assertion |
| `agents` | `--cwd PATH`, optional `--agents-dir PATH` |
| `submit` | `--agent ROLE --cwd PATH --prompt-file FILE` (or `--prompt TEXT`) |
| `submit` options | `--cli BACKEND`, `--timeout MS` (or `600s`/`10m`; under 1s rejected), repeated `--expect RELATIVE_FILE`, repeated `--depends-on ID`, `--retry-of ID` |
| `configure` | `--max-parallel N` (1–32, default 2, per state directory) |
| `status ID` | State, elapsed time, latest log activity, cwd and verification status |
| `list` | Task summaries; also restarts a stopped scheduler if necessary |
| `result ID` | Summary and first 6000 result characters, `--limit N`; full JSON stays on disk |
| `logs ID` | `--stream stdout|stderr --offset BYTE --limit N`, returns next offset |
| `cancel ID` | Requests cancellation of queued/running work; poll until terminal |
| `accept ID` | `--verdict accepted|rejected --note TEXT`; host supplies evidence |
| `cleanup ID` | Removes logs and clean, merged worktrees; retains request/result/task records |
| `shutdown` | Stops the idle supervisor; refuses while tasks are active |

Windows requires Python 3.9+ and Git on PATH. Use quoted absolute paths for Python
and the script when directories contain spaces. macOS/Linux use process groups
and an owner-pipe watchdog, and also signal POSIX descendants that left the
original session; Windows assigns a gated launcher to a kill-on-close
Job Object **before** it can launch a backend. Isolation failure aborts the run.

On POSIX, an inherited `SUB_AGENTS_PROCESS_OWNER` marker supplements ancestry so
cleanup can find descendants after setsid or reparenting. Keep this marker when
constructing child environments. This is lifecycle cleanup, not a security
sandbox: descendants that both clear the marker and leave the session require
OS-level containment for reliable termination.

A complete JSON result, including pretty-printed payloads, ends the run; a
lingering process after that result is cleaned up rather than reported as a
timeout.
Native executables and standard npm batch shims are supported. Batch shims are
resolved directly to their interpreter/entrypoint so prompt text never passes
through cmd.exe. Unrecognized custom batch launchers fail with a diagnostic.

## Definitions and context

Explicit `--agents-dir`, then `SUB_AGENTS_DIR`, select a directory without fallback.
Otherwise resolve each role in project `.agents/`, `~/.codex/worker-agents/`, then
the plugin's `defaults/`. A malformed higher-priority definition is an error.
Existing frontmatter (`run-agent`, `model`, `effort`, `permission`) is supported.
Omit model/effort to keep the backend's configuration.

Roles, prompts, and the submitter's environment are snapshotted when submitted.
A long-lived supervisor does not reuse the environment it started with for later
tasks. Dependencies must already exist, so cycles cannot be created. They supply
result-file locations; they do not copy changes or select another task's working
directory. Submit downstream implementation only after the host has reviewed its
required input.

## Lifecycle

`queued → running → completed | failed | timed_out | cancelled | interrupted`.
A failed/cancelled/interrupted/timed-out dependency produces `blocked`.
Timeout starts when external execution begins; queue time and Git preparation
are separate (Git operations have their own 30-second limits).

CLI completion, output checks, and host acceptance are independent:
- `output_check`: body present, expected files present, ready_for_review/needs_attention.
  On a timeout it also carries `stdout_chars`, the number of characters the
  backend actually emitted. Zero means it produced nothing before the deadline
  and a longer timeout alone is unlikely to help.
- `acceptance`: pending/accepted/rejected. The program cannot infer correctness
  from the wording of an LLM response; the host must review it.

SQLite schema version 1 stores task records and capacity. Independent workers
hold task locks; a restarted supervisor keeps live workers and marks vanished
ones interrupted after a five-second startup allowance. It never automatically
replays potentially mutating work. Request/result/log files remain on disk.
A supervisor stays resident until `shutdown`; no terminal window is required.

## Git integration

Git must be available on PATH for workspace detection, including non-Git tasks.
Git writers start from the recorded clean HEAD in a `runner/<task-id>` branch and
an independent worktree. A dirty source tree fails submission. Git errors other
than "not a git repository" fail closed; they do not fall back to writing in the
source checkout. Non-Git work on overlapping directories serializes, including
reads.

Inspect `artifacts.working_directory`, tracked `changes.patch`, `changed_files`,
and `untracked_files`. Untracked files are deliberately not staged or included
in the tracked patch. The host reviews and commits the intended changes in the
worktree, then integrates that commit into the destination. Runner never
performs those operations automatically. Cleanup requires a clean task worktree
and its branch merged into the source repository's current HEAD; it also refuses
if another task still uses that worktree. Squashed or cherry-picked histories
may need manual cleanup after inspecting equivalence.

Submit and cleanup share a lifecycle lock and a task-store registry in the Git
common directory, outside linked worktrees. This also protects shared worktrees
across state directories registered by new submissions. Existing holders in the
current state directory remain protected, but old submissions in other,
unregistered state directories cannot be discovered automatically.

## Failures and upgrades

- Authentication/workspace trust: inspect stderr and `doctor`; log in directly
  to the affected CLI. Do not put credentials in prompts or task files.
- Worker failure: inspect partial logs/artifacts before submitting `--retry-of`.
- Unexpected output format: raw logs remain available; parser failure stays an error.
- Before upgrading, finish/cancel old tasks and stop supervisors in **every**
  state directory you use, including custom `--state-dir`/`SUB_AGENTS_STATE_DIR`
  locations. Old processes do not use the new cross-state lifecycle lock and
  registry. Task state survives reinstall, but background Python processes load
  modules from their installed script path.
- All logs are local and may contain code/model output. Cleanup is explicit;
  do not share the state directory as part of a repository commit.

Workspace lifecycle locks and the task-store registry live in the Git common
directory, outside linked worktrees. New submissions register their state
directory so cleanup can check holders across registered stores. Historical
tasks in the current store are also checked; other stores used only by older
versions are not registered. Finish old tasks and stop supervisors in every
custom state directory before upgrading. Non-Git lifecycle locks are per store.
