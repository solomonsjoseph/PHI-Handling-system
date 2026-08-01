"""Safe .docx opener shared by every docx reader.

Historical context: iter_20 added a decompression-bomb defence to the
dictionary-path docx reader; iter_22 discovered the same defence was
missing on the narrative-path reader, which the two readers had drifted
apart. This helper is the single source of truth so any future addition
(new file type, tighter cap, additional entity refusal) applies
everywhere at once.

The helper is deliberately tiny and does ONE thing: verify the inner
``word/document.xml`` is under the size cap and return its bytes, or
None if the file is not a safe .docx to read.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

# 10 MiB is well above any legitimate document.xml we would ever see
# (Sir's real ~12 KB dictionary docx inflates to ~50 KB; a manuscript-
# sized narrative docx stays well under 1 MB). A malicious docx can
# deflate at ratios up to ~1032x so a tiny outer archive could otherwise
# expand into hundreds of megabytes and OOM the worker.
DOCX_XML_MAX_BYTES = 10 * 1024 * 1024


def safe_read_docx_xml(path: Path) -> bytes | None:
    """Return the bytes of ``word/document.xml`` if the docx is safe to
    process, else ``None``.

    "Safe" means: valid ZIP container, has a ``word/document.xml`` entry,
    and that entry's declared uncompressed size is under
    ``DOCX_XML_MAX_BYTES``. The actual streamed read is also capped so
    archives that lie about their declared size are still refused.
    """
    try:
        with zipfile.ZipFile(path) as z:
            try:
                info = z.getinfo("word/document.xml")
            except KeyError:
                return None
            if info.file_size > DOCX_XML_MAX_BYTES:
                return None
            with z.open("word/document.xml") as f:
                raw = f.read(DOCX_XML_MAX_BYTES + 1)
                if len(raw) > DOCX_XML_MAX_BYTES:
                    return None
                return raw
    except zipfile.BadZipFile:
        return None


def is_safe_docx(path: Path) -> bool:
    """Convenience for callers that use python-docx and don't need the
    raw XML bytes -- they just need to know the archive is safe to open.
    """
    return safe_read_docx_xml(path) is not None
