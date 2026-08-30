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
from phi_core.control.handoff import HandoffGateway
from phi_core.control.opaque import OpaqueMap
from phi_core.control.records import SandboxRecord
from phi_core.control.store import MemoryControlStore
from phi_core.control.testing import FakeGateway, make_ctx

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


# ==========================================================================
# Step 3: wire the header safety gate (2E)
# ==========================================================================


def test_classify_header_uncertain_for_ambiguous_embedded_digit_run() -> None:
    """``classify_header`` gains a real ``uncertain`` outcome: a header
    carrying an embedded numeric run not already caught by a strict rule
    (SSN/phone/NPI/MRN) is ambiguous -- it could be a coincidental
    site/version/sequence code, or a real identifier fragment typed into
    a header by mistake -- and is deliberately noisy (real false
    positives on ordinary numeric-suffixed columns are expected; that
    noise is exactly why ``uncertain`` routes to non-blocking review
    rather than a hard block). Ordinary digit-free column names are
    unaffected."""
    from phi_core.control.source_projection import classify_header

    disposition, reasons = classify_header("site_02139")
    assert disposition == "uncertain"
    assert reasons

    disposition, reasons = classify_header("id_1234")
    assert disposition == "uncertain"

    assert classify_header("visit_date") == ("safe", [])
    assert classify_header("patient_id")[0] == "safe"


@pytest.mark.asyncio
async def test_schema_projects_sensitive_header_to_opaque_token_never_raw_text() -> None:
    """A header carrying a typed-in SSN never reaches Schema's
    agent-facing/LLM-facing ``columns`` output under its literal text --
    only the opaque token does."""
    from phi_core.agents.specialists import Schema

    ctx = make_ctx("Schema")
    schema = Schema(ctx)
    out = await schema.run(dataset_files=[
        {"file_id": "f1", "columns": ["patient_id", "123-45-6789"]},
    ])
    names = [c["name"] for c in out["columns"]]
    assert "123-45-6789" not in names
    assert "patient_id" in names
    assert any(n.startswith("header_") for n in names)


@pytest.mark.asyncio
async def test_schema_uncertain_header_is_projected_not_blocked_and_raises_review() -> None:
    """An ``uncertain``-disposition header is projected exactly like a
    ``sensitive`` one (opaque token, run continues) and additionally
    raises a non-blocking review item -- a recorded trace event, never a
    ``HumanReviewRequest`` (which pauses the run; confirmed by reading
    ``SuperOrchestrator.request_human_review``, the wrong tool for a
    non-blocking flag)."""
    from phi_core.agents.specialists import Schema

    ctx = make_ctx("Schema")
    schema = Schema(ctx)
    out = await schema.run(dataset_files=[
        {"file_id": "f1", "columns": ["patient_id", "site_02139"]},
    ])
    names = [c["name"] for c in out["columns"]]
    assert "site_02139" not in names
    assert "patient_id" in names
    review_events = [m for m in ctx.trace.legacy_messages if m.phase == "schema.header_uncertain_review"]
    assert review_events, "expected a non-blocking review trace event for the uncertain header"


@pytest.mark.asyncio
async def test_schema_exceeding_uncertain_header_ceiling_blocks_with_failure_class(monkeypatch) -> None:
    """Past ``limits.MAX_UNCERTAIN_HEADERS_PER_RUN`` uncertain headers in
    one run, Schema refuses with the ``HEADER_SENSITIVE_CONTENT``
    ``FailureClass`` rather than silently continuing to accumulate
    unresolved ambiguous headers."""
    from phi_core.agents import specialists

    monkeypatch.setattr(specialists.limits, "MAX_UNCERTAIN_HEADERS_PER_RUN", 1)
    ctx = make_ctx("Schema")
    schema = specialists.Schema(ctx)
    with pytest.raises(specialists.UncertainHeaderCeilingExceeded) as excinfo:
        await schema.run(dataset_files=[
            {"file_id": "f1", "columns": ["site_02139", "id_1234"]},
        ])
    assert excinfo.value.failure_class == "HEADER_SENSITIVE_CONTENT"


