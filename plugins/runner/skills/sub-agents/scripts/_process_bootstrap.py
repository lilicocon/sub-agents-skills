"""Internal Windows launch gate. No command runs before Job Object assignment."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

if __name__ == "__main__":
    if sys.platform == "win32":
        if sys.stdin.read(1) != "G":
            sys.exit(1)
    else:
        # The owning runner keeps this pipe open. If it crashes, kill this
        # session and every CLI descendant rather than leaving background edits.
        def watch_owner() -> None:
            os.read(0, 1)
            os.killpg(os.getpgrp(), signal.SIGKILL)

        threading.Thread(target=watch_owner, daemon=True).start()
    # S603: private launcher receives the already validated backend argv.
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)  # noqa: S603
    code = child.wait()
    os._exit(code if code >= 0 else 128 - code)
