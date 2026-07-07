import importlib

import pytest


def test_security_import_surface_exports_phi_gate_check():
    from phi_engine.security import phi_gate_check

    result = phi_gate_check("safe clinical summary")
    assert result.blocked is False


def test_guard_llm_output_blocks_phi_without_echoing_raw_value():
    from phi_engine.security.llm_tool_guard import LLMToolOutputBlocked, guard_llm_output

    with pytest.raises(LLMToolOutputBlocked) as exc_info:
        guard_llm_output("Patient SSN 123-45-6789")

    message = str(exc_info.value)
    assert "SSN" in message
    assert "123-45-6789" not in message


def test_llm_safe_tool_passes_safe_return_and_blocks_unsafe_return():
    from phi_engine.security.llm_tool_guard import LLMToolOutputBlocked, llm_safe_tool

    @llm_safe_tool
    def safe_tool():
        return {"summary": "No identifiers detected"}

    @llm_safe_tool
    def unsafe_tool():
        return {"summary": "Patient SSN 123-45-6789"}

    assert getattr(safe_tool, "__phi_llm_safe__") is True
    assert safe_tool() == {"summary": "No identifiers detected"}
    with pytest.raises(LLMToolOutputBlocked):
        unsafe_tool()


def test_validate_llm_read_path_rejects_audit_and_snapshot_paths(tmp_path, monkeypatch):
    import phi_engine.config.config as config
    from phi_engine.security.llm_tool_guard import LLMToolPathDenied, validate_llm_read_path

    output_dir = tmp_path / "output"
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)

    audit_path = output_dir / "StudyA" / "audit" / "phi_scrub_report.json"
    snapshot_path = output_dir / "StudyA" / "snapshots" / "snap-1" / "manifest.json"

    with pytest.raises(LLMToolPathDenied):
        validate_llm_read_path(audit_path)
    with pytest.raises(LLMToolPathDenied):
        validate_llm_read_path(snapshot_path)


def test_llm_client_none_provider_is_disabled():
    from phi_engine.config.config import LLMClient

    with pytest.raises(RuntimeError, match="PHI LLM provider is disabled"):
        LLMClient(provider="none", model="none").complete("x")


def test_get_llm_client_rejects_external_provider_without_explicit_allow(monkeypatch):
    import phi_engine.config.config as config

    monkeypatch.setenv("PHI_LLM_PROVIDER", "openai")
    monkeypatch.setenv("PHI_LLM_MODEL", "gpt-4o")
    monkeypatch.delenv("PHI_ALLOW_EXTERNAL_LLM", raising=False)
    config = importlib.reload(config)

    with pytest.raises(RuntimeError, match="PHI_ALLOW_EXTERNAL_LLM=true"):
        config.get_llm_client()

    monkeypatch.setenv("PHI_ALLOW_EXTERNAL_LLM", "true")
    config = importlib.reload(config)
    client = config.get_llm_client()
    assert client.provider == "openai"

    monkeypatch.delenv("PHI_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("PHI_LLM_MODEL", raising=False)
    monkeypatch.delenv("PHI_ALLOW_EXTERNAL_LLM", raising=False)
    importlib.reload(config)
