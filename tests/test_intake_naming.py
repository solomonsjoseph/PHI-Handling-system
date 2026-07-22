"""Tests for phi_engine.pipeline.intake_naming -- the local, support-only
AI boundary that resolves an intake study name.

Every naming-content read and local-model dispatch happens through fakes:
no real Ollama server, no `config.get_llm_client()`. Behavior is driven
primarily through the public `resolve_intake_study` entry point and real
`FakeHTTPConnection`-backed `OfflineLocalLLMClient` transport; a small set
of pure-function reader/parser tests remain where they defend a genuine
boundary invariant (archive/zip-bomb, malformed-cell, field-limit bypass)
that is otherwise awkward to construct realistically end to end.
"""

from __future__ import annotations

import inspect
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

import openpyxl
import pytest
from reportlab.pdfgen import canvas

import phi_engine.pipeline.intake_naming as naming
import phi_engine.security.model_routing as routing
from phi_engine.config import config
from phi_engine.pipeline.intake_preflight import IntakeCandidate, IntakePreflight, inspect_intake_source
from phi_engine.pipeline.verified_source import FileIdentity
from phi_engine.utils import pipeline_lock

MODEL_SPEC = "qwen3:8b@sha256:" + "d" * 64
_THRESHOLD = config.PHI_CONFIDENCE_THRESHOLD
_ACCEPT_CONF = min(_THRESHOLD + 0.2, 1.0)
_REJECT_CONF = max(_THRESHOLD - 0.2, 0.0)


# --- shared fixture helpers ------------------------------------------------------------


def _local_config(**overrides):
    values = {
        "provider": "ollama",
        "models": (MODEL_SPEC,),
        "base_url": "http://127.0.0.1:11434",
        "allowed_base_urls": ("http://127.0.0.1:11434",),
        "offline_approved": True,
        "timeout_s": 2,
        "max_retries": 0,
    }
    values.update(overrides)
    return config.LocalLLMConfig(**values)


@dataclass
class FakeHTTPResponse:
    status: int
    payload: bytes

    def read(self, amount: int | None = None) -> bytes:
        return self.payload if amount is None else self.payload[:amount]

    def close(self) -> None:
        pass


class FakeHTTPConnection:
    responses: list[FakeHTTPResponse] = []
    requests: list[tuple[str, str, bytes | None, dict[str, str]]] = []

    def __init__(self, host: str, port: int, timeout: float):
        assert host in {"127.0.0.1", "::1"}
        self.host = host
        self.port = port
        self.timeout = timeout

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None):
        self.requests.append((method, path, body, headers or {}))

    def getresponse(self):
        return self.responses.pop(0)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_fake_http_connection():
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = []
    yield
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = []


def _tags_response() -> FakeHTTPResponse:
    payload = json.dumps({"models": [{"name": "qwen3:8b", "digest": "sha256:" + "d" * 64}]}, separators=(",", ":"))
    return FakeHTTPResponse(200, payload.encode())


def _generate_response(raw_text: str) -> FakeHTTPResponse:
    payload = json.dumps({"response": raw_text}, separators=(",", ":"))
    return FakeHTTPResponse(200, payload.encode())


def _decision_response(study_name: str | None, confidence: float) -> FakeHTTPResponse:
    inner = json.dumps({"study_name": study_name, "confidence": confidence}, separators=(",", ":"))
    return _generate_response(inner)


def _queue_dispatch(*decisions: tuple[str | None, float] | str) -> list[FakeHTTPResponse]:
    """Build a (tags, generate) response pair per dispatch call, in order.
    A plain string element queues that exact raw text as the model response
    (for malformed-output tests) instead of a well-formed decision."""

    responses: list[FakeHTTPResponse] = []
    for decision in decisions:
        responses.append(_tags_response())
        if isinstance(decision, str):
            responses.append(_generate_response(decision))
        else:
            responses.append(_decision_response(*decision))
    return responses


def _install_fake_client(monkeypatch, responses: list[FakeHTTPResponse], *, local_config=None) -> None:
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = list(responses)
    monkeypatch.setattr(routing.http.client, "HTTPConnection", FakeHTTPConnection)
    real_config = local_config or _local_config()
    monkeypatch.setattr(naming, "new_offline_local_client", lambda: routing.OfflineLocalLLMClient(real_config))


def _forbid_client(monkeypatch) -> None:
    def _raise() -> routing.OfflineLocalLLMClient:
        raise AssertionError("must not construct a local model client")

    monkeypatch.setattr(naming, "new_offline_local_client", _raise)


def _prompts_sent() -> list[str]:
    return [json.loads(body)["prompt"] for method, path, body, _ in FakeHTTPConnection.requests if path == "/api/generate"]


def _evidence_from_prompt(prompt: str) -> dict:
    assert prompt.startswith(naming._PROMPT_PREFIX)
    return json.loads(prompt[len(naming._PROMPT_PREFIX) :])


# --- source-tree builders ---------------------------------------------------------------


