"""Presidio-based PHI residual scanner (Wave 3 C3, primary Layer-2 gate).

Decision D2 (adopt-and-replace, OR-combined): Microsoft Presidio becomes the
**primary** structured-PHI scanner over LLM-visible artifacts, running the custom
recognizers ported from :mod:`scripts.security.phi_patterns`; the battle-tested
:func:`scripts.security.llm_source_gate.scan_tree_for_phi` is retained as an
**OR-combined secondary** (the combined gate fails if *either* finds PHI — see
:mod:`scripts.security.phi_guard_gate`).

Two design choices keep this airgapped and value-free:

* **No spaCy model.** ``PatternRecognizer.analyze(text, entities, nlp_artifacts=None)``
  runs pure-regex recognition with no NLP pipeline, so no ~560 MB
  ``en_core_web_*`` model download is required (reproducible / offline). Free-text
  PERSON / LOCATION NER stays the job of the OR-combined ``scan_tree_for_phi`` +
  ``phi_allowlist`` secondary, exactly as D2 specifies.
* **Value-free findings.** A finding carries the entity type, character offset,
  confidence score, and source location — never the matched substring — so a gate
  report can never become a PHI side channel (mirrors ``LeakScanFinding``).

The Verhoeff (Aadhaar) and Indian-phone recognizers reuse the *same*
``.validate()`` logic as the ``phi_patterns`` wrappers via a post-match validator,
so the Presidio path and the legacy path can never drift on what counts as a
real identifier vs. a placeholder.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from phi_engine.security.llm_source_gate import (
    _DATE_PATTERN_NAMES,
    _is_allowed_scrubbed_date,
    _is_dictionary_mapping_path,
)
from phi_engine.security.phi_patterns import (
    BLOCKING_PATTERNS,
    IndianPhonePattern,
    VerhoeffPattern,
)
from phi_engine.utils.logging_system import get_logger

__all__ = [
    "PresidioFinding",
    "PresidioScanResult",
    "analyze_text",
    "scan_tree_with_presidio",
]

_logger = get_logger(__name__)

# Confidence assigned to a raw regex hit before validation. Above Presidio's
# default analyze threshold so any match surfaces; the post-match validator is
# the authoritative accept/reject for checksum-bearing patterns.
_BASE_SCORE = 0.6


@dataclass(frozen=True)
class PresidioFinding:
    """A value-free Presidio finding (no matched substring)."""

    relative_path: str
    line_number: int
    pattern_name: str  # the Presidio entity type == the phi_patterns name
    start: int
    end: int
    score: float
    # Value-free: the offending JSON leaf KEY (dataset column NAME) only — never
    # the matched value. Empty for non-jsonl line scans (no column context).
    column: str = ""


@dataclass(frozen=True)
class PresidioScanResult:
    ok: bool
    findings: tuple[PresidioFinding, ...]

    @property
    def detail(self) -> str:
        if self.ok:
            return ""
        f = self.findings[0]
        return (
            f"presidio entity {f.pattern_name} matched in {f.relative_path} "
            f"line {f.line_number} offset {f.start}:{f.end} (matched content omitted)"
        )


# ── recognizer construction (built once, lazily) ──────────────────────────────
_ANALYZER: Any = None
_VALIDATORS: dict[str, Callable[[str], bool]] = {}


def _regex_string(obj: Any) -> str:
    """Return the underlying regex source for a plain pattern or a wrapper."""
    return str(obj.pattern if hasattr(obj, "pattern") else obj)


def _build_analyzer() -> Any:
    """Construct a model-free Presidio AnalyzerEngine with the ported recognizers.

    Returns the cached singleton. Imports presidio lazily so this module (and the
    security package) import cheaply when the gate is not exercised.
    """
    global _ANALYZER
    if _ANALYZER is not None:
        return _ANALYZER

    import spacy
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
    from presidio_analyzer.nlp_engine import SpacyNlpEngine

    class _TokenizerOnlyNlpEngine(SpacyNlpEngine):
        """spacy.blank('en') tokenizer — no NER model, no download."""

        def __init__(self) -> None:
            self.nlp = {"en": spacy.blank("en")}  # type: ignore[assignment]
            # presidio 2.2.x reads these during registry wiring; a tokenizer-only
            # engine recognizes no NER entities, which is exactly what we want.
            from presidio_analyzer.nlp_engine import NerModelConfiguration

            self.ner_model_configuration = NerModelConfiguration()

    registry = RecognizerRegistry()
    validators: dict[str, Callable[[str], bool]] = {}
    for name, obj in BLOCKING_PATTERNS:
        recognizer = PatternRecognizer(
            supported_entity=name,
            patterns=[Pattern(name=name, regex=_regex_string(obj), score=_BASE_SCORE)],
        )
        registry.add_recognizer(recognizer)
        if isinstance(obj, (VerhoeffPattern, IndianPhonePattern)):
            # Reuse the wrapper's own validation so the two paths cannot drift.
            validators[name] = obj.validate

    engine = _TokenizerOnlyNlpEngine()
    _ANALYZER = AnalyzerEngine(
        registry=registry,
        nlp_engine=engine,
        supported_languages=["en"],
    )
    _VALIDATORS.clear()
    _VALIDATORS.update(validators)
    return _ANALYZER


def analyze_text(text: str) -> list[PresidioFinding]:
    """Return value-free findings for *text* (offsets + entity types, no values).

    Checksum-bearing entities (Aadhaar, Indian phone) are confirmed with the
    shared ``phi_patterns`` validator on the matched span; placeholder matches
    (e.g. all-9 phone) are dropped.
    """
    analyzer = _build_analyzer()
    results = analyzer.analyze(text=text, language="en")
    findings: list[PresidioFinding] = []
    for r in results:
        validator = _VALIDATORS.get(r.entity_type)
        if validator is not None and not validator(text[r.start : r.end]):
            continue  # placeholder / checksum-invalid → not real PHI
        findings.append(
            PresidioFinding(
                relative_path="",
                line_number=0,
                pattern_name=r.entity_type,
                start=r.start,
                end=r.end,
                score=float(r.score),
            )
        )
    return findings


def _scan_json_line_presidio(
    *, root: Path, fpath: Path, line: str, line_number: int, suppress_dates: bool
) -> PresidioFinding | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None

    try:
        rel = str(fpath.relative_to(root))
    except ValueError:
        rel = fpath.name

    def _walk(obj: object, prefix: str = "") -> PresidioFinding | None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                child = f"{prefix}.{key}" if prefix else str(key)
                hit = _walk(value, child)
                if hit is not None:
                    return hit
        elif isinstance(obj, list):
            for index, value in enumerate(obj):
                hit = _walk(value, f"{prefix}[{index}]")
                if hit is not None:
                    return hit
        elif isinstance(obj, str):
            for f in analyze_text(obj):
                if f.pattern_name in _DATE_PATTERN_NAMES and (
                    suppress_dates or _is_allowed_scrubbed_date(prefix)
                ):
                    continue
                return PresidioFinding(
                    relative_path=rel,
                    line_number=line_number,
                    pattern_name=f.pattern_name,
                    start=f.start,
                    end=f.end,
                    score=f.score,
                    column=prefix.rsplit(".", 1)[-1].split("[")[0],
                )
        return None

    return _walk(payload)


def scan_tree_with_presidio(root: Path) -> PresidioScanResult:
    """Scan a tree with the Presidio recognizers (value-free, model-free).

    Mirrors :func:`scan_tree_for_phi`'s file iteration and date/dictionary
    exemptions so the OR-combined gate applies one consistent exemption policy:
    a ``dictionary_mapping/`` subtree has date-class entities suppressed (codelist
    help-text dates are documentation, not PHI), and approved scrubbed date
    fields are exempt by column name.
    """
    root = Path(root)
    if not root.is_dir():
        return PresidioScanResult(ok=True, findings=())

    for fpath in sorted(root.rglob("*")):
        if not fpath.is_file():
            continue
        suppress_dates = _is_dictionary_mapping_path(fpath, root)
        try:
            with fpath.open(encoding="utf-8", errors="replace") as fh:
                for line_number, line in enumerate(fh, start=1):
                    if fpath.suffix == ".jsonl":
                        finding = _scan_json_line_presidio(
                            root=root,
                            fpath=fpath,
                            line=line,
                            line_number=line_number,
                            suppress_dates=suppress_dates,
                        )
                        if finding is not None:
                            return PresidioScanResult(ok=False, findings=(finding,))
                        continue
                    for f in analyze_text(line):
                        if f.pattern_name in _DATE_PATTERN_NAMES and suppress_dates:
                            continue
                        try:
                            rel = str(fpath.relative_to(root))
                        except ValueError:
                            rel = fpath.name
                        return PresidioScanResult(
                            ok=False,
                            findings=(
                                PresidioFinding(
                                    relative_path=rel,
                                    line_number=line_number,
                                    pattern_name=f.pattern_name,
                                    start=f.start,
                                    end=f.end,
                                    score=f.score,
                                ),
                            ),
                        )
        except OSError as exc:
            return PresidioScanResult(
                ok=False,
                findings=(
                    PresidioFinding(
                        relative_path=str(fpath),
                        line_number=0,
                        pattern_name=f"read_error:{exc.__class__.__name__}",
                        start=0,
                        end=0,
                        score=1.0,
                    ),
                ),
            )

    return PresidioScanResult(ok=True, findings=())
