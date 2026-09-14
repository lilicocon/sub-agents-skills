"""Cross-platform detached supervisor -> worker -> CLI protocol smoke test."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

import pytest

ENTRY = Path(__file__).resolve().parents[1] / "skills/sub-agents/scripts/tasks.py"


def invoke(root: Path, *args: str) -> dict[str, object]:
    # S603: bundled CLI under test, fixed fixture inputs.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-B", str(ENTRY), "--state-dir", str(root), *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    return cast(dict[str, object], json.loads(result.stdout))


def test_detached_task_completion_and_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bins = tmp_path / "bin"
    bins.mkdir()
    stub = bins / "stub.py"
    stub.write_text(
        'import json\nprint(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"Verified fixture"}}))\nprint(json.dumps({"type":"turn.completed"}))\n'
    )
    if sys.platform == "win32":
        (bins / "codex.cmd").write_text(f'@"{sys.executable}" "{stub}"\r\n')
    else:
        executable = bins / "codex"
        executable.write_text(f"#!{sys.executable}\n" + stub.read_text())
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bins) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("SUB_AGENTS_DIR", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    state = tmp_path / "state"
    task = invoke(
        state,
        "submit",
        "--agent",
        "researcher",
        "--cli",
        "codex",
        "--cwd",
        str(project),
        "--prompt",
        "fixture",
        "--timeout",
        "5000",
    )
    task_id = str(task["id"])
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            task = invoke(state, "status", task_id)
            if task["state"] not in ("queued", "running"):
                break
            time.sleep(0.1)
        assert task["state"] == "completed", task
        result = invoke(state, "result", task_id)
        assert cast(dict[str, object], result["response"])["result"] == "Verified fixture"
        assert result["acceptance"] == "pending"
    finally:
        if task["state"] in ("queued", "running"):
            invoke(state, "cancel", task_id)
            time.sleep(2)
        invoke(state, "shutdown")
        from _state import locked

        deadline = time.monotonic() + 5
        while locked(state / "supervisor.lock") and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not locked(state / "supervisor.lock")
