"""Standalone PHI pipeline: intake, organize, classify, scrub, publish.

Every module here operates on ``phi_engine.config.config``'s workspace-aware
paths (``PHI_WORKSPACE``-relocatable) so the package can be dropped into any
project. This subpackage has no external data-production dependency and does
not construct a provider LLM client directly -- header classification and any
opt-in AI rule alignment go through ``phi_engine.security.llm_detector`` /
``phi_engine.security.phi_alignment``, the two structural chokepoints where
an LLM prompt is ever constructed, and both build headers-only prompts.
"""

from __future__ import annotations
