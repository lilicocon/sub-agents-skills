"""Private launch gate and POSIX owner-pipe watchdog."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

from _posix import OWNER_ENV, PosixOwner


def main() -> None:
    owner: PosixOwner | None = None
    cleanup_lock = threading.Lock()
    if sys.platform == "win32":
        if sys.stdin.read(1) != "G":
            sys.exit(1)
    else:
        owner = PosixOwner(os.getpid(), os.environ[OWNER_ENV])
        # Verify ownership inspection before launching any backend. Failure is
        # visible as a launcher error, never a silently weakened cleanup path.
        owner.stop(include_root=False)

        def watch_owner() -> None:
            os.read(0, 1)
            # Do not kill the launcher until detached descendants are stopped.
            # The lock also prevents a concurrent child-exit cleanup snapshot
            # from retaining stale identities while this path is terminating.
            try:
                with cleanup_lock:
                    assert owner is not None
                    owner.stop(force=True, include_root=False)
            finally:
                os.killpg(os.getpgrp(), signal.SIGKILL)

        threading.Thread(target=watch_owner, daemon=True).start()
    # S603: private launcher receives the already validated backend argv.
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)  # noqa: S603
    code = child.wait()
    if owner is not None:
        with cleanup_lock:
            owner.stop(force=True, include_root=False)
    os._exit(code if code >= 0 else 128 - code)


if __name__ == "__main__":
    main()
