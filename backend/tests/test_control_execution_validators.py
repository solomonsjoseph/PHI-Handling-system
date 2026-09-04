"""Phase 9: the seven pre-execution deterministic validators
(``control/execution_validators.py``, docs #52). Each test proves its
validator genuinely rejects at least its named category -- not merely
that the class exists."""
from __future__ import annotations

from pathlib import Path

import pytest
from phi_core.control.execution_validators import (
    CapabilityBroker,
    ExecutionValidationRejected,
    MethodRegistryValidator,
    OperationAllowlistValidator,
    PathPolicyValidator,
    ResourceLimitValidator,
    SandboxPolicyValidator,
    StaticCodeValidator,
    run_pre_execution_validators,
)
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.records import MethodRecord, SandboxRecord
from phi_core.paths import DATA_DIR

REASONING_PY = Path(__file__).resolve().parent.parent / "phi_core" / "agents" / "reasoning.py"


def _grant(**tools: int):
    policy = CapabilityPolicy(None)
    grant = policy.issue_grant(run_id="r1", task_id="t1", agent="Operator", task_type="operator")
    return grant.model_copy(update={"tools": dict(tools)})


def _sandbox(**overrides) -> SandboxRecord:
    base = dict(
        run_id="r1", workspace_path=str(DATA_DIR / "sandbox" / "r1"),
        max_cpu_seconds=10, max_memory_bytes=1024, max_wall_seconds=10,
    )
    base.update(overrides)
    return SandboxRecord(**base)


# ---- StaticCodeValidator ----------------------------------------------------


def test_static_code_validator_rejects_forbidden_import(tmp_path) -> None:
    bad = tmp_path / "bad_worker.py"
    bad.write_text("import subprocess\n\ndef f():\n    return subprocess.run(['ls'])\n", encoding="utf-8")
    violations = StaticCodeValidator().validate([bad])
    categories = {c for c, _ in violations}
    assert "unexpected_subprocess" in categories


def test_static_code_validator_rejects_network_call() -> None:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("import socket\n\ndef f():\n    return socket.socket()\n")
        path = Path(fh.name)
    try:
        violations = StaticCodeValidator().validate([path])
        categories = {c for c, _ in violations}
        assert "network_call" in categories
    finally:
        path.unlink()


def test_static_code_validator_clears_the_real_worker_module() -> None:
    assert StaticCodeValidator().validate([REASONING_PY]) == []


# ---- OperationAllowlistValidator --------------------------------------------


def test_operation_allowlist_validator_rejects_unknown_operation() -> None:
    decisions = [{"file_id": "f1", "column": "ssn", "action": "delete_everything"}]
    violations = OperationAllowlistValidator().validate(decisions, {"keep", "drop"})
    assert violations == [("unknown_operation", "file_id='f1' column='ssn' action='delete_everything'")]


def test_operation_allowlist_validator_clears_known_operations() -> None:
    decisions = [{"file_id": "f1", "column": "ssn", "action": "drop"}]
    assert OperationAllowlistValidator().validate(decisions, {"keep", "drop"}) == []


# ---- MethodRegistryValidator -------------------------------------------------


def test_method_registry_validator_rejects_unapproved_method() -> None:
    decisions = [{"file_id": "f1", "column": "dob", "method_id": "m1"}]
    researched = [MethodRecord(method_id="m1", hipaa_category="E", name="x", lifecycle="researched")]
    violations = MethodRegistryValidator().validate(decisions, researched)
    categories = {c for c, _ in violations}
    assert "unapproved_method" in categories


def test_method_registry_validator_clears_an_approved_method() -> None:
    decisions = [{"file_id": "f1", "column": "dob", "method_id": "m1"}]
    approved = [MethodRecord(method_id="m1", hipaa_category="E", name="x", lifecycle="approved")]
    assert MethodRegistryValidator().validate(decisions, approved) == []


# ---- CapabilityBroker ---------------------------------------------------------


def test_capability_broker_rejects_unapproved_import(tmp_path) -> None:
    bad = tmp_path / "bad_worker2.py"
    bad.write_text("import some_unvetted_package\n", encoding="utf-8")
    violations = CapabilityBroker().validate([bad], None)
    categories = {c for c, _ in violations}
    assert "unapproved_import" in categories


