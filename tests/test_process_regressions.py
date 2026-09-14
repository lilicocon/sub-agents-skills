"""Real subprocess regressions: pipe pressure, signals, deadlines and ownership."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from _builder import AgentInvocation, ProcessInvocation
from _executor import ExecutionOptions, execute_agent


def run_code(
    code: str, tmp_path: Path, *, timeout: int = 3000, options: ExecutionOptions | None = None
) -> dict[str, object]:
    with patch(
        "_executor.build_invocation_args",
        return_value=ProcessInvocation(sys.executable, ["-c", code]),
    ):
        return dict(
            execute_agent(
                AgentInvocation(cli="cursor-agent", prompt="test", cwd=str(tmp_path)),
                timeout_ms=timeout,
                options=options,
            )
        )


RESULT = 'print(\'{"type":"result","result":"done","session_id":"s1"}\',flush=True)'


def test_stderr_pressure_does_not_block_stdout(tmp_path: Path) -> None:
    result = run_code(
        'import sys;sys.stderr.write("x"*1048576);sys.stderr.flush();' + RESULT, tmp_path
    )
    assert result["status"] == "success"
    assert result["result"] == "done"
    assert result["metadata"] == {"session_id": "s1"}


def test_pretty_cursor_json_and_unicode(tmp_path: Path) -> None:
    code = 'import json;print(json.dumps({"type":"result","result":"完成"},indent=2))'
    assert run_code(code, tmp_path)["result"] == "完成"


def test_zero_exit_without_protocol_is_error(tmp_path: Path) -> None:
    assert run_code('print("still thinking")', tmp_path)["status"] == "error"


def test_terminal_allows_normal_shutdown(tmp_path: Path) -> None:
    code = RESULT + ';import time;time.sleep(.1);open("finished","w").write("yes")'
    assert run_code(code, tmp_path)["status"] == "success"
    assert (tmp_path / "finished").read_text() == "yes"


def test_hanging_after_terminal_has_bounded_cleanup(tmp_path: Path) -> None:
    started = time.monotonic()
    assert run_code(RESULT + ";import time;time.sleep(60)", tmp_path)["status"] == "success"
    assert time.monotonic() - started < 4


def test_large_single_line_is_bounded(tmp_path: Path) -> None:
    with patch("_executor._MAX_LINE_CHARS", 10000):
        result = run_code('import sys;sys.stdout.write("x"*100000);sys.stdout.flush()', tmp_path)
    assert "exceeded" in str(result.get("error"))


def test_terminal_does_not_disable_output_cap(tmp_path: Path) -> None:
    with patch("_executor._MAX_STDOUT_CHARS", 10000):
        result = run_code(RESULT + ';print("x"*100000,flush=True)', tmp_path)
    assert result["status"] != "success"


def test_timeout_and_cancellation_preserve_logs(tmp_path: Path) -> None:
    started = time.monotonic()
    options = ExecutionOptions(
        cancelled=lambda: time.monotonic() - started > 0.3, log_dir=tmp_path / "logs"
    )
    result = run_code(
        'import time;print("working",flush=True);time.sleep(60)', tmp_path, options=options
    )
    assert result["exit_code"] == 130
    assert "working" in (tmp_path / "logs" / "stdout.log").read_text()
    assert time.monotonic() - started < 3


def test_timeout_stops_descendant(tmp_path: Path) -> None:
    # If a descendant escapes cancellation it will create this marker later.
    child = 'import time;time.sleep(1.5);open("escaped","w").write("bad");time.sleep(60)'
    code = f'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",{child!r}]);time.sleep(60)'
    result = run_code(code, tmp_path, timeout=400)
    assert result["exit_code"] == 124
    time.sleep(1.6)
    assert not (tmp_path / "escaped").exists()


def test_pretty_json_then_hang_is_success(tmp_path: Path) -> None:
    code = (
        'import json,time;print(json.dumps({"type":"result","result":"finished"},indent=2),'
        "flush=True);time.sleep(60)"
    )
    started = time.monotonic()
    result = run_code(code, tmp_path, timeout=3000)
    assert result["status"] == "success"
    assert result["result"] == "finished"
    assert time.monotonic() - started < 4


@pytest.mark.skipif(os.name == "nt", reason="POSIX new session")
def test_timeout_stops_new_session_descendant(tmp_path: Path) -> None:
    child = 'import time;time.sleep(1.5);open("escaped","w").write("bad");time.sleep(60)'
    code = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child!r}],start_new_session=True);"
        "time.sleep(60)"
    )
    result = run_code(code, tmp_path, timeout=400)
    assert result["exit_code"] == 124
    time.sleep(1.6)
    assert not (tmp_path / "escaped").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_external_sigterm_is_not_success(tmp_path: Path) -> None:
    result = run_code(RESULT + ";import os,signal;os.kill(os.getpid(),signal.SIGTERM)", tmp_path)
    assert result["status"] == "partial"


def test_rejects_nonpositive_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        run_code(RESULT, tmp_path, timeout=0)


def test_owner_crash_stops_descendant(tmp_path: Path) -> None:
    import subprocess

    import _process

    scripts = str(Path(_process.__file__).parent)
    descendant = 'import time;time.sleep(1.5);open("orphan","w").write("bad");time.sleep(60)'
    owner = (
        f"import sys,os,time;sys.path.insert(0,{scripts!r});from _process import launch;"
        f'tree=launch([sys.executable,"-c",{descendant!r}],{str(tmp_path)!r},None);'
        "time.sleep(.3);os._exit(0)"
    )
    # S603: isolated test fixture, no backend or user supplied command.
    process = subprocess.Popen([sys.executable, "-c", owner], cwd=tmp_path)  # noqa: S603
    process.wait(timeout=5)
    time.sleep(1.6)
    assert not (tmp_path / "orphan").exists()


def test_batch_wrapper_is_resolved_without_shell(tmp_path: Path) -> None:
    from _process import resolve_command

    script = tmp_path / "worker.py"
    script.write_text("pass")
    wrapper = tmp_path / "worker.cmd"
    wrapper.write_text(f'@"{sys.executable}" "{script}" %*\n')
    wrapper.chmod(0o755)
    prompt = "x & echo unexpected | other %PATH% !NAME!"
    assert resolve_command([str(wrapper), prompt], None) == [sys.executable, str(script), prompt]


def test_unknown_batch_wrapper_fails_explicitly(tmp_path: Path) -> None:
    from _process import resolve_command

    wrapper = tmp_path / "worker.cmd"
    wrapper.write_text("@echo off\nunknown %*\n")
    wrapper.chmod(0o755)
    with pytest.raises(ValueError, match="batch CLI"):
        resolve_command([str(wrapper), "prompt"], None)
