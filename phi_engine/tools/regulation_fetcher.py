"""Regulation fetcher: fetch PHI provisions from official government sources.

Flow:
  1. Human names a jurisdiction (e.g. "HIPAA")
  2. Fetcher retrieves the page from the hardcoded official URL
  3. LLM extracts PHI-relevant provisions and self-verifies
  4. Diff is generated against the existing authorities/*.md file
  5. Diff is written to authorities/pending/<jurisdiction>_<date>.diff
  6. Human reviews and accepts via `phi-authority accept <file>`

The LLM never fetches arbitrary URLs -- only the hardcoded official sources.

Scope: this is an OFFLINE AUTHORING CLI (``phi-authority``) that maintains the
human-readable ``authorities/*.md`` corpus. It does **not** feed
``phi_engine.pipeline.run.run_pipeline``: the runtime rulebook resolves its rules
and official-source list from ``phi_engine.security.phi_review._PINNED_SOURCES``
and the closed ``phi_engine.security.official_sources._REGISTRY`` (the single
source of truth for live extraction). ``OFFICIAL_SOURCES`` here is scoped to USA
only (2026-07-16 scope change removed every non-USA jurisdiction from this repo),
so the two lists are aligned rather than deliberately separate as before.
"""

from __future__ import annotations

import difflib
import re
import sys
from datetime import date
from pathlib import Path

# Hardcoded official government sources only. Never user-supplied URLs.
OFFICIAL_SOURCES: dict[str, str] = {
    "HIPAA": "https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164",
}

# Map jurisdiction to existing authority file in authorities/
AUTHORITY_FILES: dict[str, str] = {
    "HIPAA": "01_hipaa_164_514_full.md",
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
AUTHORITIES_DIR = PROJECT_ROOT / "authorities"
PENDING_DIR = AUTHORITIES_DIR / "pending"


def _fetch_url(url: str, timeout_s: int = 60) -> str:
    """Fetch URL content as text. Handles HTML and PDF."""
    try:
        import httpx
    except ImportError as exc:
        raise ImportError("pip install httpx") from exc

    resp = httpx.get(url, timeout=timeout_s, follow_redirects=True)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "pdf" in content_type or url.lower().endswith(".pdf"):
        return _extract_pdf_text(resp.content)
    return resp.text


def _extract_pdf_text(data: bytes) -> str:
    """Extract text from PDF bytes using pypdf."""
    import io
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ImportError("pip install pypdf") from exc
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _llm_extract_provisions(raw_text: str, jurisdiction: str) -> str:
    """Use LLM to extract PHI-relevant provisions from raw regulation text."""
    from phi_engine.config.config import get_llm_client

    # Truncate to avoid token limits -- regulations can be very long
    excerpt = raw_text[:12000] if len(raw_text) > 12000 else raw_text

    prompt = f"""\
You are a regulatory expert. From the following official {jurisdiction} regulation text,
extract ALL provisions that:
1. Define what constitutes Protected Health Information (PHI) or personal data
2. List specific identifier categories that must be de-identified
3. Specify de-identification standards, methods, or safe harbors
4. Define exceptions or research exemptions

Format your extraction as structured markdown with:
- Section headings matching the original regulation sections
- Direct quotes where possible (mark with > blockquote)
- Your summary of each provision
- The exact legal citation for each provision

Regulation text:
---
{excerpt}
---

Extracted PHI provisions:"""

    from phi_engine.security.llm_tool_guard import guard_llm_output

    client = get_llm_client()
    response = client.complete(prompt)
    guard_llm_output(response)
    return response


def _llm_self_verify(raw_text: str, extracted: str, jurisdiction: str) -> tuple[str, list[str]]:
    """LLM cross-checks its extraction against the raw text. Returns (verified_text, warnings)."""
    from phi_engine.config.config import get_llm_client

    excerpt = raw_text[:8000] if len(raw_text) > 8000 else raw_text

    prompt = f"""\
You previously extracted PHI provisions from a {jurisdiction} regulation. Now verify your extraction.

Check each extracted provision against the source text below:
1. Is every quoted passage present verbatim in the source? (mark MISMATCH if not)
2. Are there important PHI provisions in the source that were missed? (mark MISSED)
3. Are any extracted provisions inaccurate or misleading? (mark INACCURATE)

Respond with:
- VERIFIED: <list of provisions confirmed accurate>
- WARNINGS: <list of MISMATCH / MISSED / INACCURATE issues found>
- CORRECTED_EXTRACTION: <corrected full extraction, same markdown format>

Source text excerpt:
---
{excerpt}
---

Your previous extraction:
---
{extracted}
---"""

    from phi_engine.security.llm_tool_guard import guard_llm_output

    client = get_llm_client()
    response = client.complete(prompt)
    guard_llm_output(response)

    # Parse warnings out of response
    warnings = re.findall(r"(?:MISMATCH|MISSED|INACCURATE)[^\n]*", response)

    # Try to extract the corrected extraction section
    match = re.search(r"CORRECTED_EXTRACTION:\s*\n(.*)", response, re.DOTALL)
    corrected = match.group(1).strip() if match else extracted

    return corrected, warnings


def _generate_diff(existing_path: Path, new_text: str, jurisdiction: str) -> str:
    """Generate unified diff between existing authority file and new extracted text."""
    if existing_path.is_file():
        existing_lines = existing_path.read_text().splitlines(keepends=True)
    else:
        existing_lines = []

    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        existing_lines,
        new_lines,
        fromfile=str(existing_path.relative_to(PROJECT_ROOT)) if existing_path.exists() else "new_file",
        tofile=f"authorities/{AUTHORITY_FILES.get(jurisdiction, jurisdiction.lower() + '.md')}",
    )
    return "".join(diff)


