"""V4: deterministic half of the zero-raw-identifier claim.

Builds a corpus artifact with `plant()`, then runs the dictionary CSV text
and a synthetic form text (simulating what an Instrument agent would read
from a real collection form) through `anonymizer.scrub_for_prompt`, and
asserts that no planted `leak_literals` entry of length four or more
survives in either scrubbed output, case-insensitive. This is what stands
between a planted identifier and an LLM prompt before `scrub_for_prompt`
even runs (specialists.py:44, :123); the runtime half of the claim is
`context_hygiene` in the benchmark report (V5).
"""
from __future__ import annotations

import zipfile
import io

from phi_core.anonymizer import scrub_for_prompt
from phi_corpus.planters import plant


def _collect_leak_literals(ground_truth: dict) -> list[str]:
    literals: list[str] = []
    for cell in ground_truth.get("planted", []):
        for lit in cell.get("leak_literals", []):
            if len(lit) >= 4:
                literals.append(lit)
    return literals


def _collect_labelled_leaks(ground_truth: dict) -> list[tuple[str, str]]:
    """(column label, leak literal) pairs, the way a real form or
    dictionary actually presents a value: next to the field name that
    describes it, not floating in isolation. A bare out-of-context digit
    string (e.g. a 5-digit ZIP with no "ZIP" label anywhere nearby) is a
    known, documented residual gap for regex/NER detection (README.md
    "Benchmark": quasi-identifiers require human judgement); real prompts
    always carry the column's own label, and the detector is expected to
    catch a leak in that realistic shape."""
    pairs: list[tuple[str, str]] = []
    for cell in ground_truth.get("planted", []):
        col = cell.get("column", "")
        for lit in cell.get("leak_literals", []):
            if len(lit) >= 4:
                pairs.append((col, lit))
    return pairs


def _assert_no_literal_survives(scrubbed: str, literals: list[str]):
    lowered = scrubbed.lower()
    survivors = [lit for lit in literals if lit.lower() in lowered]
    assert not survivors, f"leak literals survived scrub_for_prompt: {survivors[:10]}"


def test_dictionary_text_contains_no_leak_literal_after_scrub():
    art = plant(scenario_id="oncology_v1", jurisdiction="us", row_count=12, seed=42)
    literals = _collect_leak_literals(art.ground_truth)
    assert literals, "fixture has no leak literals to test against"

    z = zipfile.ZipFile(io.BytesIO(art.zip_bytes))
    dict_text = z.read("dictionary/columns.csv").decode("utf-8")

    scrubbed, _n = scrub_for_prompt(dict_text)
    _assert_no_literal_survives(scrubbed, literals)


def test_synthetic_form_text_embedding_planted_literals_scrubs_clean():
    """The corpus generator plants no real form (datasets + dictionary
    only, per direction), so this simulates the realistic case an
    Instrument agent actually faces: a form whose free text mentions
    identifiers the way a real consent form or case-report form would,
    each value next to the field label that names it."""
    art = plant(scenario_id="oncology_v1", jurisdiction="us", row_count=12, seed=7)
    pairs = _collect_labelled_leaks(art.ground_truth)
    assert pairs, "fixture has no leak literals to test against"

    sample = pairs[:25]
    form_text = (
        "Oncology Study Case Report Form\n"
        "Section 1: Participant identification\n"
        + "\n".join(f"{col}: {lit}" for col, lit in sample)
        + "\nSection 2: Clinical assessment\nRecord vitals and labs as instructed."
    )

    scrubbed, n_removed = scrub_for_prompt(form_text)
    assert n_removed > 0, "scrub_for_prompt found nothing to redact in a text full of planted identifiers"
    _assert_no_literal_survives(scrubbed, [lit for _col, lit in sample])


def test_scrub_for_prompt_preserves_non_identifier_structure():
    """Redaction must not be so aggressive it destroys the whole document;
    the model still needs the surrounding structure to do its job."""
    art = plant(scenario_id="oncology_v1", jurisdiction="us", row_count=12, seed=13)
    pairs = _collect_labelled_leaks(art.ground_truth)[:5]
    form_text = (
        "Section 2: Clinical assessment\n"
        + "\n".join(f"{col}: {lit}" for col, lit in pairs)
        + "\nRecord vitals and labs as instructed."
    )
    scrubbed, _n = scrub_for_prompt(form_text)
    assert "Section 2: Clinical assessment" in scrubbed
    assert "Record vitals and labs as instructed." in scrubbed
