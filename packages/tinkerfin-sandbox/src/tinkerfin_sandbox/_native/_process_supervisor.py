"""Hold an owned process group until its host releases the private control socket."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading


def main() -> None:
    # This trusted program runs outside the workload sandbox. The same-sandbox
    # signal rule prevents workload code from terminating this group anchor.
    # Keeping it alive avoids reusing a command PID after that command is reaped.
    if os.getpgrp() != os.getpid():
        raise RuntimeError("process supervisor requires an owned session")
    control = socket.socket(fileno=int(sys.argv[1]))

    def host_closed() -> None:
        try:
            control.recv(1)
        finally:
            # The group ID is our own live PID, including when the host crashes.
            os.killpg(os.getpid(), signal.SIGKILL)

    monitor = threading.Thread(target=host_closed, daemon=True)
    monitor.start()
    try:
        process = subprocess.Popen(sys.argv[2:], close_fds=True)
    except OSError:
        process = None
    # Only the workload owns the standard streams. The anchor cannot delay EOF
    # or prevent cleanup if a detached descendant retains a stream descriptor.
    for descriptor in (0, 1, 2):
        os.close(descriptor)
    returncode = 127 if process is None else process.wait()
    control.sendall(json.dumps({"returncode": returncode}).encode() + b"\n")
    monitor.join()


if __name__ == "__main__":
    main()
