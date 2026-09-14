# Codex host notes

Prefer `tasks.py submit` for long or parallel jobs: it returns an ID immediately,
so the host shell timeout does not have to span the whole model invocation.
The scheduler and workers run locally using the CLI's existing login/config.
Use the permissions already authorized in the session. Do not automatically
request escalation; diagnose actual launch failures first with `doctor` and logs.

For the legacy `run_subagent.py` synchronous entrypoint, keep the host command
alive for at least `--timeout` plus a small cleanup allowance (default 600000ms
plus 5000ms). Poll the same running command rather than launching duplicates.
Return the JSON payload even when its process exit code is nonzero. `partial`
contains useful evidence but is not a passed task. Quiet output is normal for
some backends.

Newly installed/updated plugin code is picked up in a new Codex thread. An
already running task uses its submitted role snapshot; finish active jobs
before upgrading or moving the installed plugin directory.