def _write_pdf(path: Path, *lines: str) -> None:
    """Each line becomes its own page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = canvas.Canvas(str(path))
    for line in lines:
        pdf.drawString(72, 750, line)
        pdf.showPage()
    pdf.save()


def _write_workbook(path: Path, rows: list[list[object]], *, sheet_names: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    names = sheet_names or ["Sheet1"]
    wb.active.title = names[0]
    for row in rows:
        wb.active.append(row)
    for name in names[1:]:
        wb.create_sheet(name)
    wb.save(path)


def _make_minimal_forms_source(root: Path, *lines: str) -> None:
    (root / "datasets").mkdir(parents=True)
    (root / "forms").mkdir(parents=True)
    _write_pdf(root / "forms" / "consent.pdf", *(lines or ("Consent form",)))


def _make_canonical_source(root: Path) -> None:
    (root / "datasets").mkdir(parents=True)
    (root / "forms").mkdir(parents=True)
    (root / "data_dictionary").mkdir(parents=True)
    (root / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    _write_pdf(root / "forms" / "consent.pdf", "StudyAlpha Consent Form")
    _write_workbook(root / "data_dictionary" / "dict.xlsx", [["StudyAlpha", "Dictionary"]])


def _synthetic_candidate(
    relative_path: str, *, source_component: str, component: str, size: int = 10
) -> IntakeCandidate:
    identity = FileIdentity(device=1, inode=hash(relative_path) & 0xFFFF, size=size, mtime_ns=1)
    return IntakeCandidate(
        relative_path=relative_path,
        source_component=source_component,
        component=component,
        identity=identity,
        sha256="0" * 64,
        sheet_count=None,
    )


# --- public contract ---------------------------------------------------------------------


def test_public_names_match_the_approved_contract():
    assert set(naming.__all__) == {"StudyNameSource", "StudyResolution", "resolve_intake_study", "canonical_source_root"}
    sig = inspect.signature(naming.resolve_intake_study)
    assert list(sig.parameters) == ["source", "preflight", "explicit_study", "support_confirmed_no_phi", "intake_root"]
    assert sig.parameters["explicit_study"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["support_confirmed_no_phi"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["intake_root"].kind is inspect.Parameter.KEYWORD_ONLY
    resolution_fields = set(inspect.signature(naming.StudyResolution).parameters)
    assert resolution_fields == {"name", "source", "review_items", "errors"}


# --- explicit study: skip naming entirely -------------------------------------------------


def test_explicit_study_skips_naming_and_reads_nothing(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_canonical_source(source)
    preflight = inspect_intake_source(source)
    _forbid_client(monkeypatch)
    monkeypatch.setattr(naming, "phi_gate_check", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no gate")))
    monkeypatch.setattr(naming, "open_verified_source", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no read")))

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study="ExplicitStudy", support_confirmed_no_phi=False, intake_root=tmp_path / "intake"
    )

    assert resolution == naming.StudyResolution(name="ExplicitStudy", source="user", review_items=(), errors=())


def test_explicit_study_ignores_support_confirmed_no_phi_flag(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_canonical_source(source)
    preflight = inspect_intake_source(source)
    _forbid_client(monkeypatch)

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study="ExplicitStudy", support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.source == "user"
    assert resolution.name == "ExplicitStudy"


def test_explicit_study_invalid_name_raises_value_error(tmp_path):
    preflight = IntakePreflight((), (), ())
    with pytest.raises(ValueError):
        naming.resolve_intake_study(
            tmp_path, preflight, explicit_study="../escape", support_confirmed_no_phi=False, intake_root=tmp_path
        )


# --- missing consent ----------------------------------------------------------------------


def test_missing_consent_generates_fallback_with_zero_reads_and_zero_calls(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_canonical_source(source)
    preflight = inspect_intake_source(source)
    _forbid_client(monkeypatch)
    monkeypatch.setattr(naming, "open_verified_source", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no read")))

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=False, intake_root=tmp_path / "intake"
    )

    assert resolution.source == "generated"
    assert re.fullmatch(r"study-[0-9a-f]{8}", resolution.name)
    assert resolution.review_items == ({"path": "", "reason": "support-phi-status-required", "blocking": True},)
    assert resolution.errors == ()


# --- exact lexical mismatch: the structural admission proof --------------------------------


def test_lexical_source_component_mismatch_is_never_admitted(tmp_path, monkeypatch):
    """A regression in `candidate.component == candidate.source_component`
    is the one bug that could let AI-eligible-looking candidates whose
    physical location disagrees with their logical classification reach
    naming evidence. None of these candidates require a real backing file
    because a correct admission filter rejects them before any I/O."""
    source = tmp_path / "source"
    source.mkdir()
    mismatched = [
        # logical component says "forms", but it physically lives under datasets/
        _synthetic_candidate("datasets/evil.pdf", source_component="datasets", component="forms"),
        # logical component says "data_dictionary", but it's lexically _unclassified
        _synthetic_candidate("_unclassified/evil.csv", source_component="_unclassified", component="data_dictionary"),
        # both individually supported values, but mismatched from each other
        _synthetic_candidate("mappings/evil.xlsx", source_component="mappings", component="data_dictionary"),
        _synthetic_candidate("data_dictionary/evil.csv", source_component="data_dictionary", component="mappings"),
    ]
    preflight = IntakePreflight(tuple(mismatched), (), ())
    opened: list[str] = []
    monkeypatch.setattr(naming, "open_verified_source", lambda src, rel, **kw: opened.append(rel) or (_ for _ in ()).throw(AssertionError))
    _forbid_client(monkeypatch)

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )

    assert opened == []
    assert resolution.source == "generated"


def test_datasets_and_unclassified_components_are_never_admission_eligible(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    candidates = [
        _synthetic_candidate("datasets/labs.csv", source_component="datasets", component="datasets"),
        _synthetic_candidate("_unclassified/odd.json", source_component="_unclassified", component="_unclassified"),
    ]
    preflight = IntakePreflight(tuple(candidates), (), ())
    _forbid_client(monkeypatch)
    monkeypatch.setattr(naming, "open_verified_source", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no read")))

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.source == "generated"


# --- support-only candidate guard: datasets/_unclassified never open for evidence --------


def test_dataset_and_unclassified_candidates_never_open_for_evidence(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_canonical_source(source)
    (source / "datasets" / "unsupported.json").write_text("{}", encoding="utf-8")
    preflight = inspect_intake_source(source)
    assert any(c.component == "_unclassified" for c in preflight.candidates)
    assert any(c.component == "datasets" for c in preflight.candidates)

    opened: list[str] = []
    real_open = naming.open_verified_source

    def spy_open(src, rel, **kw):
        opened.append(rel)
        return real_open(src, rel, **kw)

    monkeypatch.setattr(naming, "open_verified_source", spy_open)
    _install_fake_client(
        monkeypatch,
        _queue_dispatch(("StudyAlpha", _ACCEPT_CONF), ("StudyAlpha", _ACCEPT_CONF)),
    )

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )

    assert opened == sorted(opened)
    assert all(not p.startswith("datasets/") for p in opened)
    assert all(not p.startswith("_unclassified") for p in opened)
    for prompt in _prompts_sent():
        assert "SUBJID" not in prompt
        assert "labs.csv" not in prompt
        assert "unsupported.json" not in prompt
    assert resolution.name == "StudyAlpha"
    assert resolution.source == "ai"


# --- the demonstrated late-hardlink race ---------------------------------------------------


def test_late_dataset_hardlink_created_after_preflight_causes_zero_dispatch(tmp_path, monkeypatch):
    """Reproduces the audited race exactly: preflight admits a clean
    support candidate, then an attacker hardlinks a NEW datasets/ dirent to
    that SAME inode before naming runs. FileIdentity alone cannot see this
    (device/inode/size/mtime_ns are unchanged); only the fresh
    descriptor-relative datasets/ rescan can."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    _write_pdf(source / "forms" / "consent.pdf", "Alpha consent")
    alias = source / "data_dictionary" / "alias.csv"
    alias.write_text("SECRET_HEADER\nsecret-value\n", encoding="utf-8")

    preflight = inspect_intake_source(source)
    assert any(c.relative_path == "data_dictionary/alias.csv" for c in preflight.candidates)

    # The race: link a NEW dataset dirent to the SAME inode after preflight.
    os.link(alias, source / "datasets" / "late-alias.csv")

    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )

    assert FakeHTTPConnection.requests == []
    assert resolution.source == "generated"
    assert resolution.review_items == ({"path": "", "reason": "cross-component-hardlink", "blocking": True},)
    assert resolution.errors == ()
    dumped = json.dumps(resolution.review_items)
    assert "SECRET_HEADER" not in dumped and "secret-value" not in dumped