@pytest.mark.asyncio
async def test_lexicon_routes_dictionary_rows_through_source_projection(tmp_path) -> None:
    """Lexicon's extracted dictionary-row text is routed through
    ``source_projection`` before any provider call: a secret-shaped value
    typed into a dictionary row never reaches the LLM prompt."""
    from phi_core.agents.specialists import Lexicon

    src = tmp_path / "dict.csv"
    src.write_text("variable,description\nssn_col,sk-ant-" + "a" * 30 + "\n", encoding="utf-8")
    gateway = FakeGateway()
    ctx = make_ctx("Lexicon", gateway=gateway)
    lexicon = Lexicon(ctx)
    await lexicon.run(dict_files=[{"file_id": "f1", "stored_path": str(src)}])
    for req in gateway.requests:
        assert "sk-ant-" not in req.user_prompt


@pytest.mark.asyncio
async def test_instrument_routes_form_text_through_source_projection(tmp_path, monkeypatch) -> None:
    """Instrument's extracted Tier-2 form text is routed through
    ``source_projection`` before any provider call: a secret-shaped value
    embedded in form text never reaches the LLM prompt."""
    from phi_core.agents import specialists

    monkeypatch.setattr(specialists, "_read_form_text", lambda path: "Contact sk-ant-" + "a" * 30)
    src = tmp_path / "form.docx"
    src.write_bytes(b"stub")
    gateway = FakeGateway()
    ctx = make_ctx("Instrument", gateway=gateway)
    instrument = specialists.Instrument(ctx)
    await instrument.run(form_files=[{"file_id": "f1", "stored_path": str(src), "subtype": "docx"}])
    for req in gateway.requests:
        assert "sk-ant-" not in req.user_prompt


# ==========================================================================
# Step 4: put raw-row work behind the sandbox
# ==========================================================================


@pytest.mark.asyncio
async def test_executor_dataset_read_happens_only_inside_sandbox_never_in_parent(tmp_path, monkeypatch) -> None:
    """``Executor.run``'s four raw-data call sites (exercised here via
    ``apply_column_actions_to_dataset``) route through
    ``control.sandbox.run_isolated`` once a run has opted into a sandbox
    (``ActivationFactory.activate(..., needs_sandbox=True)``): the raw
    dataset file is opened only inside that separate process, never
    directly in the parent. A ``builtins.open``/``io.open`` spy in this
    (parent) test process can only ever observe parent-process opens -- a
    ``multiprocessing.spawn`` child re-imports everything fresh, so any
    open the child performs is invisible here by construction. Proving
    the source path never appears in the spy's log, while the export
    still lands with the expected transformed content, demonstrates the
    read genuinely happened in the isolated child."""
    import builtins
    import dataclasses
    import io
    import os

    from phi_core.agents.reasoning import Executor
    from phi_core.control.sandbox import create_sandbox, destroy_sandbox

    os.environ["PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY"] = "1"

    src = tmp_path / "data.csv"
    src.write_text("name\nJane Doe\n", encoding="utf-8")

    ctx = make_ctx("Executor")
    sandbox = create_sandbox(ctx.run_id)
    ctx = dataclasses.replace(ctx, sandbox=sandbox)
    executor = Executor(ctx)

    opened_paths: list[str] = []
    real_open = builtins.open

    def _spy(file, *args, **kwargs):
        opened_paths.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _spy)
    monkeypatch.setattr(io, "open", _spy)

    files = [{"file_id": "f1", "kind": "dataset", "stored_path": str(src), "subtype": "csv", "columns": ["name"]}]
    decisions = [{"file_id": "f1", "column": "name", "action": "drop"}]
    try:
        result = await executor.run(files, decisions)
    finally:
        destroy_sandbox(sandbox)

    assert str(src) not in opened_paths, (
        "raw dataset file must never be opened directly in the parent process when the run has a sandbox"
    )
    out = Path(result["exports"]["f1"]).read_text(encoding="utf-8")
    assert "Jane Doe" not in out


