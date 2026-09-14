"""Versioned local task store and cross-platform advisory locks."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, cast

TERMINAL = {"completed", "failed", "timed_out", "cancelled", "interrupted", "blocked"}


def state_root(value: str | None = None) -> Path:
    return (
        Path(value or os.environ.get("SUB_AGENTS_STATE_DIR", str(Path.home() / ".sub-agents")))
        .expanduser()
        .resolve()
    )


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.file: IO[bytes] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            return False
        return True

    def close(self) -> None:
        if self.file:
            if sys.platform == "win32":
                import msvcrt

                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


def locked(path: Path) -> bool:
    lock = FileLock(path)
    acquired = lock.acquire()
    lock.close()
    return not acquired


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported task database schema: {version}")
            db.execute(
                "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, state TEXT NOT NULL, created REAL NOT NULL, data TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO settings VALUES ('max_parallel', '2')")
            db.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.root / "tasks.sqlite3", timeout=15)
        try:
            db.execute("PRAGMA busy_timeout=15000")
            with db:
                yield db
        finally:
            db.close()

    def add(self, task: dict[str, object]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?)",
                (task["id"], task["state"], task["created"], json.dumps(task)),
            )

    def get(self, task_id: str) -> dict[str, object]:
        with self.connect() as db:
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"Unknown task: {task_id}")
        return cast(dict[str, object], json.loads(row[0]))

    def all(self) -> list[dict[str, object]]:
        with self.connect() as db:
            rows = db.execute("SELECT data FROM tasks ORDER BY created, id").fetchall()
        return [cast(dict[str, object], json.loads(row[0])) for row in rows]

    def update(self, task_id: str, **changes: object) -> dict[str, object]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown task: {task_id}")
            task = cast(dict[str, object], json.loads(row[0]))
            task.update(changes)
            db.execute(
                "UPDATE tasks SET state=?, data=? WHERE id=?",
                (task["state"], json.dumps(task), task_id),
            )
        return task

    def finish(self, task_id: str, state: str, **changes: object) -> dict[str, object]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError(f"Unknown task: {task_id}")
            task = cast(dict[str, object], json.loads(row[0]))
            if task["state"] in TERMINAL:
                return task
            task.update(changes)
            task["state"] = state
            task["finished"] = time.time()
            db.execute(
                "UPDATE tasks SET state=?, data=? WHERE id=?",
                (task["state"], json.dumps(task), task_id),
            )
        return task

    def parallelism(self) -> int:
        with self.connect() as db:
            return int(
                db.execute("SELECT value FROM settings WHERE key='max_parallel'").fetchone()[0]
            )

    def configure(self, count: int) -> None:
        if count < 1 or count > 32:
            raise ValueError("max_parallel must be between 1 and 32")
        with self.connect() as db:
            db.execute("UPDATE settings SET value=? WHERE key='max_parallel'", (str(count),))
