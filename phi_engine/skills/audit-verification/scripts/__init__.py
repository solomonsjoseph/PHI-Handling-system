"""Skill script package marker (Note 19 plugin consolidation).

The CLI entry point (``run.py``) is runnable directly by file path and is
invoked by the orchestrator as a file-path subprocess (D3). The hyphenated
parent skill directory is not an importable Python package, so this marker
exists for tooling, not for ``import``-time package resolution.
"""
