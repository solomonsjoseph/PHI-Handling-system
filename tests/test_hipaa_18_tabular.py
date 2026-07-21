from __future__ import annotations

import json

import pytest

from generators.hipaa_18_tabular import (
    HIPAA18_IDENTIFIER_SPECS,
    USHIPAA18TabularCorpusGenerator,
    validate_corpus,
)


def test_baseline_has_every_hipaa_category_once():
    corpus = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=18)

    assert [row["hipaa_category"] for row in corpus.dictionary_rows] == list(
        "ABCDEFGHIJKLMNOPQR"
    )
    assert len(corpus.dictionary_rows) == 18


def test_every_subject_has_the_ordered_eighteen_identifier_columns():
    corpus = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=18)
    expected_columns = [spec.source_column for spec in HIPAA18_IDENTIFIER_SPECS]

    assert len(corpus.dataset_rows) == 18
    assert all(list(row) == expected_columns for row in corpus.dataset_rows)
    assert all(all(row.values()) for row in corpus.dataset_rows)


def test_baseline_user_output_drops_direct_identifiers_and_retains_only_dob_year():
    corpus = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=2)
    assert len(corpus.dataset_rows) == len(corpus.expected_user_rows)

    for source, output in zip(corpus.dataset_rows, corpus.expected_user_rows):
        assert output["ROW_TOKEN"].startswith("ROW_")
        for spec in HIPAA18_IDENTIFIER_SPECS:
            if spec.hipaa_category == "C":
                assert output[spec.source_column] == source[spec.source_column][:4]
            else:
                assert output[spec.source_column] == ""


def test_in_memory_generation_is_seed_deterministic():
    first = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=18)
    second = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=18)

    assert second == first


def test_generate_requires_at_least_one_subject():
    with pytest.raises(ValueError, match="n_subjects must be >= 1"):
        USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=0)


def test_audit_events_do_not_embed_plaintext_identifiers():
    corpus = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=2)
    audit_text = json.dumps(corpus.audit_events, sort_keys=True)

    for row in corpus.dataset_rows:
        for value in row.values():
            assert value not in audit_text


def test_baseline_validates_and_cell_evidence_is_complete():
    corpus = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=18)

    assert validate_corpus(corpus) == []
    assert len(corpus.gold_entries) == 18 * 18
    assert len(corpus.audit_events) == 18 * 18


def test_gold_entries_reference_exact_dataset_cells():
    corpus = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=3)

    for entry in corpus.gold_entries:
        assert (
            corpus.dataset_rows[entry["row_index"]][entry["column"]]
            == entry["original_value"]
        )


def test_audit_events_have_what_why_how_and_hmac_evidence():
    corpus = USHIPAA18TabularCorpusGenerator(seed=42).generate(n_subjects=1)

    for event in corpus.audit_events:
        assert set(event["what"]) == {"action", "outcome"}
        assert set(event["why"]) == {"rule_id", "authority", "reason"}
        assert set(event["how"]) == {"method"}
        assert set(event["evidence"]) == {
            "input_hmac_sha256",
            "output_hmac_sha256",
        }
        assert "original_value" not in json.dumps(event, sort_keys=True)
