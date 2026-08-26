"""phi-rulebook skill scripts.

Thin CLI wrapper over the shared rulebook engine
:mod:`scripts.security.phi_rulebook`. The engine (versioned offline cache,
committed seed, drift detection) lives under ``scripts/`` so it is a stable
shared base; this skill only provides the operator-facing command surface
(plugins/ -> scripts/, one-way, per Note 19).
"""
