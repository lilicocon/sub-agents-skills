# Install and configure

The **skill/plugin** teaches the host how to invoke the runner and supplies its
Python scripts. The **backend CLI** (Grok, Cursor, Codex, etc.) runs each task.
Installing the skill does not install those CLIs, log them in, or select their
models. Python 3.9+ and Git on PATH are required. Git is used to detect the
workspace type even when a task uses a non-Git directory.
The runner has no third-party Python runtime dependencies.

## Install from a local checkout

Choose either the host's plugin installation or a standalone skill copy; avoid
installing both copies in the same host. Plugin installation is described in the
repository README. To install the checked-out code as a standalone skill:

```sh
bash install.sh --target "$HOME/.codex/skills" --skill sub-agents
# For Cursor, use --target "$HOME/.cursor/skills" instead.
```

The script stages all skill copies before replacing an existing skill, restores
the previous copy if publication fails, and rejects source-directory overwrites
and skill-name traversal. It replaces the selected skill directory, so keep
personal settings and roles outside that directory. It does not alter external
role directories or task state. It does not register a plugin marketplace.

Before an upgrade, finish or cancel active tasks, inspect `list`, then run
`shutdown` using the old installation for every state directory you use. Install the update, and reload/restart the
host as required. Existing Python workers may otherwise keep importing from the
replaced path. This applies to both standalone and plugin updates.

## Check the installation

For the standalone installation above:

```sh
RUNNER_SKILL_DIR="$HOME/.codex/skills/sub-agents"
python3 "$RUNNER_SKILL_DIR/scripts/tasks.py" doctor
python3 "$RUNNER_SKILL_DIR/scripts/tasks.py" agents --cwd "$PWD"
python3 "$RUNNER_SKILL_DIR/scripts/tasks.py" configure --max-parallel 2
```

For a plugin, use the actual skill directory provided by the host instead of the
standalone path; do not hard-code a versioned plugin cache path in your settings.
`doctor` checks CLI availability and supported flags without a model request; it
does **not** verify login. Install and log into whichever backend your role uses,
following that backend's instructions. Credentials remain in its normal login
store or supported environment variables. Do not put secrets in role files.

Built-in `researcher` and `reviewer` use Grok with read-only permissions;
`implementer` uses Cursor with safe-edit permissions. You can override each by
creating a file with the same role name, or create entirely new names.

## Custom agents

An agent is a Markdown role definition, not a separate service or account.
For one project, create `.agents/auditor.md` under its working directory. For a
personal role shared by projects, use `~/.codex/worker-agents/auditor.md`:

```markdown
---
run-agent: grok
permission: read-only
---
# Auditor
Review the requested code. Do not edit files or delegate work.
Report reproducible findings with file names, line numbers and test evidence.
```

Set `run-agent` to your installed backend, for example `cursor-agent` or `codex`.
For implementation roles explicitly use `permission: safe-edit`. The historical
default when permission is omitted is `safe-edit`, so specify `read-only` for
review roles. Permissions depend on backend enforcement and are not a universal
security boundary. `model` and `effort` are optional frontmatter fields; omitting
them preserves backend defaults. Supported values depend on the backend/model;
Cursor and Gemini do not accept `effort`.

Managed `tasks.py` resolves **each role name** in this order:

1. Explicit `--agents-dir PATH`, when supplied: use only this directory.
2. Otherwise `SUB_AGENTS_DIR`, when set: use only this directory.
3. Otherwise project `.agents/`, then `~/.codex/worker-agents/`, then bundled `defaults/`.

An explicit directory or environment override disables fallback. An invalid
higher-priority role is an error, not a reason to use a lower-priority role.
`agents --cwd /absolute/project` shows the effective roles and source directories.
These fallback rules describe managed tasks; the legacy synchronous
`run_subagent.py` defaults to the project's `.agents/` only.

Submit and inspect a custom role:

```sh
python3 "$RUNNER_SKILL_DIR/scripts/tasks.py" submit \
  --agent auditor --cwd "$PWD" --prompt "Review the parser and report evidence; do not edit."
# Replace TASK_ID with the identifier returned by submit.
python3 "$RUNNER_SKILL_DIR/scripts/tasks.py" status TASK_ID
python3 "$RUNNER_SKILL_DIR/scripts/tasks.py" result TASK_ID
```

Submit snapshots the role, prompt and environment. Editing a role affects future
submissions, not queued or running tasks. `completed` is a protocol result; review
the result and output checks before accepting it.

## Keep configuration across updates

| What | Location | Upgrade behavior |
| --- | --- | --- |
| Project roles | `/absolute/project/.agents/` | Outside installed skill; retained |
| Personal roles | `~/.codex/worker-agents/` | Outside plugin cache; retained |
| Explicit role directory | `--agents-dir` or `SUB_AGENTS_DIR` | Keep outside installed skill |
| Capacity, task records, results | `~/.sub-agents/` by default | Retained; contains private task data |
| Backend login/model defaults | Backend's own configuration | Not managed by this installer |
| Bundled definitions/scripts | Installed skill or plugin cache | Replaced by upgrades |

To separate task histories, set `SUB_AGENTS_STATE_DIR` or place `--state-dir PATH`
**before** the command. Capacity is per state directory:

```sh
python3 "$RUNNER_SKILL_DIR/scripts/tasks.py" --state-dir "$HOME/.sub-agents-work" configure --max-parallel 2
```

Use local storage for state and restrict its access: submit environment snapshots
can contain secrets. Never commit task state into the project.
