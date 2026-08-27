"""Cross-platform free-port discovery for local dev infrastructure.

Local setup steps (starting MongoDB, a test Redis, any port-bound
dependency) should never hardcode a port and hope it is free. What is
listening on a given port varies by machine and by day: another service, a
leftover container, an editor's own tooling. Hardcoding invites exactly the
failure this script exists to avoid.

Usage::

    python scripts/find_free_port.py                  # any free port
    python scripts/find_free_port.py --preferred 27017 # 27017 if free, else any free port
    python scripts/find_free_port.py --preferred 27017 --host 0.0.0.0

Prints exactly one integer to stdout and nothing else, so callers can
capture it directly::

    PORT=$(python scripts/find_free_port.py --preferred 27017)
    mongod --port "$PORT" ...

Binds and immediately releases a socket rather than just checking whether a
port is reachable, so it works for any protocol, not only ones that answer
a ping-style request. There is an inherent, unavoidable race between this
script releasing the socket and the caller's process binding it: nothing
holds the port in between. Callers that need to eliminate that race
entirely should bind the socket themselves and inherit the file descriptor,
which is out of scope for a general-purpose discovery helper.
"""
from __future__ import annotations

import argparse
import contextlib
import socket
import sys


def find_free_port(preferred: int | None = None, host: str = "127.0.0.1") -> int:
    """Return a free TCP port on ``host``.

    Tries ``preferred`` first when given. Falls back to asking the OS for
    any free ephemeral port (bind to port 0) when ``preferred`` is unset or
    already taken. Works identically on macOS, Linux, and Windows: it is
    plain ``socket``, no OS-specific commands or files.
    """
    if preferred is not None:
        with contextlib.suppress(OSError):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((host, preferred))
                return preferred

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--preferred", type=int, default=None,
        help="try this port first; fall back to any free port if it is taken")
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="interface to bind against when checking (default: 127.0.0.1)")
    args = parser.parse_args()

    print(find_free_port(args.preferred, args.host))
    return 0


if __name__ == "__main__":
    sys.exit(main())
