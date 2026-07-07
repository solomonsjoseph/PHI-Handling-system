"""Standalone PHI pipeline: intake, organize, classify, scrub, publish.

Every module here operates on ``phi_engine.config.config``'s workspace-aware
paths (``PHI_WORKSPACE``-relocatable) so the package can be dropped into any
project. Nothing under this subpackage pulls in the SYNTHETIC-corpus tooling
that lives one level up in the repo (`harness/generate_corpus.py`'s package,
never named literally here so a plain-text search for that dependency stays
a reliable acceptance check) or constructs a provider LLM client directly --
header classification and any opt-in AI rule alignment go through
``phi_engine.security.llm_detector`` / ``phi_engine.security.phi_alignment``,
the two structural chokepoints where an LLM prompt is ever constructed, and
both build headers-only prompts.
"""

from __future__ import annotations
