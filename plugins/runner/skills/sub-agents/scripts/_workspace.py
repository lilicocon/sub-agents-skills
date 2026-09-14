"""Git snapshot/worktree handling. Never merges or stages user files."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from _state import FileLock


def git_bytes(cwd: Path, *args: str) -> bytes:
    # S603: git subcommands are internal constants and paths are separate argv.
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip() or "git command failed")
    return result.stdout


def git(cwd: Path, *args: str) -> str:
    return git_bytes(cwd, *args).decode("utf-8", "replace").strip()


def snapshot(cwd: Path, *, write: bool) -> dict[str, object]:
    try:
        root = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
    except FileNotFoundError as exc:
        raise ValueError(
            "Git is required to determine workspace isolation; install Git and check PATH"
        ) from exc
    except ValueError as exc:
        if "not a git repository" in str(exc).lower():
            return {"repository": None, "base_commit": None, "relative_cwd": "."}
        raise
    base = git(root, "rev-parse", "HEAD")
    if write and git(root, "status", "--porcelain"):
        raise ValueError(
            "Git workspace has uncommitted changes. Commit the intended baseline or choose a clean checkout; nothing was stashed or copied."
        )
    return {
        "repository": str(root),
        "base_commit": base,
        "relative_cwd": str(cwd.relative_to(root)),
    }


def prepare(task: dict[str, object], task_dir: Path) -> Path:
    root = task.get("repository")
    if root and task["permission"] != "read-only":
        worktree = task_dir / "worktree"
        git(
            Path(str(root)),
            "worktree",
            "add",
            "-b",
            str(task["branch"]),
            str(worktree),
            str(task["base_commit"]),
        )
        return worktree / str(task["relative_cwd"])
    return Path(str(task["cwd"]))


def evidence(task: dict[str, object], cwd: Path, task_dir: Path) -> dict[str, object]:
    if not task.get("repository"):
        return {"working_directory": str(cwd)}
    root = Path(git(cwd, "rev-parse", "--show-toplevel"))
    base = str(task["base_commit"])
    patch = git_bytes(root, "diff", "--binary", base)
    (task_dir / "changes.patch").write_bytes(patch)
    return {
        "working_directory": str(cwd),
        "base_commit": base,
        "branch": task.get("branch"),
        "patch": str(task_dir / "changes.patch"),
        "changed_files": git(root, "diff", "--name-only", base).splitlines(),
        "untracked_files": git(root, "ls-files", "--others", "--exclude-standard").splitlines(),
        "git_status": git(root, "status", "--porcelain"),
    }


def overlaps(left: str, right: str) -> bool:
    a, b = Path(left).resolve(), Path(right).resolve()
    return a == b or a in b.parents or b in a.parents


@contextmanager
def lifecycle_lock(cwd: Path, fallback: Path) -> Iterator[Path]:
    """Serialize submit/cleanup across state dirs sharing a Git common directory.

    Lock discovery can race deletion, so callers must validate cwd again inside
    the lock. The lock and registry live outside all linked worktrees.
    """
    try:
        common = Path(git(cwd, "rev-parse", "--git-common-dir"))
        directory = (cwd / common).resolve()
    except FileNotFoundError as exc:
        raise ValueError(
            "Git is required to determine workspace isolation; install Git and check PATH"
        ) from exc
    except ValueError as exc:
        if "not a git repository" not in str(exc).lower():
            raise
        directory = fallback
    lock = FileLock(directory / "runner-workspace.lock")
    deadline = time.monotonic() + 30
    while not lock.acquire():
        if time.monotonic() >= deadline:
            raise ValueError("Workspace lifecycle is busy; retry shortly")
        time.sleep(0.02)
    try:
        yield directory
    finally:
        lock.close()


def registered_stores(directory: Path, current: Path) -> list[Path]:
    """Read state roots while holding lifecycle_lock; stale missing roots are ignored."""
    registry = directory / "runner-state-dirs.json"
    roots: object = json.loads(registry.read_text(encoding="utf-8")) if registry.exists() else []
    if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
        raise ValueError("Invalid workspace task-store registry; retaining workspace")
    return sorted({current.resolve(), *(Path(root) for root in roots)})


def register_store(directory: Path, current: Path) -> None:
    roots = registered_stores(directory, current)
    registry = directory / "runner-state-dirs.json"
    temporary = directory / "runner-state-dirs.json.tmp"
    temporary.write_text(json.dumps([str(root) for root in roots]), encoding="utf-8")
    temporary.replace(registry)
