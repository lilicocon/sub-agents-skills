#!/usr/bin/env python3
"""Persistent external-agent tasks. All public commands emit one JSON object."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn, cast

from _builder import AgentInvocation, build_invocation_args
from _loader import discover_agents, resolve_agent
from _scheduler import ensure_supervisor, supervise, work
from _state import TERMINAL, FileLock, Store, state_root
from _workspace import git, overlaps, snapshot


def doctor() -> dict[str, object]:
    backends: dict[str, object] = {}
    for cli in ("cursor-agent", "grok"):
        binary = shutil.which(cli)
        info: dict[str, object] = {
            "path": binary,
            "available": bool(binary),
            "authentication": "not_checked",
        }
        if binary:
            try:
                # S603: executable is a known backend; these are read-only flags.
                version = subprocess.run(  # noqa: S603
                    [binary, "--version"], capture_output=True, text=True, timeout=15, check=False
                )
                help_text = subprocess.run(  # noqa: S603
                    [binary, "--help"], capture_output=True, text=True, timeout=15, check=False
                )
                required = (
                    ["--output-format", "--sandbox", "--mode", "--trust"]
                    if cli == "cursor-agent"
                    else [
                        "--output-format",
                        "--sandbox",
                        "--verbatim",
                        "--permission-mode",
                        "--cwd",
                    ]
                )
                info.update(
                    version=version.stdout.strip(),
                    missing_flags=[flag for flag in required if flag not in help_text.stdout],
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                info["error"] = str(exc)
        backends[cli] = info
    return {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "backends": backends,
        "note": "No model invocation or credential-file reads. Run an explicit read-only task to verify authentication.",
    }


def submit(store: Store, args: argparse.Namespace) -> dict[str, object]:
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError("cwd must be an existing directory")
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    agent = resolve_agent(args.agent, args.agents_dir, str(cwd))
    backend = args.cli or agent.run_agent
    if not backend:
        raise ValueError("Set run-agent or --cli")
    prompt = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    if not prompt or not prompt.strip():
        raise ValueError("Task prompt must not be empty")
    for dep in args.depends_on:
        store.get(dep)
    if args.retry_of:
        store.get(args.retry_of)
    for name in args.expect:
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("Expected files must be relative paths without '..'")
    # Validate backend/model/effort combinations before enqueueing.
    build_invocation_args(
        AgentInvocation(
            cli=backend,
            prompt=prompt,
            cwd=str(cwd),
            permission=agent.permission,
            model=agent.model,
            effort=agent.effort,
        )
    )
    task_id = uuid.uuid4().hex
    task: dict[str, object] = {
        **asdict(agent),
        **snapshot(cwd, write=agent.permission != "read-only"),
        "id": task_id,
        "state": "queued",
        "created": time.time(),
        "cwd": str(cwd),
        "cli": backend,
        "prompt": prompt,
        "role": args.agent,
        "timeout_ms": args.timeout,
        "dependencies": args.depends_on,
        "retry_of": args.retry_of,
        "expected_files": args.expect,
        "cancel_requested": False,
        "acceptance": "pending",
        "branch": "runner/" + task_id if agent.permission != "read-only" else None,
        "environment": dict(os.environ),
    }
    (store.root / task_id).mkdir(mode=0o700)
    (store.root / task_id / "request.json").write_text(
        json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    store.add(task)
    ensure_supervisor(store.root)
    return {"id": task_id, "state": "queued", "state_dir": str(store.root)}


def summary(task: dict[str, object]) -> dict[str, object]:
    fields = (
        "id",
        "state",
        "role",
        "cli",
        "created",
        "started",
        "finished",
        "cwd",
        "working_directory",
        "branch",
        "base_commit",
        "cancel_requested",
        "acceptance",
        "acceptance_note",
        "output_check",
        "error",
        "cleaned",
    )
    result = {key: task[key] for key in fields if key in task}
    if "started" in task:
        end = float(cast(float, task.get("finished", time.time())))
        result["duration_seconds"] = round(end - float(cast(float, task["started"])), 2)
    return result


def task_result(store: Store, task_id: str, limit: int) -> dict[str, object]:
    task = store.get(task_id)
    result = summary(task)
    response = dict(cast(dict[str, object], task.get("response", {})))
    body = response.get("result")
    if isinstance(body, str):
        response["result"] = body[:limit]
        result["truncated"] = len(body) > limit
    result.update(
        response=response,
        artifacts=task.get("artifacts", {}),
        full_result=(
            str(store.root / task_id / "result.json")
            if (store.root / task_id / "result.json").is_file()
            else None
        ),
    )
    return result


def worktree_holder(store: Store, task_id: str, worktree: Path) -> str | None:
    for other in store.all():
        if other["id"] == task_id or other.get("cleaned"):
            continue
        for key in ("working_directory", "cwd"):
            value = other.get(key)
            if isinstance(value, str) and overlaps(value, str(worktree)):
                return str(other["id"])
    return None


def cleanup(store: Store, args: argparse.Namespace) -> dict[str, object]:
    task = store.get(args.id)
    if task["state"] not in TERMINAL:
        raise ValueError("Only terminal tasks can be cleaned up")
    lock = FileLock(store.root / args.id / "worker.lock")
    if not lock.acquire():
        raise ValueError("Worker is still cleaning up; retry shortly")
    try:
        directory = store.root / args.id
        worktree = directory / "worktree"
        # Never force-remove dirty/unmerged results. Codex integrates first.
        if worktree.exists():
            holder = worktree_holder(store, args.id, worktree)
            if holder:
                raise ValueError(f"Worktree is still in use by task {holder}; retaining it")
            root = Path(str(task["repository"]))
            if git(worktree, "status", "--porcelain"):
                raise ValueError("Worktree has uncommitted results; integrate them before cleanup")
            merged = git(root, "branch", "--merged", "HEAD", "--list", str(task["branch"]))
            if not merged:
                raise ValueError("Task branch is not merged into source HEAD; retaining worktree")
            git(root, "worktree", "remove", str(worktree))
            git(root, "branch", "-d", str(task["branch"]))
        for name in ("stdout.log", "stderr.log", "changes.patch"):
            (directory / name).unlink(missing_ok=True)
        store.update(args.id, cleaned=True)
        return {"id": args.id, "cleaned": True, "retained": "request, result, task record"}
    finally:
        lock.close()


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def parser() -> argparse.ArgumentParser:
    p = JsonArgumentParser(description=__doc__)
    p.add_argument("--state-dir", help="Local state directory (default ~/.sub-agents)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    agents = sub.add_parser("agents")
    agents.add_argument("--cwd", default=os.getcwd())
    agents.add_argument("--agents-dir")
    add = sub.add_parser("submit")
    add.add_argument("--agent", required=True)
    add.add_argument("--cwd", required=True)
    add.add_argument("--agents-dir")
    add.add_argument("--cli")
    prompts = add.add_mutually_exclusive_group(required=True)
    prompts.add_argument("--prompt")
    prompts.add_argument("--prompt-file")
    add.add_argument("--timeout", type=int, default=600000)
    add.add_argument("--depends-on", action="append", default=[])
    add.add_argument("--expect", action="append", default=[])
    add.add_argument("--retry-of")
    sub.add_parser("list")
    sub.add_parser("shutdown")
    for name in ("status", "result", "logs", "cancel", "cleanup", "accept", "_work"):
        cmd = sub.add_parser(name)
        cmd.add_argument("id")
        if name == "result":
            cmd.add_argument("--limit", type=int, default=6000)
        if name == "logs":
            cmd.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
            cmd.add_argument(
                "--offset", type=int, default=0, help="Byte offset returned by previous call"
            )
            cmd.add_argument("--limit", type=int, default=16384)
        if name == "accept":
            cmd.add_argument("--verdict", choices=("accepted", "rejected"), required=True)
            cmd.add_argument("--note", required=True, help="Codex verification evidence")
    configure = sub.add_parser("configure")
    configure.add_argument("--max-parallel", type=int, required=True)
    sub.add_parser("_supervise")
    return p


def dispatch(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "doctor":
        return doctor()
    if args.command == "agents":
        return {"agents": discover_agents(args.agents_dir, args.cwd)}
    root = state_root(args.state_dir)
    if args.command == "_supervise":
        supervise(root)
        return {}
    if args.command == "_work":
        work(root, args.id)
        return {}
    store = Store(root)
    if args.command == "shutdown":
        if any(task["state"] in {"queued", "running"} for task in store.all()):
            raise ValueError("Cancel or finish active tasks before shutdown")
        (root / "shutdown").touch()
        return {"shutdown_requested": True}
    if args.command == "submit":
        return submit(store, args)
    if args.command == "configure":
        store.configure(args.max_parallel)
        ensure_supervisor(root)
        return {"max_parallel": args.max_parallel}
    if args.command == "list":
        ensure_supervisor(root)
        return {"tasks": [summary(task) for task in store.all()]}
    task = store.get(args.id)  # validate ID before constructing any task paths
    if args.command == "status":
        ensure_supervisor(root)
        info = summary(task)
        logfiles = [root / args.id / f"{stream}.log" for stream in ("stdout", "stderr")]
        info["last_activity"] = max(
            (file.stat().st_mtime for file in logfiles if file.exists()), default=None
        )
        return info
    if args.command == "result":
        if args.limit < 1:
            raise ValueError("limit must be positive")
        return task_result(store, args.id, min(args.limit, 1000000))
    if args.command == "cancel":
        if task["state"] not in TERMINAL:
            store.update(args.id, cancel_requested=True)
            ensure_supervisor(root)
        return summary(store.get(args.id))
    if args.command == "accept":
        check = cast(dict[str, object], task.get("output_check", {}))
        if args.verdict == "accepted" and (
            task["state"] != "completed" or check.get("status") != "ready_for_review"
        ):
            raise ValueError(
                "Acceptance requires a completed task with nonempty output and all expected files"
            )
        if not args.note.strip():
            raise ValueError("Provide verification evidence")
        return summary(store.update(args.id, acceptance=args.verdict, acceptance_note=args.note))
    if args.command == "cleanup":
        return cleanup(store, args)
    if args.command == "logs":
        if args.offset < 0 or args.limit < 1:
            raise ValueError("offset must be nonnegative and limit positive")
        path = root / args.id / (args.stream + ".log")
        data = b""
        if path.exists():
            with path.open("rb") as log:
                log.seek(args.offset)
                data = log.read(min(args.limit, 1000000))
        return {
            "id": args.id,
            "text": data.decode("utf-8", errors="replace"),
            "next_offset": args.offset + len(data),
        }
    raise ValueError("Unknown command")


def main() -> None:
    try:
        result = dispatch(parser().parse_args())
        print(json.dumps(result, ensure_ascii=False))
    except (OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