def test_late_hardlink_on_one_of_two_candidates_discards_all_evidence(tmp_path, monkeypatch):
    """A late hardlink on only ONE of several otherwise-clean candidates
    still discards everything, including the clean forms evidence -- not
    just the tampered candidate."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("SUBJID\n1\n", encoding="utf-8")
    _write_pdf(source / "forms" / "consent.pdf", "Clean forms content")
    tampered = source / "data_dictionary" / "tampered.csv"
    tampered.write_text("A,B\n1,2\n", encoding="utf-8")

    preflight = inspect_intake_source(source)
    os.link(tampered, source / "datasets" / "late.csv")

    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert FakeHTTPConnection.requests == []
    assert resolution.source == "generated"
    assert resolution.review_items == ({"path": "", "reason": "cross-component-hardlink", "blocking": True},)


def test_hardlink_recheck_before_dispatch_catches_a_race_after_collection(tmp_path, monkeypatch):
    """The SECOND checkpoint: a hardlink created AFTER evidence collection
    finished (but before dispatch) is caught by the pre-dispatch recheck,
    even though the per-candidate checkpoint during collection saw nothing
    wrong."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("SUBJID\n1\n", encoding="utf-8")
    _write_pdf(source / "forms" / "consent.pdf", "Alpha consent")
    dict_file = source / "data_dictionary" / "dict.csv"
    dict_file.write_text("A,B\n1,2\n", encoding="utf-8")
    preflight = inspect_intake_source(source)

    real_current_dataset_identities = naming._current_dataset_identities
    call_count = {"n": 0}

    def timed_identities(src):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call: the per-candidate checkpoint during collection --
            # nothing linked yet.
            return real_current_dataset_identities(src)
        # The pre-dispatch recheck (first call after collection) observes
        # the race that happened in between; only create the link once so
        # a second recheck call (e.g. before a later combined dispatch)
        # doesn't hit an already-existing dirent.
        raced_link = source / "datasets" / "raced.csv"
        if not raced_link.exists():
            os.link(dict_file, raced_link)
        return real_current_dataset_identities(src)

    monkeypatch.setattr(naming, "_current_dataset_identities", timed_identities)
    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert FakeHTTPConnection.requests == []
    assert resolution.source == "generated"
    assert resolution.review_items == ({"path": "", "reason": "cross-component-hardlink", "blocking": True},)


def test_symlinked_datasets_directory_after_preflight_fails_closed(tmp_path, monkeypatch):
    """The exact audited PoC: datasets/ is renamed and replaced by a
    directory symlink to the renamed tree after preflight. A fail-open
    scan would see this as "no dataset identities"; the shared
    intake_preflight._scan_component_identities primitive must instead
    report the scan itself as unsafe/inconclusive, which naming must
    treat identically to a confirmed hardlink alias."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("SUBJID\n1\n", encoding="utf-8")
    _write_pdf(source / "forms" / "consent.pdf", "Alpha consent")
    (source / "data_dictionary" / "dict.csv").write_text("A,B\n1,2\n", encoding="utf-8")
    preflight = inspect_intake_source(source)

    real_datasets = tmp_path / "elsewhere_datasets"
    os.rename(source / "datasets", real_datasets)
    (source / "datasets").symlink_to(real_datasets)

    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert FakeHTTPConnection.requests == []
    assert resolution.source == "generated"
    assert resolution.review_items == ({"path": "", "reason": "cross-component-hardlink", "blocking": True},)


def test_dataset_scan_error_fails_closed_not_open(tmp_path, monkeypatch):
    """Any scan failure -- not just a symlink -- must fail closed. Force
    the shared scan primitive to report an inconclusive result and prove
    naming never treats that as "zero dataset files"."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    _write_pdf(source / "forms" / "consent.pdf", "Alpha consent")
    preflight = inspect_intake_source(source)

    from phi_engine.pipeline import intake_preflight as preflight_module

    monkeypatch.setattr(preflight_module, "_scan_component_identities", lambda src, name: (None, "source-unreadable"))
    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert FakeHTTPConnection.requests == []
    assert resolution.source == "generated"
    assert resolution.review_items == ({"path": "", "reason": "cross-component-hardlink", "blocking": True},)


