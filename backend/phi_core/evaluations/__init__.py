"""Phase 16: repeatable agent/model evaluation harnesses.

Every evaluation in this package (and in ``backend/tests/test_eval_phase16_*.py``,
which consume it) measures a real production code path against a labeled
synthetic ground truth. None of it calls a live LLM provider: an LLM-facing
call is always intercepted by a deterministic double (a test-only subclass
overriding ``call_json``/``call_with_web_search``) so a run is byte-for-byte
repeatable. Model self-reported confidence is never used as a pass/fail
criterion anywhere in this package -- every measurement compares a
prediction to an external label, never to the model's own certainty.

This package holds only shared, non-agent scoring infrastructure
(``scoring.py``). It imports real agent/control-plane code but never
modifies it.
"""