# ==========================================================================
# Step 5: wire MethodRegistry
# ==========================================================================


@pytest.mark.asyncio
async def test_praxis_falls_back_when_recommended_method_is_only_researched_not_approved() -> None:
    """Verified source evidence is not execution authorization (spec
    section 38): once a context carries the ``ctx.methods`` facade,
    ``PHIMethodsExpert.method_for`` additionally checks
    ``ctx.methods.get_approved_methods``. A category whose only
    registered ``MethodRecord`` sits at lifecycle ``"researched"``
    (never promoted to ``"approved"``) falls back to the deterministic
    method instead of shipping the unapproved one, even though its
    reported source is genuinely tool-backed and on an authoritative
    domain (D12 verification alone is not enough)."""
    import dataclasses
    from unittest.mock import AsyncMock

    from phi_core.agents.experts import PHIMethodsExpert
    from phi_core.control.context import StoreMethodRegistryReader
    from phi_core.control.methods import register_method

    store = MemoryControlStore()
    await register_method(store, hipaa_category="E", name="researched_only_method")

    ctx = make_ctx("PHIMethodsExpert")
    ctx = dataclasses.replace(ctx, methods=StoreMethodRegistryReader(store))
    agent = PHIMethodsExpert(ctx)
    agent._log = AsyncMock()
    real_url = "https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164"
    agent.call_json_with_web_search = AsyncMock(return_value=(
        {"methods": [{
            "name": "researched_only_method", "how_to_apply": "x", "why": "y",
            "params": {}, "utility_preserving": True, "clinical_impact": "z",
            "reference_paper": "", "sources": [{"url": real_url}],
        }]},
        [{"url": real_url}],
    ))

    reply = await agent.method_for("E")

    assert reply["methods"] == [PHIMethodsExpert._fallback("E")["methods"][0]]
    assert reply["methods"][0]["name"] != "researched_only_method"


# ==========================================================================
# Step 6: wire HandoffGateway for the three broker edges
# ==========================================================================


@pytest.mark.asyncio
async def test_manager_broker_edges_route_through_handoff_gateway_in_order() -> None:
    """``Manager``'s guardian query broker (``ask_schema``/
    ``ask_instrument``/``ask_lexicon``) additionally records each query
    as a Judge -> specialist handoff attempt through ``ctx.handoff`` once
    a context carries the facade: exactly three ``phase == "handoff"``
    trace events land, on the (Judge, Schema)/(Judge, Instrument)/
    (Judge, Lexicon) edges, in the same order the three broker calls
    were made."""
    import dataclasses

    from phi_core.agents.manager import Manager

    class _FakeSchema:
        def verify(self, column, file_id=None):
            return {"present": True, "file_id": file_id or "f1"}

    class _FakeInstrument:
        def verify(self, field_or_variable, file_id=None):
            return {"present": True, "file_id": file_id or "f1", "field": {}}

    class _FakeLexicon:
        async def answer(self, column, assumption, reasoning):
            return {"verdict": "confirmed"}

    store = MemoryControlStore()
    ctx = make_ctx("Manager")
    ctx = dataclasses.replace(ctx, handoff=HandoffGateway(store, session_id=ctx.session_id))
    manager = Manager(ctx)
    manager.attach_schema(_FakeSchema())
    manager.attach_instrument(_FakeInstrument())
    manager.attach_lexicon(_FakeLexicon())

    await manager.ask_schema("Judge", "mrn")
    await manager.ask_instrument("Judge", "dob")
    await manager.ask_lexicon("Judge", "mrn", "direct identifier", "looks like an MRN")

    events = await store.find_many("trace_events", {"phase": "handoff"})
    assert len(events) == 3
    assert [e["direction"] for e in events] == ["Judge->Schema", "Judge->Instrument", "Judge->Lexicon"]
    assert all(e["run_id"] == ctx.run_id for e in events)


