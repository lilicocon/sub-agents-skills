"""Task lifecycle and real Git isolation, without model API calls."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from _builder import AgentInvocation
from _executor import AgentResponse, ExecutionOptions
from _loader import resolve_agent
from _scheduler import tick, work
from _state import FileLock, Store
from _workspace import git
from tasks import cleanup, dispatch, parser, submit


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state")


def enqueue(store: Store, cwd: Path, *extra: str) -> str:
    cwd.mkdir(parents=True, exist_ok=True)
    args = parser().parse_args(
        [
            "submit",
            "--agent",
            "researcher",
            "--cwd",
            str(cwd),
            "--prompt",
            "Return evidence",
            *extra,
        ]
    )
    with patch("tasks.ensure_supervisor"):
        return str(submit(store, args)["id"])


def repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    (path / "hello.py").write_text("answer = 1\n")
    git(path, "add", "hello.py")
    git(path, "commit", "-m", "baseline")
    return path


def test_default_roles_and_explicit_no_fallback(tmp_path: Path) -> None:
    assert resolve_agent("implementer", None, str(tmp_path)).run_agent == "cursor-agent"
    with pytest.raises(FileNotFoundError):
        resolve_agent("implementer", str(tmp_path), str(tmp_path))
    definitions = tmp_path / ".agents"
    definitions.mkdir()
    (definitions / "implementer.md").write_text("---\nrun-agent: grok\n---\ncustom")
    assert resolve_agent("implementer", None, str(tmp_path)).run_agent == "grok"


def test_config_and_schema(store: Store) -> None:
    assert store.parallelism() == 2
    store.configure(3)
    assert Store(store.root).parallelism() == 3
    with pytest.raises(ValueError, match="between"):
        store.configure(0)
    with store.connect() as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError, match="schema"):
        Store(store.root)


def test_file_lock_excludes_second_owner(tmp_path: Path) -> None:
    first, second = FileLock(tmp_path / "lock"), FileLock(tmp_path / "lock")
    assert first.acquire()
    assert not second.acquire()
    first.close()
    assert second.acquire()
    second.close()


def test_queue_respects_capacity(store: Store, tmp_path: Path) -> None:
    ids = [enqueue(store, tmp_path / str(i)) for i in range(3)]
    with patch("_scheduler.detach") as spawn:
        tick(store)
    assert spawn.call_count == 2
    assert [store.get(i)["state"] for i in ids] == ["running", "running", "queued"]


def test_overlapping_non_git_tasks_serialize(store: Store, tmp_path: Path) -> None:
    first = enqueue(store, tmp_path / "project")
    second = enqueue(store, tmp_path / "project" / "nested")
    with patch("_scheduler.detach"):
        tick(store)
    assert store.get(first)["state"] == "running"
    assert store.get(second)["state"] == "queued"


def test_dependency_wait_failure_and_cancel(store: Store, tmp_path: Path) -> None:
    first = enqueue(store, tmp_path / "one")
    second = enqueue(store, tmp_path / "two", "--depends-on", first)
    with patch("_scheduler.detach"):
        tick(store)
    assert store.get(second)["state"] == "queued"
    store.finish(first, "failed")
    tick(store)
    assert store.get(second)["state"] == "blocked"
    third = enqueue(store, tmp_path / "three")
    store.update(third, cancel_requested=True)
    tick(store)
    assert store.get(third)["state"] == "cancelled"


def test_restart_keeps_live_worker_and_marks_lost_worker(store: Store, tmp_path: Path) -> None:
    alive = enqueue(store, tmp_path / "one")
    lost = enqueue(store, tmp_path / "two")
    for task_id in (alive, lost):
        store.update(task_id, state="running", started=time.time() - 20)
    lock = FileLock(store.root / alive / "worker.lock")
    assert lock.acquire()
    try:
        tick(store)
        assert store.get(alive)["state"] == "running"
        assert store.get(lost)["state"] == "interrupted"
    finally:
        lock.close()


def test_git_writers_get_distinct_worktrees(store: Store, tmp_path: Path) -> None:
    project = repo(tmp_path / "project")
    ids = [
        enqueue(store, project, "--agent", "implementer", "--expect", "result.txt")
        for _ in range(2)
    ]

    def implement(
        inv: AgentInvocation, timeout_ms: int, options: ExecutionOptions
    ) -> AgentResponse:
        (Path(inv.cwd) / "result.txt").write_text("actual change")
        return {
            "result": "Implemented; test command: python ...",
            "exit_code": 0,
            "status": "success",
            "cli": inv.cli,
        }

    with patch("_scheduler.detach"):
        tick(store)
    with patch("_scheduler.execute_agent", side_effect=implement):
        for task_id in ids:
            work(store.root, task_id)
    assert not (project / "result.txt").exists()
    assert store.get(ids[0])["working_directory"] != store.get(ids[1])["working_directory"]
    for task_id in ids:
        task = store.get(task_id)
        assert task["state"] == "completed"
        assert task["acceptance"] == "pending"
        with pytest.raises(ValueError, match="uncommitted"):
            cleanup(store, parser().parse_args(["cleanup", task_id]))
        git(project, "worktree", "remove", "--force", str(store.root / task_id / "worktree"))


def test_dirty_git_baseline_is_not_silently_dropped(store: Store, tmp_path: Path) -> None:
    project = repo(tmp_path / "project")
    (project / "hello.py").write_text("changed")
    with pytest.raises(ValueError, match="uncommitted"):
        enqueue(store, project, "--agent", "implementer")
    assert (project / "hello.py").read_text() == "changed"


def test_missing_deliverable_cannot_be_accepted(store: Store, tmp_path: Path) -> None:
    task_id = enqueue(store, tmp_path / "project", "--expect", "missing.txt")
    store.update(task_id, state="running", started=time.time())
    response = {"result": "I will do the work", "exit_code": 0, "status": "success", "cli": "grok"}
    with patch("_scheduler.execute_agent", return_value=response):
        work(store.root, task_id)
    args = parser().parse_args(
        [
            "--state-dir",
            str(store.root),
            "accept",
            task_id,
            "--verdict",
            "accepted",
            "--note",
            "checked",
        ]
    )
    with pytest.raises(ValueError, match="expected files"):
        dispatch(args)
    result = json.loads((store.root / task_id / "result.json").read_text())
    assert result["response"]["result"] == "I will do the work"


def test_logs_offsets_and_result_truncation(store: Store, tmp_path: Path) -> None:
    task_id = enqueue(store, tmp_path / "project")
    (store.root / task_id / "stdout.log").write_text("abcdef")
    args = parser().parse_args(["--state-dir", str(store.root), "logs", task_id, "--limit", "3"])
    assert dispatch(args)["next_offset"] == 3
    args.offset = 3
    assert dispatch(args)["text"] == "def"
    store.update(task_id, response={"result": "abcdef"})
    result = dispatch(
        parser().parse_args(["--state-dir", str(store.root), "result", task_id, "--limit", "3"])
    )
    assert result["truncated"] is True


def test_cancelled_worker_does_not_start_cli(store: Store, tmp_path: Path) -> None:
    task_id = enqueue(store, tmp_path / "project")
    store.update(task_id, state="running", started=time.time(), cancel_requested=True)
    with patch("_scheduler.execute_agent") as run:
        work(store.root, task_id)
    run.assert_not_called()
    assert store.get(task_id)["state"] == "cancelled"


def test_reconcile_does_not_overwrite_completed_task(store: Store) -> None:
    store.add({"id": "race", "state": "running", "created": 0, "started": time.time() - 20})

    def finish_then_unlocked(path: Path) -> bool:
        store.finish("race", "completed", response={"result": "done"})
        return False

    with patch("_scheduler.locked", side_effect=finish_then_unlocked):
        tick(store)
    task = store.get("race")
    assert task["state"] == "completed"
    assert task["response"] == {"result": "done"}


def test_broken_git_write_does_not_fall_back_to_source(store: Store, tmp_path: Path) -> None:
    project = repo(tmp_path / "project")
    with (project / ".git" / "config").open("a", encoding="utf-8") as handle:
        handle.write("\n[broken\n")
    with pytest.raises(ValueError, match="config"):
        enqueue(store, project, "--agent", "implementer")


def test_exported_patch_preserves_crlf(tmp_path: Path) -> None:
    from _workspace import evidence

    project = repo(tmp_path / "project")
    git(project, "config", "core.autocrlf", "false")
    (project / "hello.py").write_bytes(b"old\r\n")
    git(project, "add", "hello.py")
    git(project, "commit", "-m", "crlf")
    base = git(project, "rev-parse", "HEAD")
    worktree = tmp_path / "worktree"
    git(project, "worktree", "add", "--detach", str(worktree), base)
    (worktree / "hello.py").write_bytes(b"new\r\n")
    task_dir = tmp_path / "artifacts"
    task_dir.mkdir()
    result = evidence({"repository": str(project), "base_commit": base}, worktree, task_dir)
    assert b"\r" in Path(str(result["patch"])).read_bytes()
    git(project, "apply", "--check", str(result["patch"]))
    git(project, "worktree", "remove", "--force", str(worktree))


def test_cleanup_keeps_worktree_used_by_another_task(store: Store, tmp_path: Path) -> None:
    project = repo(tmp_path / "project")
    writer = enqueue(store, project, "--agent", "implementer")
    worktree = store.root / writer / "worktree"
    git(project, "worktree", "add", "-b", f"runner/{writer}", str(worktree), "HEAD")
    store.update(writer, state="completed", repository=str(project), branch=f"runner/{writer}")
    enqueue(store, worktree, "--agent", "researcher")
    with pytest.raises(ValueError, match="still in use"):
        cleanup(store, parser().parse_args(["cleanup", writer]))
    assert worktree.exists()
    git(project, "worktree", "remove", "--force", str(worktree))


def test_worker_uses_submitter_environment(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RUNNER_REVIEW_MARKER", "from-submit")
    task_id = enqueue(store, tmp_path / "project")
    environment = store.get(task_id)["environment"]
    assert isinstance(environment, dict)
    assert environment["RUNNER_REVIEW_MARKER"] == "from-submit"
    captured: dict[str, object] = {}

    def fake_execute(
        inv: AgentInvocation, timeout_ms: int, options: ExecutionOptions
    ) -> AgentResponse:
        captured["env"] = options.environment
        return {"result": "ok", "exit_code": 0, "status": "success", "cli": inv.cli}

    store.update(task_id, state="running", started=time.time())
    with patch("_scheduler.execute_agent", side_effect=fake_execute):
        work(store.root, task_id)
    assert captured["env"] == environment


def test_exported_patch_preserves_trailing_whitespace(tmp_path: Path) -> None:
    from _workspace import evidence

    project = repo(tmp_path / "project")
    base = git(project, "rev-parse", "HEAD")
    worktree = tmp_path / "worktree"
    git(project, "worktree", "add", "--detach", str(worktree), base)
    (worktree / "hello.py").write_text("answer = 2  \n")
    task_dir = tmp_path / "artifacts"
    task_dir.mkdir()
    result = evidence({"repository": str(project), "base_commit": base}, worktree, task_dir)
    git(project, "apply", str(result["patch"]))
    assert (project / "hello.py").read_text() == "answer = 2  \n"
    git(project, "worktree", "remove", "--force", str(worktree))