def test_client_first_call_creating_the_hardlink_gets_no_second_call(tmp_path, monkeypatch):
    """Exact reproduction of the between-dispatch disclosure: a client
    whose OWN first complete_bounded call creates a dict-to-datasets
    hardlink as a side effect must never receive a second call, and the
    dictionary content must never appear in any prompt actually sent."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("SUBJID\n1\n", encoding="utf-8")
    _write_pdf(source / "forms" / "consent.pdf", "Alpha consent")
    dict_file = source / "data_dictionary" / "dict.csv"
    dict_file.write_text("VAR,DESC\n1,2\n", encoding="utf-8")
    preflight = inspect_intake_source(source)

    sent_prompts: list[str] = []

    class RaceOnFirstCallClient:
        calls = 0

        def complete_bounded(self, prompt, *, max_output_tokens, max_response_bytes):
            RaceOnFirstCallClient.calls += 1
            sent_prompts.append(prompt)
            if RaceOnFirstCallClient.calls == 1:
                os.link(dict_file, source / "datasets" / "raced.csv")
            return json.dumps({"study_name": "StudyAlpha", "confidence": _ACCEPT_CONF})

    monkeypatch.setattr(naming, "new_offline_local_client", lambda: RaceOnFirstCallClient())
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert RaceOnFirstCallClient.calls == 1
    assert all("VAR" not in prompt and "DESC" not in prompt for prompt in sent_prompts)
    assert resolution.source == "generated"
    assert resolution.review_items == ({"path": "", "reason": "cross-component-hardlink", "blocking": True},)


# --- identity/hash drift: candidate excluded, zero dispatch when it's the only one -------


def test_identity_drift_after_preflight_excludes_candidate_and_records_error(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Original content")
    preflight = inspect_intake_source(source)

    # TOCTOU: file mutated after preflight computed identity/sha256.
    (source / "forms" / "consent.pdf").write_bytes(b"mutated-bytes-different-size-and-content")

    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )

    assert resolution.source == "generated"
    assert resolution.errors == ({"path": "forms/consent.pdf", "reason": "source-unreadable"},)


def test_hash_drift_with_matching_identity_is_rejected(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Original content")
    preflight = inspect_intake_source(source)
    candidate = preflight.candidates[0]
    # Poison the recorded sha256 while identity stays intact -- simulates a
    # defense-in-depth hash check catching something identity alone missed.
    poisoned = candidate.__class__(
        relative_path=candidate.relative_path,
        source_component=candidate.source_component,
        component=candidate.component,
        identity=candidate.identity,
        sha256="0" * 64,
        sheet_count=candidate.sheet_count,
    )
    poisoned_preflight = IntakePreflight((poisoned,), preflight.review_items, preflight.errors)

    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, poisoned_preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.source == "generated"
    assert resolution.errors == ({"path": "forms/consent.pdf", "reason": "source-unreadable"},)


def test_cross_component_hardlink_is_excluded_before_naming_ever_sees_it(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    dataset_file = source / "datasets" / "labs.csv"
    dataset_file.write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    os.link(dataset_file, source / "data_dictionary" / "aliased.csv")
    _write_pdf(source / "forms" / "consent.pdf", "hello")
    preflight = inspect_intake_source(source)
    assert any(item["reason"] == "cross-component-hardlink" for item in preflight.review_items)
    assert not any(c.component == "data_dictionary" for c in preflight.candidates)

    _install_fake_client(monkeypatch, _queue_dispatch((None, _REJECT_CONF), (None, _REJECT_CONF)))
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    for prompt in _prompts_sent():
        assert "SUBJID" not in prompt
        assert "labs.csv" not in prompt
        assert "aliased.csv" not in prompt
    assert resolution.source == "generated"


def test_symlink_naming_candidate_is_rejected_before_read(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    real = source / "data_dictionary" / "real.xlsx"
    _write_workbook(real, [["a", "b"]])
    _write_pdf(source / "forms" / "consent.pdf", "hello")
    preflight = inspect_intake_source(source)
    # Simulate a post-preflight symlink swap by handing naming a candidate
    # whose relative_path now points at a symlink placed after the fact.
    link = source / "data_dictionary" / "swapped.xlsx"
    link.symlink_to(real)
    candidate = next(c for c in preflight.candidates if c.component == "data_dictionary")
    swapped = candidate.__class__(
        relative_path="data_dictionary/swapped.xlsx",
        source_component="data_dictionary",
        component="data_dictionary",
        identity=candidate.identity,
        sha256=candidate.sha256,
        sheet_count=candidate.sheet_count,
    )
    poisoned_preflight = IntakePreflight((swapped,), (), ())

    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, poisoned_preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert any(item["reason"] == "source-symlink-not-allowed" for item in resolution.review_items)
    assert resolution.source == "generated"


# --- oversized document: skipped before open, blocking review ----------------------------


def test_oversized_document_is_never_opened_and_flags_support_evidence_limit(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "forms").mkdir(parents=True)
    big = source / "forms" / "huge.pdf"
    big.write_bytes(b"0" * (naming._MAX_DOCUMENT_BYTES + 1))
    preflight = inspect_intake_source(source)

    monkeypatch.setattr(naming, "open_verified_source", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not open")))
    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.review_items == ({"path": "forms/huge.pdf", "reason": "support-evidence-limit", "blocking": True},)
    assert resolution.source == "generated"


# --- descriptor lifecycle: reader closes before context exit -----------------------------


def test_descriptor_stays_verified_through_hash_and_parse(tmp_path, monkeypatch):
    """The verified descriptor must still be open (and identity-checked on
    exit) while the reader parses -- proven by mutating the file DURING a
    spied parser call and observing the resulting source-unreadable error
    from the post-read identity recheck, not a stale success."""
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Alpha consent")
    preflight = inspect_intake_source(source)
    pdf_path = source / "forms" / "consent.pdf"

    real_extract = naming._extract_pdf_pages

    def mutate_during_parse(stream):
        pages = real_extract(stream)
        pdf_path.write_bytes(b"mutated-during-parse-window")
        return pages

    monkeypatch.setattr(naming, "_extract_pdf_pages", mutate_during_parse)
    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.source == "generated"
    assert resolution.errors == ({"path": "forms/consent.pdf", "reason": "source-unreadable"},)


# --- exact readers/order/canonical JSON (public behavior) ---------------------------------


def test_forms_evidence_is_ordered_bounded_and_canonical(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    _write_pdf(source / "forms" / "a_first.pdf", "Alpha page one text")
    _write_pdf(source / "forms" / "z_second.pdf", "Zeta page one", "Zeta page two", "Zeta page three")
    preflight = inspect_intake_source(source)

    _install_fake_client(monkeypatch, _queue_dispatch(("StudyAlpha", _ACCEPT_CONF)))
    naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )

    prompts = _prompts_sent()
    assert len(prompts) == 1  # accepted on the first (forms) query -> no dict/combined dispatch
    assert prompts[0] == naming._PROMPT_PREFIX + (
        '{"component":"forms","documents":['
        '{"index":1,"pages":["Alpha page one text"]},'
        '{"index":2,"pages":["Zeta page one","Zeta page two"]}]}'
    )
    evidence = _evidence_from_prompt(prompts[0])
    dumped = json.dumps(evidence)
    assert "a_first.pdf" not in dumped and "z_second.pdf" not in dumped and "forms/" not in dumped


def test_dictionary_mapping_evidence_exact_canonical_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    (source / "data_dictionary" / "dict.csv").write_text("VAR,DESC\nAGE,Age in years\n", encoding="utf-8")
    preflight = inspect_intake_source(source)

    _install_fake_client(monkeypatch, _queue_dispatch(("StudyAlpha", _ACCEPT_CONF)))
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )

    prompts = _prompts_sent()
    assert len(prompts) == 1
    assert prompts[0] == naming._PROMPT_PREFIX + (
        '{"component":"dictionary_mapping","documents":'
        '[{"index":1,"kind":"data_dictionary","sheets":'
        '[{"index":1,"rows":[["VAR","DESC"],["AGE","Age in years"]]}]}]}'
    )
    assert resolution.name == "StudyAlpha"
    assert resolution.source == "ai"


def test_dictionary_mapping_evidence_mixes_csv_and_xlsx_with_kind(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    (source / "mappings").mkdir(parents=True)
    (source / "data_dictionary" / "dict.csv").write_text("VAR,DESC\nAGE,Age in years\n", encoding="utf-8")
    _write_workbook(source / "mappings" / "map.xlsx", [["code", "label"], ["1", "male"]])
    preflight = inspect_intake_source(source)

    _install_fake_client(monkeypatch, _queue_dispatch(("StudyAlpha", _ACCEPT_CONF)))
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )

    prompts = _prompts_sent()
    assert len(prompts) == 1
    evidence = _evidence_from_prompt(prompts[0])
    assert evidence["component"] == "dictionary_mapping"
    # POSIX candidate order: data_dictionary/ sorts before mappings/
    assert [d["kind"] for d in evidence["documents"]] == ["data_dictionary", "mappings"]
    assert evidence["documents"][0]["sheets"][0]["rows"] == [["VAR", "DESC"], ["AGE", "Age in years"]]
    xlsx_rows = evidence["documents"][1]["sheets"][0]["rows"]
    assert [row[:2] for row in xlsx_rows] == [["code", "label"], ["1", "male"]]
    assert all(len(row) == 20 for row in xlsx_rows)  # openpyxl's own max_col=20 window, padded
    assert all(cell == "" for row in xlsx_rows for cell in row[2:])
    assert resolution.name == "StudyAlpha"
    assert resolution.source == "ai"


def test_combined_evidence_exact_canonical_snapshot_forms_first(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    _write_pdf(source / "forms" / "consent.pdf", "Alpha consent")
    (source / "data_dictionary" / "dict.csv").write_text("VAR,DESC\n", encoding="utf-8")
    preflight = inspect_intake_source(source)
    _install_fake_client(
        monkeypatch, _queue_dispatch((None, _REJECT_CONF), (None, _REJECT_CONF), ("StudyCombined", _ACCEPT_CONF))
    )

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    prompts = _prompts_sent()
    assert len(prompts) == 3
    assert prompts[2] == naming._PROMPT_PREFIX + (
        '{"component":"combined",'
        '"dictionary_mapping":[{"index":1,"kind":"data_dictionary","sheets":[{"index":1,"rows":[["VAR","DESC"]]}]}],'
        '"forms":[{"index":1,"pages":["Alpha consent"]}]}'
    )
    assert resolution.name == "StudyCombined"
    assert resolution.source == "ai"


def test_xlsx_windows_to_four_sheets_twenty_rows_twenty_cols():
    rows = [[f"r{r}c{c}" for c in range(25)] for r in range(25)]
    buf = io.BytesIO()
    wb = openpyxl.Workbook()
    wb.active.title = "S1"
    for row in rows:
        wb.active.append(row)
    for extra in ("S2", "S3", "S4", "S5"):
        ws = wb.create_sheet(extra)
        ws.append(["x"])
    wb.save(buf)

    sheets = naming._extract_xlsx_sheets(io.BytesIO(buf.getvalue()))
    assert [index for index, _ in sheets] == [1, 2, 3, 4]  # only first 4 sheets
    first_index, first_rows = sheets[0]
    assert len(first_rows) == 20  # only first 20 rows
    assert len(first_rows[0]) == 20  # only first 20 cols

def test_xlsx_max_col_bounds_the_reader_itself_not_a_post_slice():
    """Behaviorally proves the allocation contract, not just the output
    shape: a 25-column worksheet must never let openpyxl materialize more
    than 20 columns before naming ever sees the row."""
    from openpyxl.worksheet._read_only import ReadOnlyWorksheet

    rows = [[f"r{r}c{c}" for c in range(25)] for r in range(25)]
    buf = io.BytesIO()
    wb = openpyxl.Workbook()
    for row in rows:
        wb.active.append(row)
    wb.save(buf)

    observed_kwargs: dict = {}
    real_iter_rows = ReadOnlyWorksheet.iter_rows

    def spy_iter_rows(self, *args, **kwargs):
        observed_kwargs.update(kwargs)
        return real_iter_rows(self, *args, **kwargs)

    ReadOnlyWorksheet.iter_rows = spy_iter_rows
    try:
        sheets = naming._extract_xlsx_sheets(io.BytesIO(buf.getvalue()))
    finally:
        ReadOnlyWorksheet.iter_rows = real_iter_rows

    assert observed_kwargs.get("min_col") == 1
    assert observed_kwargs.get("max_col") == 20
    assert len(sheets[0][1][0]) == 20


def test_pdf_reader_caps_at_two_pages_in_order():
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    for text in ("Page One", "Page Two", "Page Three"):
        pdf.drawString(72, 720, text)
        pdf.showPage()
    pdf.save()
    pages = naming._extract_pdf_pages(io.BytesIO(buf.getvalue()))
    assert len(pages) == 2
    assert pages[0].strip().startswith("Page One")
    assert pages[1].strip().startswith("Page Two")


# --- archive/source/cell/field/parser boundaries -------------------------------------------


def test_fragment_codepoint_limit_rejects_rather_than_truncates():
    long_text = "a" * (naming._MAX_FRAGMENT_CODEPOINTS + 1)
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf)
    pdf.drawString(10, 750, long_text)
    pdf.save()
    with pytest.raises(naming._EvidenceLimitError):
        naming._extract_pdf_pages(io.BytesIO(buf.getvalue()))


def test_csv_field_byte_limit_is_enforced():
    huge_field = "x" * (naming._MAX_CSV_FIELD_BYTES + 1)
    data = f"A,B\n{huge_field},ok\n".encode("utf-8")
    with pytest.raises(naming._EvidenceLimitError):
        naming._extract_csv_rows(io.BytesIO(data))


def test_csv_column_21_cannot_bypass_the_field_byte_limit():
    """A field beyond the retained 20 columns must still be validated --
    proves the byte check runs over every parsed field before slicing,
    not only the ones that survive to the output."""
    huge_field = "x" * (naming._MAX_CSV_FIELD_BYTES + 1)
    row = ",".join(["a"] * 20 + [huge_field])
    data = (row + "\n").encode("utf-8")
    with pytest.raises(naming._EvidenceLimitError):
        naming._extract_csv_rows(io.BytesIO(data))


def test_csv_row_and_column_windowing():
    header = ",".join(f"c{i}" for i in range(25))
    lines = [header] + [",".join(f"v{r}{i}" for i in range(25)) for r in range(25)]
    data = ("\n".join(lines) + "\n").encode("utf-8")
    rows = naming._extract_csv_rows(io.BytesIO(data))
    assert len(rows) == 20
    assert all(len(row) == 20 for row in rows)


def test_invalid_utf8_csv_is_evidence_limit_not_a_crash():
    with pytest.raises(naming._EvidenceLimitError):
        naming._extract_csv_rows(io.BytesIO(b"\xff\xfe\x00bad-utf8"))


def test_csv_field_size_limit_is_restored_after_parsing():
    original = __import__("csv").field_size_limit()
    try:
        naming._extract_csv_rows(io.BytesIO(b"a,b\n1,2\n"))
        assert __import__("csv").field_size_limit() == original
    finally:
        __import__("csv").field_size_limit(original)


def test_csv_field_size_limit_is_restored_after_an_exception():
    """The success-path restoration test cannot catch exception-only
    leakage of the process-global csv.field_size_limit -- trigger
    _EvidenceLimitError via an oversized field and assert the ambient
    value was still restored."""
    import csv as csv_module

    original = csv_module.field_size_limit()
    huge_field = "x" * (naming._MAX_CSV_FIELD_BYTES + 1)
    data = f"A,B\n{huge_field},ok\n".encode("utf-8")
    try:
        with pytest.raises(naming._EvidenceLimitError):
            naming._extract_csv_rows(io.BytesIO(data))
        assert csv_module.field_size_limit() == original
    finally:
        csv_module.field_size_limit(original)


def test_csv_field_size_limit_is_restored_after_invalid_utf8():
    import csv as csv_module

    original = csv_module.field_size_limit()
    try:
        with pytest.raises(naming._EvidenceLimitError):
            naming._extract_csv_rows(io.BytesIO(b"\xff\xfe\x00bad-utf8"))
        assert csv_module.field_size_limit() == original
    finally:
        csv_module.field_size_limit(original)


def test_zip_bomb_ratio_is_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", "0" * (10 * 1024 * 1024))
    with pytest.raises(naming._EvidenceLimitError):
        naming._extract_xlsx_sheets(io.BytesIO(buf.getvalue()))


def test_malformed_zip_is_evidence_limit():
    with pytest.raises(naming._EvidenceLimitError):
        naming._extract_xlsx_sheets(io.BytesIO(b"not a zip file at all"))


def test_zip_central_directory_overrun_is_rejected():
    """Independent of any single member's declared size: a directory with
    many small members can itself exceed max_zip_directory_bytes before
    any member is ever opened."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(2048):
            zf.writestr(f"member_{i}_" + "x" * 50 + ".txt", "0")
    data = buf.getvalue()
    with pytest.raises(naming._EvidenceLimitError):
        naming._extract_xlsx_sheets(io.BytesIO(data))