def fetch_jurisdiction(jurisdiction: str, *, timeout_s: int = 60) -> Path:
    """Fetch regulation for *jurisdiction*, extract PHI provisions, write diff.

    Returns the path to the written .diff file in authorities/pending/.
    """
    jurisdiction = jurisdiction.upper()
    if jurisdiction not in OFFICIAL_SOURCES:
        known = ", ".join(OFFICIAL_SOURCES)
        raise ValueError(f"Unknown jurisdiction: {jurisdiction!r}. Known: {known}")

    url = OFFICIAL_SOURCES[jurisdiction]
    print(f"Fetching {jurisdiction} from: {url}")
    raw_text = _fetch_url(url, timeout_s=timeout_s)
    print(f"  Fetched {len(raw_text):,} characters")

    print("  Extracting PHI provisions via LLM...")
    extracted = _llm_extract_provisions(raw_text, jurisdiction)

    print("  Self-verifying extraction...")
    verified, warnings = _llm_self_verify(raw_text, extracted, jurisdiction)
    if warnings:
        print(f"  Warnings ({len(warnings)}):")
        for w in warnings:
            print(f"    {w}")

    # Determine existing authority file
    authority_file = AUTHORITY_FILES.get(jurisdiction)
    existing_path = (
        AUTHORITIES_DIR / authority_file if authority_file else AUTHORITIES_DIR / f"{jurisdiction.lower()}.md"
    )

    diff = _generate_diff(existing_path, verified, jurisdiction)
    if not diff:
        print("  No changes detected -- authority file is already up to date.")
        return existing_path

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    diff_path = PENDING_DIR / f"{jurisdiction.lower()}_{date.today().isoformat()}.diff"
    diff_path.write_text(diff)
    print(f"  Diff written to: {diff_path}")
    print("  Review and run: phi-authority accept <diff_file>")
    return diff_path


def accept_diff(diff_file: Path) -> None:
    """Apply a pending diff to the authority file (human-approval step)."""
    import subprocess
    result = subprocess.run(
        ["patch", "-p1", "--input", str(diff_file)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"patch failed:\n{result.stderr}")
        sys.exit(1)
    diff_file.unlink()
    print(f"Applied {diff_file.name} and removed pending diff.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Fetch PHI regulations from official sources")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_p = subparsers.add_parser("fetch", help="Fetch and diff a jurisdiction")
    fetch_p.add_argument("jurisdiction", help=f"One of: {', '.join(OFFICIAL_SOURCES)}")
    fetch_p.add_argument("--timeout", type=int, default=60)

    accept_p = subparsers.add_parser("accept", help="Apply a pending diff")
    accept_p.add_argument("diff_file", type=Path, help="Path to .diff file in authorities/pending/")

    subparsers.add_parser("list", help="List supported jurisdictions")

    args = parser.parse_args()

    if args.command == "fetch":
        fetch_jurisdiction(args.jurisdiction, timeout_s=args.timeout)
    elif args.command == "accept":
        accept_diff(args.diff_file)
    elif args.command == "list":
        print("Supported jurisdictions:")
        for j, url in OFFICIAL_SOURCES.items():
            print(f"  {j:15s} {url}")


if __name__ == "__main__":
    _main()
