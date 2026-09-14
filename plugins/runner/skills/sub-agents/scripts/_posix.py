"""POSIX lifecycle ownership across setsid and reparenting (not a sandbox)."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass

# Inherited across fork/exec, unlike ancestry after an intermediate parent exits.
# Deliberately clearing this marker AND leaving the session requires OS-level
# containment; a userspace process owner is not a security boundary.
OWNER_ENV = "SUB_AGENTS_PROCESS_OWNER"


@dataclass(frozen=True)
class ProcessIdentity:
    parent: int
    started: str
    owned: bool


def snapshot(token: str) -> dict[int, ProcessIdentity]:
    # Use the OS utility, not a task-controlled PATH. Never log its environment
    # output: retain only the random ownership marker and process identities.
    # S603: fixed system executable and arguments, no shell or user input.
    result = subprocess.run(
        ["/bin/ps", "eww", "-axo", "pid=,ppid=,lstart=,command="],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=2,
        check=False,
        env={"PATH": os.defpath, "LC_ALL": "C"},
    )
    if result.returncode:
        raise OSError("Cannot inspect POSIX process ownership")
    processes: dict[int, ProcessIdentity] = {}
    marker = f" {OWNER_ENV}={token}"
    for line in result.stdout.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        try:
            pid, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        command = parts[7]
        owned = bool(token) and any(
            suffix == "" or suffix[0].isspace() for suffix in command.split(marker)[1:]
        )
        processes[pid] = ProcessIdentity(parent, " ".join(parts[2:7]), owned)
    return processes


class PosixOwner:
    def __init__(self, root_pid: int, token: str) -> None:
        self.root_pid = root_pid
        self.token = token
        self.known: dict[int, str] = {}

    def stop(self, *, force: bool = False, include_root: bool = True) -> None:
        try:
            processes = snapshot(self.token)
        except (OSError, subprocess.TimeoutExpired):
            # Never let inspection failure prevent termination of a root whose
            # lifetime the caller still owns. Do not signal stale remembered PIDs.
            if include_root:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.root_pid, signal.SIGKILL)
            raise
        owned = {pid for pid, info in processes.items() if info.owned}
        # Preserve identities across TERM -> KILL even if a child clears its
        # environment and is reparented in between. Reject observed PID reuse.
        owned.update(
            pid
            for pid, started in self.known.items()
            if pid in processes and processes[pid].started == started
        )
        # Ancestry also covers children launched with a replacement environment.
        children: dict[int, list[int]] = {}
        for pid, info in processes.items():
            children.setdefault(info.parent, []).append(pid)
        pending = list(owned)
        if include_root:
            pending.append(self.root_pid)
        while pending:
            for pid in children.get(pending.pop(), []):
                if pid not in owned:
                    owned.add(pid)
                    pending.append(pid)
        self.known.update({pid: processes[pid].started for pid in owned})
        sig = signal.SIGKILL if force else signal.SIGTERM
        # Signal descendants before the launcher so they cannot escape through
        # reparenting between the soft and forced phases.
        for pid in owned - {self.root_pid, os.getpid()}:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)
        if include_root:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.root_pid, sig)
