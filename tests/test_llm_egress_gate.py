"""Prompt egress gate: LLMClient.complete blocks a PHI-contaminated outbound
prompt BEFORE provider dispatch (closes prior-audit C2 for the pipeline path).
"""

from __future__ import annotations

import pytest

import phi_engine.config.config as config

# NOTE: PHIEgressBlockedError is intentionally NOT imported at module level.
# phi_engine.config.config.LLMClient.complete() imports it LOCALLY on every
# call (to avoid a circular import with phi_engine.security.phi_gate). Other
# hermetic tests in this suite sweep `phi_engine.*` out of sys.modules
# between hermetic workspaces (see `_drop_phi_runtime_modules` in
# tests/test_stress_standalone.py) to avoid stale import-time configuration
# and class identity; if this file captured the exception class at
# COLLECTION time, a sweep by an earlier-running test would leave a stale
# class object here that no longer matches the freshly re-imported class
# raised at call time. Catch the STABLE base (PermissionError) and verify by
# class NAME instead of identity.


def test_clean_prompt_is_not_blocked_by_the_gate():
    client = config.LLMClient(provider="ollama", model="qwen3:8b", base_url="http://127.0.0.1:1")
    # No Ollama server is reachable at that address -- the call must fail with
    # a NETWORK error, never PHIEgressBlockedError, proving the gate does not
    # false-positive on an ordinary headers-only classification prompt.
    with pytest.raises(Exception) as exc_info:
        client.complete("Classify these column headers for PHI: SUBJID, AGE, NOTES, SITE_CODE")
    assert type(exc_info.value).__name__ != "PHIEgressBlockedError"


@pytest.mark.parametrize(
    "prompt",
    [
        "Patient SSN is 123-45-6789, please summarize the record.",
        "Contact the participant at jane.doe@example.com for follow-up.",
        "Subject lives at 742 Evergreen Terrace, please verify.",
    ],
)
def test_contaminated_prompt_is_blocked_before_dispatch(prompt: str):
    client = config.LLMClient(provider="ollama", model="qwen3:8b", base_url="http://127.0.0.1:1")
    with pytest.raises(PermissionError) as exc_info:
        client.complete(prompt)
    assert type(exc_info.value).__name__ == "PHIEgressBlockedError"
    # Value-free: the raised message must name a pattern CATEGORY, never echo
    # the matched text itself.
    message = str(exc_info.value)
    assert "123-45-6789" not in message
    assert "jane.doe@example.com" not in message
    assert "Evergreen Terrace" not in message


def test_disabled_provider_raises_before_the_gate_even_runs():
    client = config.LLMClient(provider="none", model="none")
    with pytest.raises(RuntimeError, match="disabled"):
        client.complete("Patient SSN is 123-45-6789.")
