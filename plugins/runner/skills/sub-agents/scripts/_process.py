"""Process-tree ownership and bounded shutdown on POSIX and Windows."""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from _posix import OWNER_ENV, PosixOwner


class ProcessTree:
    def __init__(
        self, process: subprocess.Popen[str], job: int | None = None, *, token: str = ""
    ) -> None:
        self.process = process
        self.job = job
        self.owner = PosixOwner(process.pid, token)

    def stop(self, *, force: bool = False) -> None:
        if sys.platform == "win32":
            if self.job is not None:
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                if not kernel.TerminateJobObject(self.job, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
        else:
            self.owner.stop(force=force, include_root=self.process.poll() is None)

    def close(self) -> None:
        if sys.platform == "win32" and self.job is not None:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle(self.job)
            self.job = None


def _windows_job(process: subprocess.Popen[str]) -> int:
    if sys.platform != "win32":
        raise OSError("Windows Job Objects require Windows")
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("per_process", ctypes.c_int64),
            ("per_job", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("min_ws", ctypes.c_size_t),
            ("max_ws", ctypes.c_size_t),
            ("active", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in ("read_ops", "write_ops", "other_ops", "read", "write", "other")
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", IoCounters),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    handle = kernel.OpenProcess(0x0100 | 0x0001, False, process.pid)
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        if not job or not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(job, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(job)
    except OSError:
        if job:
            kernel.CloseHandle(job)
        raise
    finally:
        if handle:
            kernel.CloseHandle(handle)


def resolve_command(command: list[str], env: dict[str, str] | None) -> list[str]:
    executable = shutil.which(
        command[0], path=(os.environ if env is None else env).get("PATH", os.defpath)
    )
    if executable is None:
        raise FileNotFoundError(command[0])
    path = Path(executable)
    if path.suffix.lower() not in (".cmd", ".bat"):
        return [executable, *command[1:]]
    # Never pass LLM prompts through cmd.exe's implicit batch-file shell. Resolve
    # standard npm shims directly to node + entrypoint, or a simple interpreter
    # wrapper to its literal executable/script. Unknown batch programs fail closed.
    script = path.read_text(encoding="utf-8", errors="replace")
    npm = re.search(r'"%(?:dp0|~dp0)%?[/\\]([^"\r\n]+\.[cm]?js)"', script, re.IGNORECASE)
    if npm:
        entry = path.parent / npm.group(1).replace("\\", "/")
        node = path.parent / "node.exe"
        binary = (
            str(node)
            if node.is_file()
            else shutil.which(
                "node", path=(os.environ if env is None else env).get("PATH", os.defpath)
            )
        )
        if binary and entry.is_file():
            return [binary, str(entry), *command[1:]]
    simple = re.fullmatch(r'\s*@?"([^"\r\n]+)"\s+"([^"\r\n]+)"(?:\s+%\*)?\s*', script)
    if simple:
        binary, entry_name = simple.groups()
        if Path(binary).is_file() and Path(entry_name).is_file():
            return [binary, entry_name, *command[1:]]
    raise ValueError(
        "Unsupported batch CLI launcher. Install a native executable or standard npm shim; prompts are not sent through cmd.exe."
    )


def launch(command: list[str], cwd: str, env: dict[str, str] | None) -> ProcessTree:
    # Windows bootstrap waits for a byte BEFORE starting any user command. This
    # eliminates the spawn/AssignProcessToJobObject race without private APIs.
    actual = [
        sys.executable,
        str(Path(__file__).with_name("_process_bootstrap.py")),
        *resolve_command(command, env),
    ]
    token = uuid.uuid4().hex
    child_env = dict(os.environ if env is None else env)
    if sys.platform != "win32":
        child_env[OWNER_ENV] = token
    # S603: argv is constructed by the closed backend registry; shell is disabled.
    process = subprocess.Popen(  # noqa: S603
        actual,
        cwd=cwd,
        env=child_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=sys.platform != "win32",
    )
    tree = ProcessTree(process, token=token)
    try:
        if sys.platform == "win32":
            tree.job = _windows_job(process)
            assert process.stdin is not None
            process.stdin.write("G")
            process.stdin.close()
            process.stdin = None
        return tree
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        tree.close()
        raise
