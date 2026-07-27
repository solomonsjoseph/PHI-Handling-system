"""Minimal top-level shim package.

Not part of the ported phi_engine plugin layer. Exists solely so
``phi_engine.security.phi_scrub.run_scrub`` can resolve its hard dependency
on ``scripts.extraction.forms_manifest`` (see forms_manifest.py docstring
for the full porting-gap rationale).
"""
