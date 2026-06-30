"""Orchestrator skill package marker (Note 19 plugin consolidation).

The orchestrator entry point (``run.py``) is the top-level publish driver: a
10-phase state machine that invokes the pipeline skills as file-path
subprocesses (D3). The hyphenated parent skill directory is not an importable
Python package, so this marker exists for tooling, not for ``import``-time
package resolution.
"""
