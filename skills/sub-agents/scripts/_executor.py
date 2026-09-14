from __future__ import annotations

import contextlib
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, TypedDict

from _builder import AgentInvocation, ProcessInvocation, build_invocation_args
from _constants import DEFAULT_TIMEOUT_MS
from _process import ProcessTree, launch
from _stream import JSONEventDecoder, StreamData, StreamProcessor


class _AgentResponseFields(TypedDict):
    # The error path passes the backend's payload through, so result is not str.
    result: object
    exit_code: int
    status: str
    cli: str


# Two classes because NotRequired needs 3.11 and the scripts support 3.9.
class AgentResponse(_AgentResponseFields, total=False):
    """The JSON contract returned to the caller; ``error`` is present on failure."""

    error: str
    metadata: dict[str, object]


def _result_value(result: StreamData | None) -> object:
    """Read the result out of a stream payload, preserving whatever it holds."""
    if not result:
        return ""
    return result.get("result", "")


# SIGTERM may be reported as 143 or -15.
_SUCCESS_EXIT_CODES = (0,)

_CURSOR_AUTH_ERROR_PHRASES = (
    "authentication required",
    "authentication failed",
    "not authenticated",
    "not logged in",
    "unauthenticated",
    "unauthorized",
    "please log in",
    "please login",
)


def _cursor_legacy_key_guidance(
    error: str, environment: Mapping[str, str] | None = None
) -> str | None:
    """Give the calling LLM migration guidance when legacy config explains auth failure."""
    environment = os.environ if environment is None else environment
    if "CLI_API_KEY" not in environment:
        return None

    cursor_api_key = environment.get("CURSOR_API_KEY")
    if cursor_api_key and cursor_api_key.strip():
        return None

    normalized_error = error.lower()
    is_auth_error = any(phrase in normalized_error for phrase in _CURSOR_AUTH_ERROR_PHRASES) or (
        "api key" in normalized_error
        and any(word in normalized_error for word in ("invalid", "missing", "rejected"))
    )
    if not is_auth_error:
        return None

    return (
        "Cursor authentication error: CLI_API_KEY is set but no longer supported. "
        "Run `cursor-agent login` or set CURSOR_API_KEY, then retry."
    )


# Below this, a silent run says more about the deadline than about the backend.
_SHORT_TIMEOUT_MS = 60000


def _timeout_error(error: str, stdout_chars: int, timeout_ms: int) -> str:
    """Say whether a timed-out backend was producing anything when it was killed."""
    if stdout_chars:
        return f"{error} after emitting {stdout_chars} characters"
    detail = (
        f"{error} without emitting any output. Backends invoked with a non-streaming "
        "output format buffer the whole reply until the run ends, so a killed run "
        "leaves nothing behind"
    )
    if timeout_ms < _SHORT_TIMEOUT_MS:
        return f"{detail}, and a deadline this short may be the whole reason."
    return (
        f"{detail}. Check whether the task can be narrowed, or the files the worker "
        "would otherwise search for supplied in the prompt itself."
    )


# PLR0913: the diagnostic count is a distinct input; keyword-only after the error.
def _partial_response(  # noqa: PLR0913
    cli: str,
    result: StreamData | None,
    exit_code: int,
    error: str,
    *,
    stdout_chars: int | None = None,
) -> AgentResponse:
    response: AgentResponse = {
        "result": _result_value(result),
        "exit_code": exit_code,
        "status": "partial" if result else "error",
        "cli": cli,
        "error": error,
    }
    if stdout_chars is not None:
        response["metadata"] = {"stdout_chars": stdout_chars}
    return response


def _error_response(
    cli: str, exit_code: int, error: str, partial_result: StreamData | None = None
) -> AgentResponse:
    return {
        "result": _result_value(partial_result),
        "exit_code": exit_code,
        "status": "error",
        "cli": cli,
        "error": error,
    }


def _classify_status(result: StreamData | None, exit_code: int, *, terminated_by_us: bool) -> str:
    """Decide the run's outcome, treating intentional termination as success."""
    if not result:
        return "error"
    if result.get("status") == "error" or result.get("is_error") is True:
        return "error"
    if result.get("status") == "partial":
        return "partial"
    if terminated_by_us or exit_code in _SUCCESS_EXIT_CODES:
        return "success"
    return "partial"


