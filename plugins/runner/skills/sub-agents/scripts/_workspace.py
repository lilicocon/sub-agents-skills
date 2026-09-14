"""Git snapshot/worktree handling. Never merges or stages user files."""

from __future__ import annotations

import subprocess
from pathlib import Path


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
    except FileNotFoundError:
        return {"repository": None, "base_commit": None, "relative_cwd": "."}
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