def test_xlsx_lazy_worksheet_iteration_failure_is_evidence_limit_not_a_leak():
    """Reproduces the audited leak: a well-formed-looking workbook whose
    cell reference is malformed fails during LAZY worksheet iteration
    (not during load_workbook itself), and must still collapse to the
    fixed code -- never a raw ValueError with the malformed content
    embedded in its message."""
    buf = io.BytesIO()
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    wbrels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    sentinel = "PHI123-45-6789"
    sheet1 = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData><row r="1"><c r="{sentinel}" t="str"><v>bad</v></c></row></sheetData>'
        "</worksheet>"
    )
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wbrels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1)
    data = buf.getvalue()

    try:
        naming._extract_xlsx_sheets(io.BytesIO(data))
        pytest.fail("expected _EvidenceLimitError")
    except naming._EvidenceLimitError:
        pass
    except Exception as exc:  # pragma: no cover -- explicit anti-leak assertion
        pytest.fail(f"raw exception leaked instead of _EvidenceLimitError: {type(exc).__name__}: {exc}")


def test_malformed_xlsx_end_to_end_produces_fixed_review_not_a_crash(tmp_path, monkeypatch):
    """A malformed cell reference lives inside worksheet XML, which
    preflight's own workbook.xml-only sheet count never inspects -- this
    candidate reaches naming as a clean-looking admitted data_dictionary
    file, and only naming's fuller openpyxl load+iterate trips over it."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        "</Types>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    wbrels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    sheet1 = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row r="1"><c r="PHI123-45-6789" t="str"><v>bad</v></c></row></sheetData>'
        "</worksheet>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wbrels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1)
    (source / "data_dictionary" / "malformed.xlsx").write_bytes(buf.getvalue())
    preflight = inspect_intake_source(source)
    # Confirms the fixture premise: preflight's workbook.xml-only sheet
    # count admits this as a clean data_dictionary candidate.
    assert any(
        c.relative_path == "data_dictionary/malformed.xlsx" and c.component == "data_dictionary"
        for c in preflight.candidates
    )

    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.source == "generated"
    assert resolution.review_items == (
        {"path": "data_dictionary/malformed.xlsx", "reason": "support-evidence-limit", "blocking": True},
    )
    dumped = json.dumps(resolution.review_items)
    assert "PHI123-45-6789" not in dumped


# --- maximal truncation boundary for all three payload classes -----------------------------


def test_forms_truncation_boundary_is_maximal_not_just_within_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(naming, "_MAX_EVIDENCE_BYTES", 90)
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    _write_pdf(source / "forms" / "a_doc.pdf", "AAAAAAAAAA")
    _write_pdf(source / "forms" / "b_doc.pdf", "BBBBBBBBBB")
    _write_pdf(source / "forms" / "c_doc.pdf", "CCCCCCCCCC")
    preflight = inspect_intake_source(source)
    _install_fake_client(monkeypatch, _queue_dispatch((None, 0.0)))
    naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    prompts = _prompts_sent()
    evidence = _evidence_from_prompt(prompts[0])
    encoded = prompts[0][len(naming._PROMPT_PREFIX) :].encode("utf-8")
    assert len(encoded) <= 90

    # Maximality: appending the next ordered fragment (proven by
    # reconstructing it through the module's own payload builder) exceeds
    # the cap -- this is not an arbitrary early stop.
    kept_docs = {d["index"]: list(d["pages"]) for d in evidence["documents"]}
    next_index = (max(kept_docs) if kept_docs else 0) + 1
    trial = {k: list(v) for k, v in kept_docs.items()}
    trial.setdefault(next_index, []).append("D" * 10)
    trial_payload = {"component": "forms", "documents": [{"index": i, "pages": trial[i]} for i in sorted(trial)]}
    assert len(json.dumps(trial_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 90


def test_dictionary_truncation_boundary_is_maximal(tmp_path, monkeypatch):
    monkeypatch.setattr(naming, "_MAX_EVIDENCE_BYTES", 140)
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    (source / "data_dictionary" / "a_dict.csv").write_text("AAAA,BBBB\n1111,2222\n3333,4444\n", encoding="utf-8")
    preflight = inspect_intake_source(source)
    _install_fake_client(monkeypatch, _queue_dispatch((None, 0.0)))
    naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    prompts = _prompts_sent()
    evidence = _evidence_from_prompt(prompts[0])
    encoded = prompts[0][len(naming._PROMPT_PREFIX) :].encode("utf-8")
    assert len(encoded) <= 140
    kept_rows = evidence["documents"][0]["sheets"][0]["rows"] if evidence["documents"] else []
    all_rows = [["AAAA", "BBBB"], ["1111", "2222"], ["3333", "4444"]]
    assert kept_rows == all_rows[: len(kept_rows)]
    assert len(kept_rows) < len(all_rows)  # budget genuinely truncated something
    trial_rows = kept_rows + [all_rows[len(kept_rows)]]
    trial_payload = {
        "component": "dictionary_mapping",
        "documents": [{"index": 1, "kind": "data_dictionary", "sheets": [{"index": 1, "rows": trial_rows}]}],
    }
    assert len(json.dumps(trial_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 140


def test_combined_truncation_boundary_is_maximal_forms_then_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(naming, "_MAX_EVIDENCE_BYTES", 160)
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    _write_pdf(source / "forms" / "consent.pdf", "FORMSFORMSFORMS")
    (source / "data_dictionary" / "dict.csv").write_text("DICTDICT,ROWROW\n1,2\n3,4\n", encoding="utf-8")
    preflight = inspect_intake_source(source)
    _install_fake_client(monkeypatch, _queue_dispatch((None, 0.0), (None, 0.0), (None, 0.0)))
    naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    prompts = _prompts_sent()
    assert len(prompts) == 3
    encoded = prompts[2][len(naming._PROMPT_PREFIX) :].encode("utf-8")
    assert len(encoded) <= 160
    evidence = json.loads(encoded)
    assert evidence["component"] == "combined"
    # forms-first ordering: forms content is never sacrificed for dict
    # content (forms fragments are appended before any dict fragment) --
    # with this budget, forms alone already consumes enough of the shared
    # cap that dict is excluded entirely, not partially.
    assert evidence["forms"] == [{"index": 1, "pages": ["FORMSFORMSFORMS"]}]
    assert evidence["dictionary_mapping"] == []
    # Maximality: appending even the FIRST dict row would exceed the cap.
    trial = dict(evidence)
    trial["dictionary_mapping"] = [
        {"index": 1, "kind": "data_dictionary", "sheets": [{"index": 1, "rows": [["DICTDICT", "ROWROW"]]}]}
    ]
    assert len(json.dumps(trial, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > 160


# --- descriptor OSError normalization -------------------------------------------------------


def test_descriptor_dup_failure_is_normalized_to_source_unreadable(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Alpha consent")
    preflight = inspect_intake_source(source)

    def failing_dup(fd):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(naming.os, "dup", failing_dup)
    _forbid_client(monkeypatch)
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.source == "generated"
    assert resolution.errors == ({"path": "forms/consent.pdf", "reason": "source-unreadable"},)
    for err in resolution.errors:
        assert "Too many open files" not in json.dumps(err)


# --- no dataset evidence -------------------------------------------------------------------


def test_combined_payload_never_includes_dataset_component():
    combined = naming._combined_payload_dict({1: ["form text"]}, {1: ("data_dictionary", {1: [["header"]]})})
    assert combined["component"] == "combined"
    assert set(combined) == {"component", "forms", "dictionary_mapping"}
    assert "datasets" not in json.dumps(combined)


# --- local-only routing -------------------------------------------------------------------


def test_never_calls_ordinary_llm_client(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_canonical_source(source)
    preflight = inspect_intake_source(source)
    monkeypatch.setattr(config, "get_llm_client", lambda: (_ for _ in ()).throw(AssertionError("must not be called")))
    _install_fake_client(monkeypatch, _queue_dispatch((None, _REJECT_CONF), (None, _REJECT_CONF)))
    naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )  # no assertion error raised == get_llm_client never touched


def test_dispatch_uses_real_complete_bounded_with_128_tokens_4096_bytes(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Alpha consent")
    preflight = inspect_intake_source(source)
    _install_fake_client(monkeypatch, _queue_dispatch(("StudyAlpha", _ACCEPT_CONF)))

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.name == "StudyAlpha"
    generate_body = json.loads(FakeHTTPConnection.requests[1][2])
    assert generate_body["options"] == {"num_predict": 128}


# --- PHI gate on evidence, before dispatch --------------------------------------------------


def test_phi_bearing_evidence_blocks_before_any_dispatch(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Patient SSN is 123-45-6789 please verify")
    preflight = inspect_intake_source(source)
    _install_fake_client(monkeypatch, [])  # any request at all is a bug -> IndexError on empty queue

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )

    assert FakeHTTPConnection.requests == []
    assert resolution.source == "generated"
    assert resolution.review_items == ({"path": "", "reason": "possible-phi-requires-study", "blocking": True},)
    dumped = json.dumps(resolution.review_items)
    assert "123-45-6789" not in dumped


# --- guard_llm_output on the response -------------------------------------------------------


def test_phi_bearing_model_output_is_an_inspection_failure_not_a_leak(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Alpha consent")
    preflight = inspect_intake_source(source)
    responses = _queue_dispatch('{"study_name":"123-45-6789","confidence":0.99}', (None, 0.0))
    _install_fake_client(monkeypatch, responses)

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )

    assert resolution.errors == ({"path": None, "reason": "study-name-inspection-failed"},)
    assert resolution.source == "generated"
    dumped = json.dumps(resolution.errors) + json.dumps(resolution.review_items) + resolution.name
    assert "123-45-6789" not in dumped


# --- strict output schema validation --------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "",
        "```json\n{\"study_name\":\"X\",\"confidence\":0.9}\n```",  # fencing must be rejected, not unwrapped
        '{"study_name":"X","confidence":0.9} trailing text',
        '{"study_name":"X"}',  # missing confidence
        '{"study_name":"X","confidence":0.9,"extra":1}',  # extra key
        '{"study_name":1,"confidence":0.9}',  # wrong type
        '{"study_name":"X","confidence":"0.9"}',  # confidence as string
        '{"study_name":"X","confidence":true}',  # boolean confidence
        '{"study_name":"X","confidence":1.5}',  # out of range
        '{"study_name":"X","confidence":-0.1}',  # out of range
        '{"study_name":"X","confidence":NaN}',  # nonfinite
        '{"study_name":"X","confidence":Infinity}',  # nonfinite
        '{"study_name":"X","confidence":0.9,"study_name":"Y"}',  # duplicate key
        '[1,2,3]',  # not an object
        '"just a string"',
    ],
)
def test_strict_output_schema_rejects_every_malformed_shape(raw):
    with pytest.raises(naming._InspectionFailed):
        naming._parse_model_output(raw)


def test_strict_output_schema_accepts_null_study_name():
    study_name, confidence = naming._parse_model_output('{"study_name":null,"confidence":0.42}')
    assert study_name is None
    assert confidence == pytest.approx(0.42)


def test_strict_output_schema_accepts_valid_string_and_bounds():
    study_name, confidence = naming._parse_model_output('{"study_name":"StudyAlpha","confidence":1}')
    assert study_name == "StudyAlpha"
    assert confidence == 1.0


def test_oversized_transport_envelope_end_to_end_is_inspection_failed(tmp_path, monkeypatch):
    """A >4096-byte /api/generate envelope end to end (real transport
    ceiling, not a stubbed client) collapses to the fixed code."""
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Alpha consent")
    preflight = inspect_intake_source(source)
    name, digest = MODEL_SPEC.split("@", 1)
    oversized = json.dumps({"response": "x" * 5000}).encode()
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = [
        FakeHTTPResponse(200, json.dumps({"models": [{"name": name, "digest": digest}]}).encode()),
        FakeHTTPResponse(200, oversized),
    ]
    monkeypatch.setattr(routing.http.client, "HTTPConnection", FakeHTTPConnection)
    monkeypatch.setattr(naming, "new_offline_local_client", lambda: routing.OfflineLocalLLMClient(_local_config()))

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.errors == (
        {"path": None, "reason": "study-name-inspection-failed"},
        {"path": None, "reason": "study-name-inspection-failed"},
    )
    assert resolution.source == "generated"


def test_oversized_or_malformed_model_response_is_inspection_failed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Alpha consent")
    preflight = inspect_intake_source(source)
    responses = _queue_dispatch("not-json-at-all", (None, 0.0))
    _install_fake_client(monkeypatch, responses)

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.errors == ({"path": None, "reason": "study-name-inspection-failed"},)
    assert resolution.source == "generated"


def test_local_transport_failure_is_inspection_failed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Alpha consent")
    preflight = inspect_intake_source(source)
    FakeHTTPConnection.requests = []
    FakeHTTPConnection.responses = [FakeHTTPResponse(500, b"")]
    monkeypatch.setattr(routing.http.client, "HTTPConnection", FakeHTTPConnection)
    client = routing.OfflineLocalLLMClient(_local_config(max_retries=0))
    monkeypatch.setattr(naming, "new_offline_local_client", lambda: client)

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.errors == (
        {"path": None, "reason": "study-name-inspection-failed"},
        {"path": None, "reason": "study-name-inspection-failed"},
    )
    assert resolution.source == "generated"


def test_local_endpoint_and_digest_controls_still_enforced(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Alpha consent")
    preflight = inspect_intake_source(source)
    # offline_approved=False must fail closed before any transport happens.
    bad_client = routing.OfflineLocalLLMClient(_local_config(offline_approved=False))
    monkeypatch.setattr(naming, "new_offline_local_client", lambda: bad_client)

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.errors == (
        {"path": None, "reason": "study-name-inspection-failed"},
        {"path": None, "reason": "study-name-inspection-failed"},
    )
    assert resolution.source == "generated"


def test_local_client_configuration_failure_is_inspection_failed(tmp_path, monkeypatch):
    """The public client factory's own config-loading failure
    (LocalLLMConfigurationError) must not escape resolve_intake_study."""
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Alpha consent")
    preflight = inspect_intake_source(source)

    def broken_factory():
        raise config.LocalLLMConfigurationError("bad local_llm config")

    monkeypatch.setattr(naming, "new_offline_local_client", broken_factory)
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.source == "generated"
    for err in resolution.errors:
        assert err["reason"] == "study-name-inspection-failed"
        assert "bad local_llm config" not in json.dumps(err)


# --- separate / matching / conflicting / combined / null decisions -------------------------


def test_forms_only_accepted_is_used_without_dict_query(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    _write_pdf(source / "forms" / "consent.pdf", "Alpha consent")
    preflight = inspect_intake_source(source)
    _install_fake_client(monkeypatch, _queue_dispatch(("StudyAlpha", _ACCEPT_CONF)))
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.name == "StudyAlpha"
    assert resolution.source == "ai"
    assert len(_prompts_sent()) == 1


def test_dictionary_only_accepted_is_used_symmetrically(tmp_path, monkeypatch):
    """Symmetric to the forms-only branch: dictionary_mapping alone can be
    accepted and used as the final name with no forms evidence at all."""
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    (source / "data_dictionary" / "dict.csv").write_text("VAR,DESC\n", encoding="utf-8")
    preflight = inspect_intake_source(source)
    _install_fake_client(monkeypatch, _queue_dispatch(("StudyDict", _ACCEPT_CONF)))
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.name == "StudyDict"
    assert resolution.source == "ai"
    assert len(_prompts_sent()) == 1


def test_matching_candidates_preserve_forms_spelling(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    _write_pdf(source / "forms" / "consent.pdf", "Alpha consent")
    _write_workbook(source / "data_dictionary" / "dict.xlsx", [["dict"]])
    preflight = inspect_intake_source(source)
    _install_fake_client(monkeypatch, _queue_dispatch(("StudyAlpha", _ACCEPT_CONF), ("STUDYALPHA", _ACCEPT_CONF)))

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.name == "StudyAlpha"  # forms spelling preserved despite dict differing only in case
    assert resolution.source == "ai"
    assert resolution.review_items == ()


def test_conflicting_candidates_block_with_conflict_review_and_generated_fallback(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    _write_pdf(source / "forms" / "consent.pdf", "Alpha consent")
    _write_workbook(source / "data_dictionary" / "dict.xlsx", [["dict"]])
    preflight = inspect_intake_source(source)
    _install_fake_client(monkeypatch, _queue_dispatch(("StudyAlpha", _ACCEPT_CONF), ("StudyBeta", _ACCEPT_CONF)))

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.source == "generated"
    assert resolution.review_items == (
        {
            "path": "",
            "reason": "study-name-conflict",
            "blocking": True,
            "candidates": {"forms": "StudyAlpha", "dictionary_mapping": "StudyBeta"},
        },
    )


def test_neither_accepted_falls_back_to_combined_query(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "forms").mkdir(parents=True)
    (source / "data_dictionary").mkdir(parents=True)
    _write_pdf(source / "forms" / "consent.pdf", "Alpha consent")
    _write_workbook(source / "data_dictionary" / "dict.xlsx", [["dict"]])
    preflight = inspect_intake_source(source)
    _install_fake_client(
        monkeypatch,
        _queue_dispatch((None, _REJECT_CONF), (None, _REJECT_CONF), ("StudyCombined", _ACCEPT_CONF)),
    )

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    prompts = _prompts_sent()
    assert len(prompts) == 3
    combined_evidence = _evidence_from_prompt(prompts[2])
    assert combined_evidence["component"] == "combined"
    assert set(combined_evidence) == {"component", "forms", "dictionary_mapping"}
    assert resolution.name == "StudyCombined"
    assert resolution.source == "ai"


def test_clean_low_confidence_and_null_result_means_generated_fallback(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Alpha consent")
    preflight = inspect_intake_source(source)
    # forms low-confidence, no dict evidence -> combined re-queried on the
    # same (only) forms fragments, also clean-null -> generated, no errors.
    _install_fake_client(monkeypatch, _queue_dispatch((None, _REJECT_CONF), (None, 0.0)))

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.source == "generated"
    assert resolution.errors == ()
    assert resolution.review_items == ()


def test_exact_threshold_confidence_is_accepted(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Alpha consent")
    preflight = inspect_intake_source(source)
    _install_fake_client(monkeypatch, _queue_dispatch(("StudyAlpha", _THRESHOLD)))
    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.name == "StudyAlpha"
    assert resolution.source == "ai"


def test_combined_dispatch_skipped_when_zero_evidence_after_consent(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "datasets").mkdir(parents=True)
    (source / "datasets" / "labs.csv").write_text("SUBJID\n1\n", encoding="utf-8")
    preflight = inspect_intake_source(source)  # no forms/, no dict/mappings admitted
    _forbid_client(monkeypatch)

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    assert resolution.source == "generated"
    assert FakeHTTPConnection.requests == []


# --- random fallback -------------------------------------------------------------------------


def test_generated_name_format_and_validity():
    name = naming._generate_study_name()
    assert re.fullmatch(r"study-[0-9a-f]{8}", name)
    pipeline_lock.lock_path_for(name)  # must not raise


def test_generated_names_are_not_constant():
    names = {naming._generate_study_name() for _ in range(20)}
    assert len(names) > 1


# --- fixed, value-free error/review vocabulary ------------------------------------------------


def test_review_and_error_records_never_carry_raw_content_or_paths_beyond_source_relative(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _make_minimal_forms_source(source, "Patient SSN is 123-45-6789")
    preflight = inspect_intake_source(source)
    _install_fake_client(monkeypatch, [])

    resolution = naming.resolve_intake_study(
        source, preflight, explicit_study=None, support_confirmed_no_phi=True, intake_root=tmp_path / "intake"
    )
    for item in resolution.review_items:
        assert set(item) <= {"path", "reason", "blocking", "detail", "candidates"}
        assert isinstance(item["reason"], str)
    for item in resolution.errors:
        assert set(item) <= {"path", "reason", "detail"}


# --- canonical_source_root: pure, no I/O ------------------------------------------------------


def test_canonical_source_root_is_pure_path_resolution(tmp_path):
    target = tmp_path / "does-not-exist" / "still-not-created"
    result = naming.canonical_source_root(target)
    assert result == str(target.resolve())
    assert not target.exists()  # never created anything


def test_canonical_source_root_used_consistently_for_same_path(tmp_path):
    a = naming.canonical_source_root(tmp_path / "x")
    b = naming.canonical_source_root(tmp_path / "x")
    assert a == b