def _error_message(
    response: AgentResponse,
    result: StreamData | None,
    stderr: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    result_error = result.get("error") if result else None
    result_subtype = result.get("subtype") if result else None
    result_text = result.get("result") if result else None
    if isinstance(result_error, str) and result_error.strip():
        msg = result_error.strip()
    elif isinstance(result_subtype, str) and result_subtype.startswith("error_"):
        msg = f"CLI reported {result_subtype.strip()}"
    elif isinstance(result_text, str) and result_text.strip():
        msg = result_text.strip()
    elif result:
        msg = "CLI reported an error"
    else:
        msg = f"CLI exited with code {response['exit_code']}"

    if stderr and stderr.strip():
        msg += f": {stderr.strip()}"

    if response["cli"] == "cursor-agent":
        error_context = msg
        output = response["result"]
        # With no parsed result the response carries the joined stdout.
        if result is None and isinstance(output, str):
            error_context += f"\n{output[:8192]}"
        msg = _cursor_legacy_key_guidance(error_context, environment) or msg
    return msg


# PLR0913: every input decides a case; keyword-only so order is never inferred.
def build_final_response(  # noqa: PLR0913
    *,
    cli: str,
    returncode: int | None,
    result: StreamData | None,
    stdout_lines: list[str],
    stderr: str,
    terminated_by_us: bool = False,
    environment: Mapping[str, str] | None = None,
) -> AgentResponse:
    exit_code = returncode if returncode is not None else 1
    status = _classify_status(result, exit_code, terminated_by_us=terminated_by_us)

    response: AgentResponse = {
        "result": _result_value(result) if result else "".join(stdout_lines),
        "exit_code": exit_code,
        "status": status,
        "cli": cli,
    }
    metadata: dict[str, object] = {
        key: result[key]
        for key in ("session_id", "stop_reason", "usage", "model")
        if result and key in result
    }
    if metadata:
        response["metadata"] = metadata
    if status == "error":
        response["error"] = _error_message(response, result, stderr, environment)
    return response


# Caps apply even after terminal events; queues and individual reads are bounded.
_MAX_STDOUT_CHARS = 64 * 1024 * 1024
_MAX_LINE_CHARS = 4 * 1024 * 1024
_GRACE_SECONDS = 1.0


@dataclass
class ExecutionOptions:
    cancelled: Callable[[], bool] = lambda: False
    log_dir: Path | None = None
    environment: dict[str, str] | None = None


def _spawn_reader(
    stream: IO[str], name: str, output: queue.Queue[tuple[str, str | None]], stop: threading.Event
) -> threading.Thread:
    def put(value: str | None) -> None:
        while not stop.is_set():
            try:
                output.put((name, value), timeout=0.05)
                return
            except queue.Full:
                continue

    def read() -> None:
        try:
            # readline(size) preserves prompt terminal events without waiting to
            # fill a block, but bounds memory even when there is no newline.
            while not stop.is_set():
                chunk = stream.readline(8192)
                if not chunk:
                    break
                put(chunk)
        except (OSError, ValueError):
            pass
        finally:
            put(None)

    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    return thread


# PLR1702: bounded stream loop and unconditional process cleanup share state.
def _drive_process(
    tree: ProcessTree, cli: str, timeout_ms: int, options: ExecutionOptions
) -> AgentResponse:
    process = tree.process
    assert process.stdout is not None and process.stderr is not None
    output: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=64)
    stop = threading.Event()
    threads = [
        _spawn_reader(stream, name, output, stop)
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    processor = StreamProcessor(cli)
    chunks: list[str] = []
    stderr = ""
    pending = ""
    count = 0
    stdout_chars = 0
    eof: set[str] = set()
    deadline = time.monotonic() + timeout_ms / 1000
    terminal_at: float | None = None
    terminated = False
    failure: tuple[int, str] | None = None
    decoder = JSONEventDecoder(_MAX_STDOUT_CHARS)
    logs: dict[str, IO[str]] = {}
    try:  # noqa: PLR1702
        if options.log_dir:
            options.log_dir.mkdir(parents=True, exist_ok=True)
            logs = {
                name: (options.log_dir / (name + ".log")).open("w", encoding="utf-8")
                for name in ("stdout", "stderr")
            }
        while True:
            now = time.monotonic()
            if options.cancelled():
                failure = (130, "Sub-agent cancelled")
                break
            if now >= deadline:
                failure = (124, f"Sub-agent timed out after {timeout_ms} ms")
                break
            if terminal_at is not None and now - terminal_at >= _GRACE_SECONDS:
                terminated = process.poll() is None
                break
            if len(eof) == 2 and process.poll() is not None:
                break
            try:
                name, chunk = output.get(timeout=min(0.05, max(0.001, deadline - now)))
            except queue.Empty:
                continue
            if chunk is None:
                eof.add(name)
                continue
            count += len(chunk)
            if count > _MAX_STDOUT_CHARS:
                failure = (1, f"Sub-agent output exceeded {_MAX_STDOUT_CHARS} characters")
                break
            if name in logs:
                logs[name].write(chunk)
                logs[name].flush()
            if name == "stderr":
                stderr = (stderr + chunk)[-65536:]
                continue
            chunks.append(chunk)
            stdout_chars += len(chunk)
            pending += chunk
            if len(pending) > _MAX_LINE_CHARS:
                failure = (1, "Sub-agent output line exceeded maximum length")
                break
            if pending.endswith("\n"):
                pending = ""
            try:
                for event in decoder.feed(chunk):
                    if processor.process_line(event):
                        terminal_at = time.monotonic()
            except ValueError as decode_error:
                failure = (1, str(decode_error))
                break
        if failure:
            if failure[0] == 124 and processor.get_result() is not None:
                terminated = process.poll() is None
                return build_final_response(
                    cli=cli,
                    returncode=process.poll(),
                    result=processor.get_result(),
                    stdout_lines=chunks,
                    stderr=stderr,
                    terminated_by_us=terminated,
                    environment=options.environment,
                )
            code, error = failure
            if code == 124:
                error = _timeout_error(error, stdout_chars, timeout_ms)
            return _partial_response(
                cli, processor.get_result(), code, error, stdout_chars=stdout_chars
            )
        return build_final_response(
            cli=cli,
            returncode=process.poll(),
            result=processor.get_result(),
            stdout_lines=chunks,
            stderr=stderr,
            terminated_by_us=terminated,
            environment=options.environment,
        )
    finally:
        # Always clean descendants, including those keeping inherited pipes open.
        stop.set()
        try:
            tree.stop()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=0.5)
            tree.stop(force=True)
            process.wait(timeout=1)
        finally:
            tree.close()
            if process.stdin is not None:
                process.stdin.close()
            for thread in threads:
                thread.join(timeout=0.2)
            for log in logs.values():
                log.close()
            # Don't close a stream from another thread while it is in read().
            if not any(thread.is_alive() for thread in threads):
                process.stdout.close()
                process.stderr.close()