# ==========================================================================
# Step 7: route the gateway through the named AuthorizationService
# ==========================================================================


@pytest.mark.asyncio
async def test_provider_gateway_routes_validation_through_authorization_service(monkeypatch) -> None:
    """``ProviderGateway._validate_request`` now calls
    ``authorization.authorize_capability`` -- the same
    ``policy.check_provider``/``policy.check_data_class`` pair it
    already called internally, renamed for naming consistency with the
    spec's AuthorizationService boundary; a pure rename with no new
    security check -- exactly once per completion attempt. A request
    whose ``max_tokens`` exceeds its grant is denied later in
    ``_validate_request``, before any provider call, so this proves the
    routing without a live or mocked LLM call."""
    from types import MappingProxyType, SimpleNamespace

    from phi_core.control import authorization
    from phi_core.control import policy as policy_module
    from phi_core.control.gateway import GatewayRequest, ProviderGateway
    from phi_core.control.policy import MANIFESTS, POLICY_VERSION, CapabilityPolicy

    calls: list[tuple] = []
    real = authorization.authorize_capability

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(authorization, "authorize_capability", _spy)

    pipeline = MANIFESTS["Pipeline"].model_copy(update={"allowed_providers": frozenset({"anthropic"})})
    monkeypatch.setattr(policy_module, "MANIFESTS", MappingProxyType({**MANIFESTS, "Pipeline": pipeline}))

    store = MemoryControlStore()
    run_id, task_id, session_id = "d" * 32, "e" * 32, "f" * 32
    policy = CapabilityPolicy(SimpleNamespace(provider="anthropic", model="claude-test", base_url=""))
    grant = policy.issue_grant(run_id=run_id, task_id=task_id, agent="Pipeline", task_type="pipeline_run")
    await store.insert("capability_grants", grant)

    req = GatewayRequest(
        session_id=session_id, run_id=run_id, task_id=task_id, agent="Pipeline", attempt=1,
        purpose="pipeline", input_class="internal", grant_id=grant.grant_id,
        provider="anthropic", model="claude-test", endpoint="",
        system_prompt="system", user_prompt="user", coaching_note=None, tool_results=(),
        allowed_tools={}, response_schema="no_provider_output",
        timeout_s=30.0, max_tokens=grant.budget.max_tokens + 1, max_cost_usd=0.01,
        policy_version=POLICY_VERSION,
    )

    result = await ProviderGateway(store).complete(req)

    assert result.status == "denied"
    assert "MAX_TOKENS_PER_TASK" in result.denial_reason
    assert len(calls) == 1


# ==========================================================================
# Step 8: invariant tests -- 5 exclusivity scans + behavioral proofs
# ==========================================================================
#
# Invariants 2-5's behavioral proof is the corresponding Step's own test
# above (Step 4/5/6/7), reused verbatim per the wave's own design rather
# than duplicated: `test_executor_dataset_read_happens_only_inside_
# sandbox_never_in_parent`, `test_praxis_falls_back_when_recommended_
# method_is_only_researched_not_approved`, `test_manager_broker_edges_
# route_through_handoff_gateway_in_order`, `test_provider_gateway_routes_
# validation_through_authorization_service`. Invariant 1's behavioral
# proof gets a new test below: Steps 1-3 already proved Schema's own
# output never carries a raw sensitive header, but not yet that Judge's
# actual LLM-facing prompt (the real downstream consumer) stays clean.


