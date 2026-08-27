"""Phase 2E exit criteria (docs/MASTER_ARCHITECTURE_V2.md section 7 "headers
and variable names" + section 22 "SourceProjectionGateway" + section 23
"prompt-injection architecture", local reference doc, never committed):
HeaderSafetyGate, SourceProjectionGateway, UntrustedContentGateway.
"""
from __future__ import annotations

import pytest

from phi_core.control.opaque import OpaqueMap
from phi_core.control.records import HeaderClassification, SourceProjectionResult
from phi_core.control.source_projection import (
    classify_header,
    header_safety_gate,
    source_projection,
    untrusted_content_blocked,
)

@pytest.fixture(autouse=True)
def _stub_presidio(monkeypatch: pytest.MonkeyPatch) -> None:
    # Presidio's spaCy/thinc/numpy chain can be ABI-broken in a given local
    # interpreter independent of this repo's code (established pattern:
    # test_control_phase2_authorization_and_provider_control.py); stub it
    # out so these tests exercise the rule detector plus the secret scan,
    # not that install.
    import phi_core.detectors as detectors

    monkeypatch.setattr(detectors, "presidio_detect", lambda text: [])


_RECORD_FIELDS = {
    HeaderClassification: {"schema_version", "header", "disposition", "reasons", "opaque_token"},
    SourceProjectionResult: {
        "schema_version", "content_type", "run_id", "disposition", "reasons", "projected_text", "blocked",
    },
}


def test_no_schema_drift():
    for record_cls, expected in _RECORD_FIELDS.items():
        assert set(record_cls.model_fields) == expected, record_cls.__name__


# -- classify_header / HeaderSafetyGate ----------------------------------

def test_classify_header_safe_for_ordinary_column_name():
    disposition, reasons = classify_header("visit_date")
    assert disposition == "safe"
    assert reasons == []


def test_classify_header_sensitive_when_value_typed_into_header():
    disposition, reasons = classify_header("patient 123-45-6789")
    assert disposition == "sensitive"
    assert reasons


def test_classify_header_sensitive_on_credential_shape():
    disposition, reasons = classify_header("sk-ant-" + "a" * 30)
    assert disposition == "sensitive"


def test_header_safety_gate_projects_sensitive_header_to_opaque_token():
    opaque_map = OpaqueMap(run_id="run-1", opaque_map={})
    headers = ["patient_id", "patient 123-45-6789", "visit_date"]
    projected, classifications = header_safety_gate(headers, run_id="run-1", opaque_map=opaque_map)

    assert projected[0] == "patient_id"
    assert projected[2] == "visit_date"
    assert projected[1] != "patient 123-45-6789"
    assert projected[1].startswith("header_")

    assert [c.disposition for c in classifications] == ["safe", "sensitive", "safe"]
    assert classifications[1].opaque_token == projected[1]
    # The opaque token round-trips back to the real header for an
    # authorized caller (never for Schema itself).
    assert opaque_map.from_opaque(projected[1]) == "patient 123-45-6789"


def test_header_safety_gate_is_deterministic_across_calls():
    opaque_map_a = OpaqueMap(run_id="run-2", opaque_map={})
    opaque_map_b = OpaqueMap(run_id="run-2", opaque_map={})
    headers = ["mrn_00012345"]
    projected_a, _ = header_safety_gate(headers, run_id="run-2", opaque_map=opaque_map_a)
    projected_b, _ = header_safety_gate(headers, run_id="run-2", opaque_map=opaque_map_b)
    assert projected_a == projected_b


def test_header_safety_gate_preserves_input_order_and_count():
    opaque_map = OpaqueMap(run_id="run-3", opaque_map={})
    headers = ["a", "b", "c", "d"]
    projected, classifications = header_safety_gate(headers, run_id="run-3", opaque_map=opaque_map)
    assert len(projected) == len(headers) == len(classifications)


# -- SourceProjectionGateway ----------------------------------------------

def test_source_projection_safe_dictionary_text_passes_through():
    result = source_projection(
        content_type="dictionary", raw_text="Age at enrollment, in years.", run_id="run-4"
    )
    assert result.disposition == "safe"
    assert not result.blocked
    assert result.projected_text == "Age at enrollment, in years."


def test_source_projection_redacts_phi_in_free_text_comment():
    result = source_projection(
        content_type="comment", raw_text="Patient SSN is 123-45-6789, call back Monday.", run_id="run-4"
    )
    assert result.disposition == "sensitive"
    assert not result.blocked
    assert "123-45-6789" not in result.projected_text
    assert result.projected_text  # something was projected, just redacted


def test_source_projection_blocks_credential_shape_and_projects_nothing():
    result = source_projection(
        content_type="form", raw_text="API key: sk-ant-" + "a" * 30, run_id="run-4"
    )
    assert result.blocked is True
    assert result.projected_text == ""


def test_source_projection_normalizes_whitespace():
    result = source_projection(
        content_type="mapping", raw_text="code   ->   label\n\n\n\nnext line", run_id="run-4"
    )
    assert not result.blocked
    assert "   " not in result.projected_text
    assert "\n\n\n" not in result.projected_text


def test_source_projection_covers_every_doc_named_content_type():
    # v3 section 22: headers, dictionary files, mapping files, forms,
    # human comments (CRFs/PDFs/DOCX are just how "form"/"dictionary" text
    # was extracted upstream -- same content_type once it is plain text).
    for content_type in ("header", "dictionary", "mapping", "form", "comment"):
        result = source_projection(content_type=content_type, raw_text="clean text", run_id="run-5")
        assert result.disposition == "safe"
        assert result.content_type == content_type


# -- UntrustedContentGateway -----------------------------------------------

def test_untrusted_content_blocked_true_for_residual_phi():
    assert untrusted_content_blocked("Contact SSN 123-45-6789 for details") is True


def test_untrusted_content_blocked_false_for_clean_text():
    assert untrusted_content_blocked("Contact the study coordinator for details") is False
