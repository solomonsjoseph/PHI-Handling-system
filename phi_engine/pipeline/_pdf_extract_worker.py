"""Private, minimal-import worker for phi_engine.pipeline.intake_naming's
isolated PDF text extraction.

Deliberately imports NOTHING from ``phi_engine`` itself, and imports
``pdfplumber`` only inside :func:`run` -- the whole point of the
``RLIMIT_AS`` bound this module's entry point applies to itself is to
catch a PDF content stream that decompresses into far more memory than
its on-disk size implies, and that bound is only meaningful if the
process's own baseline virtual-address-space footprint (before touching
any PDF bytes) stays small. ``intake_naming.py``'s own top-level imports
(``config``, the security/model-routing chokepoints, ``intake_preflight``)
pull in a multi-gigabyte virtual-address-space baseline that would make
any address-space bound set from inside that module meaningless for a
process that has already imported it -- this module exists solely so the
spawned child never imports that module (or ``phi_engine.pipeline``'s
other siblings) at all, only the standard library plus ``pdfplumber``.

Applying the hard ``RLIMIT_AS``/``RLIMIT_CPU`` bounds is mandatory and
fail-closed: if ``resource`` cannot be imported (a non-POSIX platform) or
either ``setrlimit`` call itself fails for any reason, ``pdfplumber`` is
never imported and the (possibly hostile) PDF bytes are never touched --
the fixed error sentinel is sent instead. A best-effort limit that
silently falls back to an unbounded parse on failure would defeat the
entire point of isolating this worker.

The reply crossing back into the privileged parent process is bounded,
non-executable UTF-8 (ASCII-subset) JSON -- never ``pickle`` or any other
format that could execute code while being decoded -- with an exact,
fixed ``{"status": ..., "pages": [...]}`` shape the parent independently
validates.
"""

from __future__ import annotations

import io
import json
from typing import Any

_ERROR_PAYLOAD = json.dumps({"status": "error", "pages": []}, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _send(conn: Any, payload: bytes) -> None:
    try:
        conn.send_bytes(payload)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def run(data: bytes, max_pages: int, max_address_bytes: int, max_cpu_seconds: int, conn: Any) -> None:
    """Runs ONLY inside the isolated child process spawned by
    ``intake_naming._extract_pdf_pages``. Applies the caller's hard
    address-space and CPU-time limits before ``pdfplumber`` ever touches
    the (possibly hostile) bytes -- failing closed, with zero pdfplumber
    import and zero byte access, if either limit cannot be established --
    then extracts text from at most ``max_pages`` pages and sends a
    small, bounded, non-executable JSON result back over ``conn``. Every
    internal failure -- a resource-limit-application failure, a
    resource-limit kill, a pdfplumber/pdfminer exception, a pathological
    structure -- collapses to the same fixed ``{"status": "error", "pages":
    []}`` sentinel rather than letting a raw exception (whose message
    could echo source content) cross the process boundary.
    """
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (max_address_bytes, max_address_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (max_cpu_seconds, max_cpu_seconds))
    except Exception:
        # Fail closed: pdfplumber (and the PDF bytes) must NEVER be
        # touched without both hard limits already in place. Any
        # failure here -- resource unavailable, either setrlimit call
        # rejected by the platform -- sends the fixed error sentinel and
        # returns before `import pdfplumber` is ever reached.
        _send(conn, _ERROR_PAYLOAD)
        return

    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            texts = [(page.extract_text() or "") for page in pdf.pages[:max_pages]]
        payload = json.dumps({"status": "ok", "pages": texts}, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    except BaseException:
        payload = _ERROR_PAYLOAD
    _send(conn, payload)
