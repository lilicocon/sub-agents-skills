"""Detached scheduler and independent task workers; SQLite is the mailbox."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

from _builder import AgentInvocation
from _executor import ExecutionOptions, execute_agent
from _state import FileLock, Store, locked
from _workspace import evidence, overlaps, prepare

# Keep references until reaped (avoids zombies in long-lived hosts).
_CHILDREN: list[subprocess.Popen[bytes]] = []


def detach(root: Path, *args: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "supervisor.log").open("ab") as log:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        # S603: only the bundled entrypoint and internal command names are used.
        child = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-B",
                str(Path(__file__).with_name("tasks.py")),
                "--state-dir",
                str(root),
                *args,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=sys.platform != "win32",
            creationflags=flags,
        )
        _CHILDREN.append(child)
    _CHILDREN[:] = [child for child in _CHILDREN if child.poll() is None]


def ensure_supervisor(root: Path) -> None:
    if (root / "shutdown").exists():
        deadline = time.monotonic() + 5
        while locked(root / "supervisor.lock") and time.monotonic() < deadline:
            time.sleep(0.05)
        (root / "shutdown").unlink(missing_ok=True)
    if not locked(root / "supervisor.lock"):
        detach(root, "_supervise")


def _conflicts(task: dict[str, object], running: list[dict[str, object]]) -> bool:
    # Git writers have their own worktrees. Non-Git work serializes with ANY
    # overlapping task directory; no assumptions about read-only shell tools.
    return any(
        (not task.get("repository") or not other.get("repository"))
        and overlaps(str(task["cwd"]), str(other["cwd"]))
        for other in running
    )


def tick(store: Store) -> bool:
    _CHILDREN[:] = [child for child in _CHILDREN if child.poll() is None]
    now = time.time()
    tasks = store.active()
    for task in tasks:
        task_id = str(task["id"])
        if (
            task["state"] == "running"
            and now - float(cast(float, task["started"])) > 5
            and not locked(store.root / task_id / "worker.lock")
        ):
            store.finish(
                task_id,
                "interrupted",
                error="Worker exited without recording an outcome; inspect artifacts before retrying",
            )
    tasks = store.active()
    dependency_states = store.dependency_states(
        [dep for task in tasks for dep in cast(list[str], task.get("dependencies", []))]
    )
    parallelism = store.parallelism()
    running = [task for task in tasks if task["state"] == "running"]
    for task in tasks:
        if task["state"] != "queued":
            continue
        task_id = str(task["id"])
        if task.get("cancel_requested"):
            store.finish(task_id, "cancelled")
            continue
        deps = [dependency_states[dep] for dep in cast(list[str], task["dependencies"])]
        if any(
            dep in {"failed", "cancelled", "interrupted", "blocked", "timed_out"} for dep in deps
        ):
            store.finish(task_id, "blocked", error="A dependency did not complete")
            continue
        if any(dep != "completed" for dep in deps):
            continue
        if len(running) >= parallelism or _conflicts(task, running):
            continue
        task = store.update(task_id, state="running", started=time.time())
        try:
            detach(store.root, "_work", task_id)
            running.append(task)
        except OSError as exc:
            store.finish(task_id, "failed", error=str(exc))
    return store.has_active()


def supervise(root: Path) -> None:
    lock = FileLock(root / "supervisor.lock")
    if not lock.acquire():
        return
    try:
        store = Store(root)
        while not (root / "shutdown").exists():
            tick(store)
            time.sleep(0.25)
    finally:
        lock.close()


def work(root: Path, task_id: str) -> None:
    store = Store(root)
    task_dir = root / task_id
    lock = FileLock(task_dir / "worker.lock")
    if not lock.acquire():
        return
    try:
        task = store.get(task_id)
        if task["state"] != "running":
            return
        if task.get("cancel_requested"):
            store.finish(task_id, "cancelled")
            return
        cwd = prepare(task, task_dir)
        store.update(task_id, working_directory=str(cwd))
        deps = cast(list[str], task["dependencies"])
        context = str(task["system_context"])
        context += "\nDo not delegate to other agents. Deliver a final report with evidence, not progress alone."
        prompt = str(task["prompt"])
        if deps:
            prompt += "\nDependency results (read these files as evidence):\n" + "\n".join(
                str(root / dep / "result.json") for dep in deps
            )
        invocation = AgentInvocation(
            cli=str(task["cli"]),
            prompt=prompt,
            cwd=str(cwd),
            system_context=context,
            agent_file=str(task_dir / "agent.md"),
            permission=str(task["permission"]),
            model=cast("str | None", task.get("model")),
            effort=cast("str | None", task.get("effort")),
        )
        # Gemini's system-file adapter needs the immutable per-task definition.
        (task_dir / "agent.md").write_text(context, encoding="utf-8")
        raw_env = task.get("environment")
        response = execute_agent(
            invocation,
            timeout_ms=int(cast(int, task["timeout_ms"])),
            options=ExecutionOptions(
                cancelled=lambda: bool(store.get(task_id).get("cancel_requested")),
                log_dir=task_dir,
                environment=(
                    {
                        key: value
                        for key, value in raw_env.items()
                        if isinstance(key, str) and isinstance(value, str)
                    }
                    if isinstance(raw_env, dict)
                    else None
                ),
            ),
        )
        expected = cast(list[str], task["expected_files"])
        missing = [name for name in expected if not (cwd / name).is_file()]
        text = response.get("result")
        has_body = isinstance(text, str) and bool(text.strip())
        artifacts = evidence(task, cwd, task_dir)
        (task_dir / "result.json").write_text(
            json.dumps(
                {"response": response, "artifacts": artifacts}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        state = "completed" if response["status"] == "success" else "failed"
        if response["exit_code"] == 124:
            state = "timed_out"
        if response["exit_code"] == 130 or store.get(task_id).get("cancel_requested"):
            state = "cancelled"
        store.finish(
            task_id,
            state,
            response=response,
            artifacts=artifacts,
            output_check={
                "has_body": has_body,
                "missing_files": missing,
                "status": "ready_for_review" if has_body and not missing else "needs_attention",
            },
            acceptance="pending",
        )
    except Exception as exc:
        store.finish(task_id, "failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        lock.close()
