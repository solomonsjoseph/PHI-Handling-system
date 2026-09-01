#!/usr/bin/env python3
"""Self-contained container entrypoint shim (rewrite plan step 5).

Deliberately dependency-free beyond the stdlib: this script runs inside
the sandbox-runner image, which has no ``phi_core`` installed by design
(``STATIC_CHECK_ALLOWED_IMPORTS``, step 9, is stdlib plus pandas/
openpyxl only). Its return_kind shape checks below deliberately MIRROR,
but cannot literally share code with, ``phi_core.control.sandbox``'s
``_validate_return_contract`` -- ``control.runner.ContainerRunner`` (the
host side) re-validates the same contract authoritatively, using that
real shared function, once this container exits and the result file is
read back onto the trusted host. This is the same "cheap child
pre-check, authoritative parent re-check" split
``control/sandbox.py``'s multiprocessing path already uses for
first-party helpers, now spanning a far less trusted boundary.

Protocol: this process always exits 0. It writes exactly one JSON
object to ``RESULT_PATH`` describing what happened --
``{"ok": true, "payload": ...}`` on success,
``{"ok": false, "type": <exception class name>, "message": <capped
text>}`` on failure. ``ContainerRunner`` treats a missing or
unparseable result file, or in fact any nonzero exit code from this
process, as an infrastructure-level failure distinct from a worker-
level one (the container itself never reaching this script, an OOM
kill, a seccomp/cap denial killing the interpreter, etc.).

Invocation: ``python runner_shim.py <entrypoint_module> <return_kind>``
-- argv, never an environment variable, per the plan's "no environment
variables beyond a controlled PATH" requirement. ``entrypoint_module``
is imported from ``/input`` (the read-only source+dataset mount) and
must define a zero-argument ``run() -> <payload>`` function; everything
it needs (dataset paths, opaque header tokens, etc.) is read from fixed,
known locations under ``/input``, never passed as a function argument.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
import traceback
from pathlib import Path

RESULT_PATH = Path("/workspace/.container_result.json")
INPUT_DIR = Path("/input")
WORKSPACE_DIR = Path("/workspace")

_MAX_MESSAGE_CHARS = 2000
_MAX_JSON_CHARS = 131072
_MAX_PATH_CHARS = 512
_STATUS_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _write_result(payload: dict) -> None:
    RESULT_PATH.write_text(json.dumps(payload), encoding="utf-8")


def _validate(payload: object, return_kind: str) -> None:
    """Mirrors ``phi_core.control.sandbox._validate_return_contract``'s
    shape checks (see module docstring for why this cannot import it
    directly). Every raised message here is fixed/static and never
    embeds ``payload`` itself, for the same reason sandbox.py's own
    messages do not: a rejection message must not become a second way
    for the same content to leak."""
    if return_kind == "count":
        if type(payload) is not int:
            raise ValueError(f"return_kind='count' but worker returned {type(payload).__name__}")
        return
    if return_kind == "status":
        if not isinstance(payload, str) or not _STATUS_TOKEN_RE.match(payload):
            raise ValueError("return_kind='status' but worker did not return a short lowercase token")
        return
    if return_kind == "json":
        if not isinstance(payload, str) or len(payload) > _MAX_JSON_CHARS:
            raise ValueError(
                f"return_kind='json' but worker returned a non-string or oversized (> {_MAX_JSON_CHARS} chars) value"
            )
        json.loads(payload)  # raises ValueError/JSONDecodeError on invalid JSON
        return
    if return_kind == "path":
        if not isinstance(payload, str) or len(payload) > _MAX_PATH_CHARS:
            raise ValueError(
                f"return_kind='path' but worker returned a non-string or oversized (> {_MAX_PATH_CHARS} chars) value"
            )
        base = WORKSPACE_DIR.resolve()
        resolved = (base / payload).resolve()
        if resolved != base and base not in resolved.parents:
            raise ValueError("return_kind='path' but the returned value does not resolve inside /workspace")
        if not resolved.is_file():
            raise ValueError("return_kind='path' but the resolved artifact does not exist")
        return
    raise ValueError(f"unknown return_kind: {return_kind!r}")


def main() -> None:
    if len(sys.argv) != 3:
        _write_result({
            "ok": False, "type": "UsageError",
            "message": "expected exactly two arguments: <entrypoint_module> <return_kind>",
        })
        return
    entrypoint_module, return_kind = sys.argv[1], sys.argv[2]
    sys.path.insert(0, str(INPUT_DIR))
    try:
        module = importlib.import_module(entrypoint_module)
        run_fn = getattr(module, "run", None)
        if run_fn is None or not callable(run_fn):
            raise AttributeError(f"{entrypoint_module} defines no callable run() function")
        payload = run_fn()
        _validate(payload, return_kind)
        _write_result({"ok": True, "payload": payload})
    except BaseException as exc:  # the boundary this script exists to hold: never propagate
        message = "".join(traceback.format_exception_only(type(exc), exc))[:_MAX_MESSAGE_CHARS]
        _write_result({"ok": False, "type": type(exc).__name__, "message": message})


if __name__ == "__main__":
    main()
