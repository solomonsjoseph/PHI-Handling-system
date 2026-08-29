"""Wave R-c: integration of Phases 1-3's built-but-inert control-plane
modules into the live ``phi_core/agents/`` pipeline.

Ordering (mandatory, see docs/PHASE_STATUS.md "Phase R: remediation and
integration of Phases 1 to 3"): Step 1 (AgentContext control-plane
fields) -> Step 2 (opaque map encryption, D5) -> Step 3 (header safety
gate) -> Step 4 (sandboxed raw reads) -> Step 5 (MethodRegistry) ->
Step 6 (HandoffGateway broker edges) -> Step 7 (AuthorizationService
naming). Step 2 must land before Step 3: wiring the header gate before
encrypting the opaque map it writes into would create an unencrypted,
unpurged cleartext PHI store, a net security regression.

Step 8 invariants below are modelled on ``test_architecture_boundaries.py``:
AST-based, positive-controlled (a scan matching nothing fails loudly), with
documented allowlist exceptions.

Allowlist rationale (Step 8 invariant 2, sandboxed raw reads):

- ``phi_core/agents/operator.py::_read_columns`` calls
  ``_read_dataset_headers`` directly as a real on-disk-header fallback.
  ``operator.py`` is outside this wave's owns list (Phase 10 retires the
  whole module per docs/PHASE_STATUS.md); allowlisted here, not edited.
- ``phi_core/agents/reasoning.py::verify_keep_decisions`` reads real
  dataset row values directly via ``iter_dataset_rows`` (not one of the
  four relocated readers, so it never matches this scan's call-site
  pattern at all, but is named here for the same reason: it is a real,
  accepted, unsandboxed raw-data read, retired only in Phase 9 per
  docs/PHASE_STATUS.md, not touched by Wave R-c).
- ``phi_core/agents/reasoning.py::Executor.run``'s own direct-call
  fallback (used only when ``ctx.sandbox is None``, i.e. a context built
  without ``ActivationFactory``'s ``needs_sandbox=True`` opt-in, such as
  ``control.testing.make_ctx`` in every pre-existing unit test) is a
  permanent, documented compatibility path, not a retiring debt --
  allowlisted by enclosing-function name below.
- ``phi_corpus/replay.py`` (a corpus-benchmark/scoring harness, not part
  of the live agent pipeline) calls ``apply_column_actions_to_dataset``/
  ``_redact_metadata_file`` directly. Out of scope: the scan below is
  rooted at ``phi_core/`` only, a sibling package to ``phi_corpus/``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from phi_core.control.context import AgentContext
from phi_core.control.handoff import HandoffGateway
from phi_core.control.methods import get_approved_methods
from phi_core.control.opaque import OpaqueMap
from phi_core.control.records import MethodRecord, SandboxRecord
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import make_ctx

BACKEND_ROOT = Path(__file__).resolve().parent.parent
PHI_CORE_ROOT = BACKEND_ROOT / "phi_core"


def _exclude(path: Path) -> bool:
    s = str(path)
    return "/.venv/" in s or "/node_modules/" in s or "/tests/" in s or "/__pycache__/" in s


# ==========================================================================
# Step 1: AgentContext gains the control plane
# ==========================================================================


@pytest.mark.asyncio
async def test_claimed_agent_context_carries_handoff_and_sandbox() -> None:
    """``ActivationFactory._claim_and_build`` is the single wiring point:
    every claimed context carries a non-null ``handoff`` gateway
    (cheap, always attached), and -- when the caller opts a run into a
    sandbox via ``needs_sandbox=True`` -- a non-null ``sandbox`` record
    naming a real, existing workspace directory."""
    import os

    os.environ["PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY"] = "1"
    from phi_core.control.activation import ActivationFactory
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.testing import _TestLlmConfig, start_test_run

    store = MemoryControlStore()
    session_id = "a" * 32
    run = await start_test_run(store, session_id)
    factory = ActivationFactory(None, _TestLlmConfig(), store=store)

    ctx = await factory.activate(session_id=session_id, run_id=run.run_id, agent="Schema")
    assert isinstance(ctx.handoff, HandoffGateway)
    assert ctx.sandbox is None

    ctx2 = await factory.activate(
        session_id=session_id, run_id=run.run_id, agent="Executor", needs_sandbox=True,
    )
    assert isinstance(ctx2.sandbox, SandboxRecord)
    assert Path(ctx2.sandbox.workspace_path).is_dir()

    # Same run -> same cached sandbox, never a fresh workspace per agent.
    ctx3 = await factory.activate(
        session_id=session_id, run_id=run.run_id, agent="Executor", needs_sandbox=True,
    )
    assert ctx3.sandbox.workspace_path == ctx2.sandbox.workspace_path

    from phi_core.control.sandbox import destroy_sandbox

    destroy_sandbox(ctx2.sandbox)


# ==========================================================================
# Step 2: encrypt the opaque map at rest (D5) -- must land before Step 3
# ==========================================================================


@pytest.mark.asyncio
async def test_opaque_map_never_persists_the_canonical_value_in_cleartext() -> None:
    """D5: ``OpaqueMap.to_opaque`` used to store the raw canonical value in
    cleartext inside ``WorkflowRun.opaque_map``. Plant a sensitive header
    value, run it through the store-backed ``StoreOpaqueWriter.to_opaque``,
    and assert the raw value never appears in cleartext anywhere in the
    persisted ``workflow_runs`` document -- not just the in-memory
    ``OpaqueMap`` object -- while still round-tripping back to the exact
    original value through ``from_opaque``."""
    import os

    os.environ["PHI_ENV"] = "dev"
    from phi_core.control.context import StoreOpaqueWriter
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.tasks import TaskService
    from phi_core.control.testing import _TestLlmConfig, start_test_run

    store = MemoryControlStore()
    session_id = "b" * 32
    run = await start_test_run(store, session_id)
    tasks = TaskService(store, CapabilityPolicy(_TestLlmConfig()))
    writer = StoreOpaqueWriter(store, tasks, run_id=run.run_id)

    sensitive = "SENSITIVE_HEADER_VALUE_123-45-6789"
    token = await writer.to_opaque("header", sensitive)

    doc = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert sensitive not in json.dumps(doc, default=str), (
        "raw canonical value found in cleartext in the persisted workflow_runs document"
    )
    assert doc["opaque_map"][token] != sensitive

    assert await writer.from_opaque(token) == sensitive


def test_opaque_map_collision_check_still_works_once_values_are_encrypted() -> None:
    """The pure ``OpaqueMap`` class (no store): a genuine collision (same
    token, different canonical value) must still raise, and an identical
    re-mint of the same (kind, canonical) pair must still be a no-op --
    both must survive encrypting what gets stored under the token."""
    import os

    os.environ["PHI_ENV"] = "dev"
    from phi_core.control.opaque import OpaqueLookupError

    mapping: dict[str, str] = {}
    opaque = OpaqueMap("run-collision", mapping)
    token = opaque.to_opaque("header", "value-one")
    assert opaque.to_opaque("header", "value-one") == token  # idempotent re-mint
    assert "value-one" not in mapping[token]
    assert opaque.from_opaque(token) == "value-one"

    # Force the same token to already (encrypted-at-rest) hold a
    # different canonical value, then mint again: must still fail closed.
    from phi_core.crypto import encrypt_api_key

    opaque2 = OpaqueMap("run-collision", {token: encrypt_api_key("a-different-value")})
    with pytest.raises(OpaqueLookupError):
        opaque2.to_opaque("header", "value-one")


@pytest.mark.asyncio
async def test_super_orchestrator_can_erase_a_runs_opaque_map() -> None:
    """D5 right-to-erasure/retention capability: ``SuperOrchestrator
    .erase_opaque_map`` clears a run's ``opaque_map`` to empty through the
    same CAS boundary ``record_opaque_map`` already uses, so the
    sensitive-header vault can be wiped independently of the rest of the
    run's record. (Wiring an actual caller into server.py's
    ``session_delete`` route / ``_purge_settled_sessions_loop`` is outside
    this wave's owns list -- server.py is not in it -- and is reported as
    a known gap, not silently left unimplemented without disclosure.)"""
    import os

    os.environ["PHI_ENV"] = "dev"
    from phi_core.control.context import StoreOpaqueWriter
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.superorchestrator import SuperOrchestrator
    from phi_core.control.tasks import TaskService
    from phi_core.control.testing import _TestLlmConfig, start_test_run

    store = MemoryControlStore()
    session_id = "c" * 32
    run = await start_test_run(store, session_id)
    tasks = TaskService(store, CapabilityPolicy(_TestLlmConfig()))
    writer = StoreOpaqueWriter(store, tasks, run_id=run.run_id)
    await writer.to_opaque("header", "some-sensitive-value")

    doc = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert doc["opaque_map"]

    orchestrator = SuperOrchestrator(store, tasks)
    updated = await orchestrator.erase_opaque_map(run_id=run.run_id)
    assert updated.opaque_map == {}

    doc = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert doc["opaque_map"] == {}