def _call_sites(root: Path, target_names: set[str]) -> list[tuple[Path, int]]:
    """AST-scan every .py file under `root` (skipping this suite's
    standard excludes -- see `_exclude` above) for a call whose callee's
    bare name or attribute name is in `target_names`. Mirrors
    `test_architecture_boundaries.py`'s call-site-scan convention."""
    sites: list[tuple[Path, int]] = []
    for path in root.rglob("*.py"):
        if _exclude(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in target_names:
                sites.append((path, node.lineno))
            elif isinstance(func, ast.Attribute) and func.attr in target_names:
                sites.append((path, node.lineno))
    return sites


def _enclosing_function_name(path: Path, lineno: int) -> str | None:
    """Name of the innermost function/method definition in `path` whose
    body spans `lineno`, or ``None`` at module level."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    candidates = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno <= lineno <= getattr(node, "end_lineno", node.lineno)
    ]
    if not candidates:
        return None
    innermost = min(candidates, key=lambda n: getattr(n, "end_lineno", n.lineno) - n.lineno)
    return innermost.name


# ---- invariant 1: header gate is live --------------------------------------


def test_classify_header_call_sites_confined_to_schema_and_source_projection() -> None:
    """Exclusivity: ``classify_header`` is only ever invoked from
    ``specialists.py`` (Schema's own header-safety wiring, Step 3) or
    from ``source_projection.py`` itself (the sibling ``source_
    projection`` function Lexicon/Instrument route dictionary/form text
    through) -- never from any other output path."""
    sites = _call_sites(PHI_CORE_ROOT, {"classify_header"})
    allowed = {
        PHI_CORE_ROOT / "agents" / "specialists.py",
        PHI_CORE_ROOT / "control" / "source_projection.py",
    }
    offenders = [(p, ln) for p, ln in sites if p not in allowed]
    assert offenders == [], f"classify_header called outside its allowed sites: {offenders}"
    assert sites, "the scan itself found nothing"


@pytest.mark.asyncio
async def test_judge_prompt_never_carries_a_raw_sensitive_header_only_the_opaque_token() -> None:
    """Behavioral (extends the Step 3 Schema-output test with the actual
    downstream consumer): Schema's own opaque-projected output is what
    ``Judge.run``'s prompt embeds (``f"...{schema}..."``) -- an SSN typed
    into a header never reaches the LLM-facing Judge prompt, only the
    ``header_...`` opaque token does."""
    from phi_core.agents.reasoning import Judge
    from phi_core.agents.specialists import Schema

    schema = Schema(make_ctx("Schema"))
    schema_out = await schema.run(dataset_files=[
        {"file_id": "f1", "columns": ["patient_id", "123-45-6789"]},
    ])

    gateway = FakeGateway()
    judge = Judge(make_ctx("Judge", gateway=gateway))
    await judge.run(schema=schema_out, instrument={}, lexicon={}, statute={})

    assert gateway.requests, "Judge never called the gateway"
    for req in gateway.requests:
        assert "123-45-6789" not in req.user_prompt
    assert any("header_" in req.user_prompt for req in gateway.requests)


# ---- invariant 2: raw reads are sandboxed ----------------------------------


def test_sandboxed_raw_reader_call_sites_confined_to_reasoning_and_operator_read_columns() -> None:
    """Exclusivity: the four relocated raw-data readers
    (``_read_dataset_headers``, ``read_narrative``, ``_redact_metadata_
    file``, ``apply_column_actions_to_dataset``) are only ever called
    from ``reasoning.py`` (their own definitions, the Step 4 sandboxed
    dispatch wrappers, and ``Executor``'s sandboxed/direct-call dispatch
    methods) or from ``operator.py::_read_columns`` (the documented
    on-disk-header fallback, allowlisted -- ``operator.py`` is outside
    this wave's owns list and Phase 10 retires the whole module per
    docs/PHASE_STATUS.md). ``phi_corpus/replay.py``'s direct calls are
    out of scope by construction: this scan is rooted at ``phi_core/``,
    a sibling package."""
    targets = {
        "_read_dataset_headers", "read_narrative",
        "_redact_metadata_file", "apply_column_actions_to_dataset",
        "verify_keep_decisions",
    }
    sites = _call_sites(PHI_CORE_ROOT, targets)
    reasoning_py = PHI_CORE_ROOT / "agents" / "reasoning.py"
    operator_py = PHI_CORE_ROOT / "agents" / "operator.py"
    offenders = [
        (p, ln) for p, ln in sites
        if not (p == reasoning_py or (p == operator_py and _enclosing_function_name(p, ln) == "_read_columns"))
    ]
    assert offenders == [], f"raw reader called outside its allowed sites: {offenders}"
    assert sites, "the scan itself found nothing"


# Behavioral: test_executor_dataset_read_happens_only_inside_sandbox_never_in_parent (Step 4, above).


# ---- invariant 3: HandoffGateway is live -----------------------------------


def test_handoff_call_sites_confined_to_manager_broker() -> None:
    """Exclusivity: ``HandoffGateway.handoff`` is only ever called from
    ``manager.py``'s guardian query broker (Step 6) and, since 2026-08-29
    (Phase 5/6 orchestrator follow-up item 1), ``agents/orchestrator.py``'s
    ``_run_regulations_expert``/``_run_phi_methods_expert_method`` -- the
    (RegulationsExpert, Judge) and (PHIMethodsExpert, Judge) finding-report
    edges. Phase 6 originally persisted those findings directly to the
    control store to avoid this exclusivity scan (see git history around
    commit 542f5af); this follow-up found ``HandoffGateway.ALLOWED_EDGES``/
    ``EDGE_SCHEMAS`` already registered both edges and their
    ``RegulatoryFinding``/``MethodFinding`` payload schemas (Wave R-c Step
    6 / Phase R-a), so the workaround was unnecessary -- the findings now
    travel the governed path like every other edge, and this scan's
    allowlist is widened by one file, not disabled. No other module hands
    off directly."""
    sites = _call_sites(PHI_CORE_ROOT, {"handoff"})
    allowed = {PHI_CORE_ROOT / "agents" / "manager.py", PHI_CORE_ROOT / "agents" / "orchestrator.py"}
    offenders = [(p, ln) for p, ln in sites if p not in allowed]
    assert offenders == [], f".handoff( called outside manager.py/orchestrator.py: {offenders}"
    assert sites, "the scan itself found nothing"

# Behavioral: test_manager_broker_edges_route_through_handoff_gateway_in_order (Step 6, above).


# ---- invariant 4: MethodRegistry is live -----------------------------------


def test_get_approved_methods_call_sites_confined_to_praxis_and_its_facade() -> None:
    """Exclusivity: ``get_approved_methods`` is only ever called from
    ``experts.py`` (PHIMethodsExpert's method-resolution gate, Step 5) or from
    ``context.py::StoreMethodRegistryReader.get_approved_methods`` (the
    Step 1 facade's own delegation to the free function of the same
    name -- the plumbing itself, not a second execution-time caller)."""
    sites = _call_sites(PHI_CORE_ROOT, {"get_approved_methods"})
    experts_py = PHI_CORE_ROOT / "agents" / "experts.py"
    context_py = PHI_CORE_ROOT / "control" / "context.py"
    offenders = [
        (p, ln) for p, ln in sites
        if not (p == experts_py or (p == context_py and _enclosing_function_name(p, ln) == "get_approved_methods"))
    ]
    assert offenders == [], f"get_approved_methods called outside its allowed sites: {offenders}"
    assert sites, "the scan itself found nothing"


# Behavioral: test_praxis_falls_back_when_recommended_method_is_only_researched_not_approved (Step 5, above).


# ---- invariant 5: authorize_capability is live -----------------------------


def test_authorize_capability_call_sites_confined_to_gateway() -> None:
    """Exclusivity: ``authorization.authorize_capability`` is only ever
    called from ``gateway.py``'s request validation (Step 7) --
    ``authorization.py`` itself only defines it."""
    sites = _call_sites(PHI_CORE_ROOT, {"authorize_capability"})
    allowed = PHI_CORE_ROOT / "control" / "gateway.py"
    offenders = [(p, ln) for p, ln in sites if p != allowed]
    assert offenders == [], f"authorize_capability called outside gateway.py: {offenders}"
    assert sites, "the scan itself found nothing"


# Behavioral: test_provider_gateway_routes_validation_through_authorization_service (Step 7, above).