def test_capability_broker_rejects_credential_shaped_grant_tool() -> None:
    grant = _grant(aws_secret=1)
    violations = CapabilityBroker().validate([REASONING_PY], grant)
    assert violations == [("credential_access", "grant tool 'aws_secret' is credential-shaped")]


def test_capability_broker_clears_a_grant_with_no_credential_shaped_tools() -> None:
    grant = _grant(web_search=1)
    assert CapabilityBroker().validate([REASONING_PY], grant) == []


# ---- PathPolicyValidator ------------------------------------------------------


def test_path_policy_validator_rejects_path_traversal() -> None:
    files = [{"file_id": "f1", "stored_path": str(DATA_DIR / "uploads" / ".." / ".." / "etc" / "passwd")}]
    violations = PathPolicyValidator().validate(files)
    categories = {c for c, _ in violations}
    assert "path_escape" in categories


def test_path_policy_validator_rejects_host_filesystem_access() -> None:
    files = [{"file_id": "f1", "stored_path": "/etc/passwd"}]
    violations = PathPolicyValidator().validate(files)
    categories = {c for c, _ in violations}
    assert "host_filesystem_access" in categories


def test_path_policy_validator_clears_a_path_inside_data_dir() -> None:
    files = [{"file_id": "f1", "stored_path": str(DATA_DIR / "uploads" / "f1.csv")}]
    assert PathPolicyValidator().validate(files) == []


# ---- ResourceLimitValidator ---------------------------------------------------


def test_resource_limit_validator_rejects_unbounded_runtime() -> None:
    violations = ResourceLimitValidator().validate(_sandbox(max_wall_seconds=0))
    categories = {c for c, _ in violations}
    assert "unbounded_runtime" in categories


def test_resource_limit_validator_rejects_unbounded_memory() -> None:
    violations = ResourceLimitValidator().validate(_sandbox(max_memory_bytes=0))
    categories = {c for c, _ in violations}
    assert "unbounded_memory" in categories


def test_resource_limit_validator_clears_bounded_sandbox() -> None:
    assert ResourceLimitValidator().validate(_sandbox()) == []


def test_resource_limit_validator_is_a_noop_with_no_sandbox() -> None:
    assert ResourceLimitValidator().validate(None) == []


# ---- SandboxPolicyValidator ----------------------------------------------------


def test_sandbox_policy_validator_rejects_network_not_denied() -> None:
    violations = SandboxPolicyValidator().validate(_sandbox(network_denied=False))
    assert violations == [("network_call", "sandbox record does not have network_denied set")]


def test_sandbox_policy_validator_clears_network_denied_sandbox() -> None:
    assert SandboxPolicyValidator().validate(_sandbox(network_denied=True)) == []


# ---- run_pre_execution_validators ----------------------------------------------


def test_run_pre_execution_validators_raises_with_every_violation_named() -> None:
    decisions = [{"file_id": "f1", "column": "ssn", "action": "not_a_real_action"}]
    files = [{"file_id": "f1", "stored_path": "/etc/passwd"}]
    with pytest.raises(ExecutionValidationRejected) as excinfo:
        run_pre_execution_validators(
            decisions=decisions, files=files, allowed_operations={"keep", "drop"},
            worker_module_paths=[REASONING_PY], sandbox=_sandbox(max_wall_seconds=0),
        )
    assert "unknown_operation" in excinfo.value.categories
    assert "host_filesystem_access" in excinfo.value.categories
    assert "unbounded_runtime" in excinfo.value.categories


def test_run_pre_execution_validators_passes_clean_input() -> None:
    decisions = [{"file_id": "f1", "column": "ssn", "action": "drop"}]
    files = [{"file_id": "f1", "stored_path": str(DATA_DIR / "uploads" / "f1.csv")}]
    run_pre_execution_validators(
        decisions=decisions, files=files, allowed_operations={"keep", "drop"},
        worker_module_paths=[REASONING_PY], sandbox=_sandbox(),
    )


def test_data_dir_is_resolved_so_the_traversal_check_never_fires_on_our_own_config():
    """An operator can legitimately write DATA_DIR=/srv/app/../data. Every
    stored path is built from DATA_DIR, and PathPolicyValidator refuses a
    stored path containing a '..' component, so an unresolved DATA_DIR made
    every run fail pre-execution validation on the service's own config."""
    from phi_core.paths import DATA_DIR

    assert DATA_DIR.is_absolute()
    assert ".." not in DATA_DIR.parts