def _build_proc_env(
    env_override: dict[str, str | None] | None,
    base: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Apply child environment overrides; ``None`` removes a variable."""
    if not env_override and base is None:
        return None
    proc_env = dict(os.environ if base is None else base)
    for key, value in (env_override or {}).items():
        if value is None:
            proc_env.pop(key, None)
        else:
            proc_env[key] = value
    return proc_env


# PLR0913: keep the existing adapter inputs and add optional runtime controls.
def _spawn_and_drive(  # noqa: PLR0913
    process_invocation: ProcessInvocation,
    inv: AgentInvocation,
    proc_env: dict[str, str] | None,
    timeout_ms: int,
    options: ExecutionOptions | None = None,
) -> AgentResponse:
    command, args = process_invocation.command, process_invocation.args
    try:
        tree = launch([command, *args], inv.cwd, proc_env)
        return _drive_process(tree, inv.cli, timeout_ms, options or ExecutionOptions())
    except FileNotFoundError:
        return _error_response(
            inv.cli,
            127,
            f"CLI unavailable: {command!r} was not found on PATH. Install it or select another backend.",
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return _error_response(inv.cli, 1, f"{type(e).__name__}: {e}")


def _isolated_opencode_env(
    env_override: dict[str, str | None] | None,
    temp_dir: str,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str | None]:
    """Isolate OpenCode state to prevent concurrent SQLite session locks."""
    data_home = os.path.join(temp_dir, "data")
    state_home = os.path.join(temp_dir, "state")
    os.makedirs(os.path.join(data_home, "opencode"))
    os.makedirs(state_home)

    source_environment = os.environ if environment is None else environment
    home = source_environment.get("HOME") or source_environment.get("USERPROFILE")
    if home is None and environment is None:
        home = os.path.expanduser("~")
    default_data_home = source_environment.get("XDG_DATA_HOME") or (
        os.path.join(home, ".local", "share") if home else ""
    )
    auth_file = os.path.join(default_data_home, "opencode", "auth.json")
    try:
        if default_data_home and os.path.isfile(auth_file):
            shutil.copy2(auth_file, os.path.join(data_home, "opencode", "auth.json"))
    except OSError:
        # OpenCode reports authentication failures when this copy was required.
        pass

    return {**(env_override or {}), "XDG_DATA_HOME": data_home, "XDG_STATE_HOME": state_home}


def execute_agent(
    inv: AgentInvocation,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    options: ExecutionOptions | None = None,
) -> AgentResponse:
    if timeout_ms <= 0:
        raise ValueError("timeout must be positive")
    options = options or ExecutionOptions()
    process_invocation = build_invocation_args(inv, options.environment)

    if inv.cli == "opencode":
        temp_dir = tempfile.mkdtemp(prefix="subagent-opencode-")
        try:
            proc_env = _build_proc_env(
                _isolated_opencode_env(
                    process_invocation.env_override, temp_dir, options.environment
                ),
                options.environment,
            )
            return _spawn_and_drive(process_invocation, inv, proc_env, timeout_ms, options)
        finally:
            # _spawn_and_drive reaps the process before returning.
            shutil.rmtree(temp_dir, ignore_errors=True)

    proc_env = _build_proc_env(process_invocation.env_override, options.environment)
    return _spawn_and_drive(process_invocation, inv, proc_env, timeout_ms, options)
