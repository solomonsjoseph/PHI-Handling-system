#!/usr/bin/env python3
"""Generate a PDF-aware SoT policy candidate from a source pack.

This helper is intentionally conservative. It uses row-1 dataset headers for
variable keys and PDF annotation geometry plus nearby printed text for prompt
drafting. When the printed widget cannot be verified, it emits a header-only
variable with a discrepancy instead of inventing wording.

Portability note: the curated annotation tables below (aliases, non-variable
labels, true PDF-only variables, TST/HIV calibrations, and STUDY_NAME) are
Indo-VAP-specific source knowledge preserved from the archived producer. A new
deployment must clear those dictionaries or replace them with that study's own
annotation review tables; this module is not a generic annotated-PDF extractor
out of the box.
"""

# ruff: noqa: S108

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pdfplumber
import yaml

PDF_FIELD_COUNT_MISMATCH_KIND = "pdf_field_count_column_count_mismatch"

STUDY_NAME = "Indo-US VAP Biomarkers for Risk of Tuberculosis and for Tuberculosis Treatment Failure and Relapse"

FORM_10_TST_ROW_RE = re.compile(r"^TST_.*?(\d)$")

ANNOTATION_ALIASES: dict[str, dict[str, str]] = {
    "10_TST": {
        "TST_BLIST7": "TST_BLIST6",
    },
    "14_CaseControl": {
        "ALCDOSTX": "CC_ALCDOSTX",
        "BIDICURR": "CC_BIDICURR",
        "CC-NOPRGTESTSP": "CC_NOPREGTESTSP",
        "CC-WSH": "CC_WSH",
        "CC_NOMENES": "CC_NOMENSES",
        "CC_RASH": "CC_RSH",
        "CC_THIRST": "CC_THRST",
        "CC_VISTDAT": "CC_VISDAT",
        "CC_WEEGHT": "CC_WEIGHT",
        "CGRCURR": "CC_CGRCURR",
        "CIGCURR": "CC_CIGCURR",
        "HOOCUR": "CC_HOOCURR",
        "LUMPS": "CC_LUMPS",
        "SMKOTHCURR": "CC_SMKOTHCURR",
    },
    "15_Feces": {
        "FC_PARAS3_4": "FC_PARAS4_4",
    },
    "1B_HCScreening": {
        "BINCL01": "HHC_BINCL01",
        "HHC-ENROLLCP": "HHC_ENROLLCP",
        "HHC_UNWILLING": "HHC_NOTENROLL",
        "ICF": "HHC_ICF",
        "SEX": "HHC_SEX",
    },
    "2B_HCBaseline": {
        "CANCER": "Cancer",
    },
}

NON_VARIABLE_ANNOTATIONS: dict[str, set[str]] = {
    "1A_ICScreening": {"IS_"},
    "1B_HCScreening": {"selbyk"},
    "2A_ICBaseline": {"TBKNOWLINK"},
    "14_CaseControl": {"CC_", "selbyk"},
    "99B_FSB": {"EPTB"},
}

TRUE_PDF_VARIABLES_WITHOUT_DATASET_HEADER: dict[str, set[str]] = {
    "15_Feces": {
        "FC_CONSIST1",
        "FC_CONSIST2",
        "FC_CONSIST3",
        "FC_SIGN",
        "FC_TECH1",
        "FC_TECH2",
        "FC_TECH3",
    },
}


def _is_system(name: str) -> bool:
    lower = name.lower()
    return lower in {"time_stamp", "timestamp", "time stamp"} or name.startswith(
        ("Batch", "Remote_", "Orig", "Route_", "Verify_", "Image_", "FormID", "Suspense_")
    )


def _is_identifier(name: str) -> bool:
    upper = name.upper()
    return (
        upper in {"SUBJID", "FID", "CSID"}
        or re.fullmatch(r"SUBJID\d*(?:_\d+)?", upper) is not None
        or re.fullmatch(r"HHC\d+", upper) is not None
    )


def _looks_signature(name: str) -> bool:
    return name.upper().endswith(("SIGN", "SIGNATURE", "COLLSIG", "PROCSIG"))


def _looks_initials(name: str) -> bool:
    return name.upper().endswith("INIT")


def _looks_date(name: str) -> bool:
    upper = re.sub(r"\d+$", "", name.upper())
    return (
        upper.endswith(("DAT", "DATE", "DTE", "DT"))
        or upper.endswith("RDATE")
        or "COMPDAT" in upper
    )


def _looks_time(name: str) -> bool:
    upper = re.sub(r"\d+$", "", name.upper())
    return upper.endswith(("TIM", "TIME", "TM"))


def _looks_free_text(name: str) -> bool:
    upper = re.sub(r"\d+$", "", name.upper())
    return any(
        token in upper
        for token in ("OTH", "OTHER", "SPEC", "EXPLAIN", "COMMENT", "NOTE", "REASONSP")
    )


def _looks_integer(name: str) -> bool:
    upper = re.sub(r"\d+$", "", name.upper())
    return any(
        token in upper
        for token in (
            "AGE",
            "DAYS",
            "YRS",
            "YEAR",
            "MONTH",
            "DOSE",
            "NUM",
            "COUNT",
            "CD4",
            "INDUR",
            "EGG",
        )
    )


def _looks_decimal(name: str) -> bool:
    upper = re.sub(r"\d+$", "", name.upper())
    return any(
        token in upper
        for token in (
            "WEIGHT",
            "HEIGHT",
            "VOL",
            "HGB",
            "WBC",
            "RBC",
            "MCV",
            "RDW",
            "PCT",
            "PERCENT",
        )
    )


def _is_not_done_var(name: str) -> bool:
    upper = name.upper()
    return upper.endswith("ND") or "_ND" in upper


def _known_prompt_override(name: str, prompt: str | None) -> str | None:
    upper = name.upper()
    row = re.search(r"(\d+)$", upper)
    row_note = f" (row {row.group(1)})" if row else ""
    base = re.sub(r"\d+$", "", upper)
    if base == "TST_INDUR":
        return f"TST Result: induration in mm{row_note}"
    if base == "TST_BLIST":
        return f"TST Result: Blistering{row_note}"
    if base == "TST_STREN":
        return f"PPD Strength{row_note}"
    if base == "TST_STRENOTH":
        return f"PPD Strength: Other, specify{row_note}"
    feces_prompt = _feces_prompt_override(upper)
    if feces_prompt is not None:
        return feces_prompt
    return prompt


def _ordinal(value: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(value, str(value))


def _feces_sample_slot(upper: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"FC_(?:PARAS|EGG)([0-9]+)_([0-9]+)", upper)
    if not match:
        return None
    sample = int(match.group(1))
    slot = int(match.group(2))
    # The PDF annotation says FC_PARAS3_4, while the dataset row-1 header says
    # FC_PARAS4_4. Treat the dataset key as sample 3, parasite-code slot 4.
    if upper == "FC_PARAS4_4":
        sample = 3
    return sample, slot


def _feces_prompt_override(upper: str) -> str | None:
    if upper == "FID":
        return "Family ID:"
    if upper == "FC_GIVEDAT":
        return "Date specimen containers given to participant:"
    if upper == "FC_RECDAT":
        return "Date specimen containers received:"
    if upper == "FC_TEST":
        return "Test run:"
    if upper == "FC_COMPDTE":
        return "Date of CRF Completion:"

    match = re.fullmatch(r"FC_NOCOLL([123])", upper)
    if match:
        sample = int(match.group(1))
        return f"1. Sample: {_ordinal(sample)} sample - Not collected"

    match = re.fullmatch(r"FC_PROCDAT([123])", upper)
    if match:
        sample = int(match.group(1))
        return f"4. Date of Processing ({_ordinal(sample)} sample)"

    sample_slot = _feces_sample_slot(upper)
    if sample_slot and upper.startswith("FC_PARAS"):
        sample, slot = sample_slot
        return f"5. Parasites: Legend A code slot {slot} ({_ordinal(sample)} sample)"
    if sample_slot and upper.startswith("FC_EGG"):
        sample, slot = sample_slot
        return f"5. Parasites: eggs/g for code slot {slot} ({_ordinal(sample)} sample)"

    match = re.fullmatch(r"FC_OTHER([123])", upper)
    if match:
        sample = int(match.group(1))
        return f"5. Parasites: If other (16), specify type and eggs/g ({_ordinal(sample)} sample)"

    match = re.fullmatch(r"FC_COMEN([123])", upper)
    if match:
        sample = int(match.group(1))
        return f"6. Commensals: Legend B code ({_ordinal(sample)} sample)"

    return None


def _known_options_override(name: str) -> list[str] | None:
    upper = name.upper()
    if upper == "FC_TEST":
        return ["Initial", "Confirmation"]
    if re.fullmatch(r"FC_NOCOLL[123]", upper):
        return ["Not collected"]
    if upper.startswith("FC_PARAS"):
        return [
            "01. Ancyclostoma duodenale (eggs)",
            "02. Ascaris lumbricoides (eggs)",
            "03. Cryptosporidium parvum (oocysts)",
            "04. Diphyllobothrium (fish tapeworm) (eggs)",
            "05. Entamoeba histolytica; Entamoeba dispar (cysts)",
            "06. Enterobius vermicularis (eggs)",
            "07. Giardia lamblia (cysts)",
            "08. Hymenolepis nana (dwarf tapeworm) (eggs)",
            "09. Isospora belli (oocysts)",
            "10. Schistosoma mansoni (eggs)",
            "11. Strongyloides stercoralis (larva)",
            "12. Taenia solium (pork tape worm) (eggs)",
            "13. Taenia saginatum (beef tapeworm) (eggs)",
            "14. Trichuris trichiura (eggs)",
            "15. Indistinguishable (eggs)",
            "16. Other",
            "17. No Parasite found",
        ]
    if re.fullmatch(r"FC_COMEN[123]", upper):
        return [
            '1. Blatocystis hominis ("cyst-like")',
            "2. Entamoeba coli (cysts)",
            "3. Endolimax nana (cysts)",
            "4. Iodamoeba butschlii (cysts)",
            "5. No Parasite found",
        ]
    return None


def _is_completion(name: str) -> bool:
    upper = name.upper()
    return (
        _looks_signature(name) or _looks_initials(name) or "COMPDAT" in upper or "COMPDTE" in upper
    )


def _form_number(form: str) -> str:
    match = re.match(r"^([0-9]+[A-Z]?)", form)
    return f"Form {match.group(1)}" if match else form


def _form_version(pdf_path: str) -> str:
    match = re.search(r"[vV]\s*([0-9]+(?:\.[0-9]+)?)", pdf_path)
    return f"v{match.group(1)}" if match else "v1.0"


def _first_title(lines: list[str]) -> str:
    for line in lines[:20]:
        upper = line.upper()
        if (
            len(line) > 6
            and upper == line
            and "INDO-US" not in upper
            and not upper.startswith("FORM ")
            and not _is_mask_or_value_line(line)
        ):
            return line
    for line in lines[:20]:
        upper = line.upper()
        if (
            "INDO-US" not in upper
            and not upper.startswith("FORM ")
            and len(line) > 6
            and not _is_mask_or_value_line(line)
        ):
            return line
    return "Untitled form"


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace(" :", ":")
    return text


def _line_key(word: dict[str, Any]) -> float:
    return float(word.get("top") or 0)


def _extract_page_lines(pdf_path: Path) -> dict[int, list[dict[str, Any]]]:
    pages: dict[int, list[dict[str, Any]]] = {}
    with pdfplumber.open(pdf_path) as doc:
        for page_index, page in enumerate(doc.pages, start=1):
            words = page.extract_words(x_tolerance=1, y_tolerance=3) or []
            words = sorted(words, key=lambda w: (float(w.get("top") or 0), float(w.get("x0") or 0)))
            groups: list[list[dict[str, Any]]] = []
            for word in words:
                top = _line_key(word)
                if not groups or abs(top - _line_key(groups[-1][0])) > 3.5:
                    groups.append([word])
                else:
                    groups[-1].append(word)
            lines: list[dict[str, Any]] = []
            for group in groups:
                group = sorted(group, key=lambda w: float(w.get("x0") or 0))
                text = _clean_text(" ".join(str(w.get("text") or "") for w in group))
                if not text:
                    continue
                segments = _word_segments(group)
                lines.append(
                    {
                        "text": text,
                        "x0": min(float(w.get("x0") or 0) for w in group),
                        "x1": max(float(w.get("x1") or 0) for w in group),
                        "top": min(float(w.get("top") or 0) for w in group),
                        "bottom": max(float(w.get("bottom") or 0) for w in group),
                        "segments": segments,
                    }
                )
            pages[page_index] = lines
    return pages


def _word_segments(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: list[list[dict[str, Any]]] = []
    for word in words:
        if not segments:
            segments.append([word])
            continue
        gap = float(word.get("x0") or 0) - float(segments[-1][-1].get("x1") or 0)
        if gap > 18:
            segments.append([word])
        else:
            segments[-1].append(word)
    out = []
    for segment in segments:
        text = _clean_text(" ".join(str(w.get("text") or "") for w in segment))
        if text:
            out.append(
                {
                    "text": text,
                    "x0": min(float(w.get("x0") or 0) for w in segment),
                    "x1": max(float(w.get("x1") or 0) for w in segment),
                }
            )
    return out


def _is_mask_or_value_line(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return True
    letters = re.sub(r"[^A-Z]", "", compact.upper())
    mask_letters = set("DMYHBTU")
    if (
        re.fullmatch(r"[DMYH:/#.\-0-9A-Z]+", compact.upper())
        and len(letters) <= 8
        and set(letters) <= mask_letters
    ):
        return True
    return (
        re.fullmatch(r"[0-9:/#.\-A-Z ]+", text.upper())
        and len(letters) <= 8
        and set(letters) <= mask_letters
        and not re.search(r"[a-z]", text)
    )


def _line_center(line: dict[str, Any]) -> tuple[float, float]:
    return (
        (float(line["x0"]) + float(line["x1"])) / 2,
        (float(line["top"]) + float(line["bottom"])) / 2,
    )


def _ann_center(ann: dict[str, Any]) -> tuple[float, float]:
    return (
        (float(ann["x0"]) + float(ann["x1"])) / 2,
        (float(ann["top"]) + float(ann["bottom"])) / 2,
    )


def _best_prompt_from_geometry(
    name: str,
    ann: dict[str, Any],
    page_lines: dict[int, list[dict[str, Any]]],
) -> tuple[str | None, str | None]:
    lines = page_lines.get(int(ann.get("page") or 1), [])
    ax, ay = _ann_center(ann)
    row_suffix = re.search(r"(\d+)$", name)
    row_note = f" (row {row_suffix.group(1)})" if row_suffix else ""

    header_candidates: list[tuple[float, str, str]] = []
    local_candidates: list[tuple[float, str, str]] = []
    for line in lines:
        segment = _best_segment_for_x(line, ax)
        text = segment["text"] if segment else line["text"]
        text = _clean_text(text)
        if (
            not re.search(r"[A-Za-z]", text)
            or _is_mask_or_value_line(text)
            or _is_form_artifact_line(text)
        ):
            continue
        _lx, ly = _line_center(line)
        vertical_above = ay - ly
        vertical_below = ly - ay
        sx0 = float(segment["x0"]) if segment else float(line["x0"])
        sx1 = float(segment["x1"]) if segment else float(line["x1"])
        overlap = max(0.0, min(sx1, ax + 60) - max(sx0, ax - 60))
        horizontal_distance = (
            0.0 if sx0 - 20 <= ax <= sx1 + 20 else min(abs(ax - sx0), abs(ax - sx1))
        )

        if (
            row_suffix
            and 0 < vertical_above < 430
            and float(line["top"]) < 135
            and (overlap > 0 or horizontal_distance < 90)
        ):
            header_candidates.append(
                (horizontal_distance * 0.08 + vertical_above * 0.005, text, "column-header")
            )
        elif 0 < vertical_above < 90 and (overlap > 0 or horizontal_distance < 80):
            local_candidates.append((vertical_above + horizontal_distance * 0.08, text, "above"))
        elif abs(ay - ly) < 18 and horizontal_distance < 120:
            local_candidates.append(
                (abs(ay - ly) + horizontal_distance * 0.05 + 20, text, "same-row")
            )
        elif abs(ay - ly) < 28 and float(line["x1"]) <= ax + 20:
            local_candidates.append(
                (abs(ay - ly) + max(0, ax - float(line["x1"])) * 0.04 + 25, text, "left")
            )
        elif 0 < vertical_below < 55 and horizontal_distance < 120:
            local_candidates.append(
                (vertical_below + horizontal_distance * 0.05 + 35, text, "below")
            )

    if row_suffix and header_candidates:
        _score, header_text, location = sorted(header_candidates, key=lambda item: item[0])[0]
        row_text = None
        if local_candidates:
            _local_score, local_text, _local_loc = sorted(
                local_candidates, key=lambda item: item[0]
            )[0]
            if (
                local_text != header_text
                and not _is_mask_or_value_line(local_text)
                and not re.fullmatch(r"Done", local_text, re.I)
            ):
                row_text = local_text
        if (
            row_text
            and not name.upper().startswith("HHC")
            and not _looks_date(name)
            and not _looks_time(name)
            and "PPD Strength" in header_text
            and "other" not in row_text.lower()
        ):
            row_text = None
        if row_text and not name.upper().startswith("HHC"):
            return f"{_clean_text(header_text)}: {_clean_text(row_text)}{row_note}", location
        return f"{_clean_text(header_text)}{row_note}", location

    if not local_candidates:
        return None, None

    _score, text, location = sorted(local_candidates, key=lambda item: item[0])[0]
    if _is_not_done_var(name) and text.lower() == "done":
        text = "Not Done"
    return f"{_clean_text(text)}{row_note}", location


def _best_segment_for_x(line: dict[str, Any], x: float) -> dict[str, Any] | None:
    segments = line.get("segments") or []
    if not isinstance(segments, list) or not segments:
        return None
    containing = [seg for seg in segments if float(seg["x0"]) - 12 <= x <= float(seg["x1"]) + 12]
    if containing:
        return sorted(containing, key=lambda seg: float(seg["x1"]) - float(seg["x0"]))[0]
    return sorted(
        segments, key=lambda seg: min(abs(x - float(seg["x0"])), abs(x - float(seg["x1"])))
    )[0]


def _is_form_artifact_line(text: str) -> bool:
    upper = text.upper()
    return "INDO-US VAP" in upper or upper.startswith("FORM ")


def _format_from_nearby_text(
    name: str, ann: dict[str, Any], page_lines: dict[int, list[dict[str, Any]]]
) -> str | None:
    if not _looks_date(name):
        return None
    lines = page_lines.get(int(ann.get("page") or 1), [])
    _ax, ay = _ann_center(ann)
    nearby = " ".join(line["text"] for line in lines if abs((_line_center(line)[1]) - ay) < 45)
    compact = re.sub(r"\s+", "", nearby).upper()
    if "DD/MM/YY" in compact and "DD/MM/YYYY" not in compact:
        return "DD/MM/YY"
    return None


def _type_and_phi(name: str, matched: bool) -> dict[str, Any]:
    if _is_system(name):
        return {"type": "datetime", "phi": "drop"}
    if _is_identifier(name):
        return {"type": "identifier", "phi": "pseudonymize"}
    if _looks_signature(name):
        return {"type": "signature", "phi": "drop"}
    if _looks_initials(name):
        return {"type": "initials", "phi": "drop"}
    if _looks_date(name):
        meta: dict[str, Any] = {"type": "date"}
        if name.upper().endswith(("COMPDAT", "COMPDTE")):
            meta["phi"] = "jitter_date"
        return meta
    if _looks_time(name):
        return {"type": "time"}
    if _looks_free_text(name):
        return {"type": "free_text", "notes": "no PHI expected"}
    if _looks_decimal(name):
        return {"type": "decimal"}
    if _looks_integer(name):
        return {"type": "integer"}
    return {"type": "code"} if matched else {}


def _widget_for(name: str, prompt: str | None, fmt: str | None, matched: bool) -> str:
    upper = name.upper()
    prompt_low = (prompt or "").lower()
    if not matched:
        return "no visible printed widget found on rendered PDF; dataset row-1 header retained for binding only"
    if upper == "FC_TEST":
        return "2 checkboxes: Initial, Confirmation"
    if re.fullmatch(r"FC_NOCOLL[123]", upper):
        return "single checkbox for Not collected in the Sample column"
    if re.fullmatch(r"FC_PROCDAT[123]", upper) or upper in {
        "FC_GIVEDAT",
        "FC_RECDAT",
        "FC_COMPDTE",
    }:
        return "Day(2) / Month(2) / Year(4) date boxes"
    if upper.startswith("FC_PARAS"):
        return "2 character-box parasite code field aligned to the printed Legend A code list"
    if upper.startswith("FC_EGG"):
        return "rectangular eggs/g numeric entry field aligned under the parasite code slot"
    if re.fullmatch(r"FC_OTHER[123]", upper):
        return "single-line free-text underline for Other parasite type and eggs/g"
    if re.fullmatch(r"FC_COMEN[123]", upper):
        return "5 checkbox code options in the Commensals column"
    if _is_identifier(name):
        return "identifier entry field aligned to printed identifier prompt"
    if _looks_signature(name):
        return "signature line"
    if _looks_initials(name):
        return "initials entry boxes"
    if fmt == "DD/MM/YY":
        return "Day(2) / Month(2) / Year(2) date boxes"
    if _looks_date(name):
        return "date entry boxes"
    if _looks_time(name):
        return "24-hour time entry boxes"
    if upper.endswith("ND") or "_ND" in upper or "not done" in prompt_low:
        return "single checkbox for Not Done/Unknown"
    if _looks_free_text(name):
        return "single-line free-text field"
    if _looks_decimal(name) or _looks_integer(name):
        return "numeric entry field"
    return "coded response field"


def _section_for(name: str, matched: bool) -> str:
    if _is_system(name):
        return "system"
    if not matched:
        return "unmatched_dataset"
    if _is_completion(name):
        return "completion"
    if _is_identifier(name):
        return "header"
    return "form_body"


def _annotation_maps(
    pack: dict[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], list[str]]:
    exact: dict[str, list[dict[str, Any]]] = defaultdict(list)
    folded: dict[str, list[dict[str, Any]]] = defaultdict(list)
    labels: list[str] = []
    for page in pack.get("pages", []):
        for ann in page.get("annotation_details", []) or []:
            text = ann.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if (
                ann.get("x0") is None
                or ann.get("top") is None
                or ann.get("x1") is None
                or ann.get("bottom") is None
            ):
                continue
            cleaned = text.strip()
            labels.append(cleaned)
            exact[cleaned].append(ann)
            folded[cleaned.lower()].append(ann)
    return exact, folded, labels


def _is_expected_repeated_annotation(label: str) -> bool:
    stripped = label.strip()
    if re.fullmatch(r"[0-9]+", stripped):
        return True
    return stripped.lower() in {"yes", "no", "unknown", "not done", "allowed", "never allowed"}


def _bindable_headers(headers: list[str]) -> set[str]:
    return {name for name in headers if not _is_system(name)}


def _resolve_annotation_header(
    label: str,
    *,
    header_set: set[str],
    lower_to_header: dict[str, str],
    annotation_aliases: dict[str, str],
) -> str | None:
    if label in header_set:
        return label
    if label.lower() in lower_to_header:
        return lower_to_header[label.lower()]
    target = annotation_aliases.get(label)
    if isinstance(target, str) and target in header_set:
        return target
    return None


def _bound_headers_from_annotations(
    annotation_labels: list[str],
    *,
    header_set: set[str],
    lower_to_header: dict[str, str],
    annotation_aliases: dict[str, str],
    non_variable_annotations: set[str],
    true_missing_annotations: set[str],
) -> set[str]:
    bound: set[str] = set()
    for label in set(annotation_labels):
        if label in non_variable_annotations or label in true_missing_annotations:
            continue
        target = _resolve_annotation_header(
            label,
            header_set=header_set,
            lower_to_header=lower_to_header,
            annotation_aliases=annotation_aliases,
        )
        if target is not None:
            bound.add(target)
    return bound


def _pdf_field_count_mismatch_discrepancy(
    *,
    headers: list[str],
    missing_headers: list[str],
    annotation_labels: list[str],
    header_set: set[str],
    lower_to_header: dict[str, str],
    annotation_aliases: dict[str, str],
    non_variable_annotations: set[str],
    true_missing_annotations: set[str],
) -> dict[str, Any] | None:
    """Return a hold discrepancy when PDF-bound headers do not reconcile bindable columns."""
    bindable = _bindable_headers(headers)
    bound = _bound_headers_from_annotations(
        annotation_labels,
        header_set=header_set,
        lower_to_header=lower_to_header,
        annotation_aliases=annotation_aliases,
        non_variable_annotations=non_variable_annotations,
        true_missing_annotations=true_missing_annotations,
    )
    accounted = bound | set(missing_headers)
    if accounted == bindable:
        return None
    return {
        "kind": PDF_FIELD_COUNT_MISMATCH_KIND,
        "where": "PDF annotations vs dataset row-1 headers",
        "pdf_annotation_says": {
            "bound_header_count": len(bound),
            "binding_like_label_count": len(
                {
                    label
                    for label in set(annotation_labels)
                    if label not in non_variable_annotations
                    and label not in true_missing_annotations
                    and _resolve_annotation_header(
                        label,
                        header_set=header_set,
                        lower_to_header=lower_to_header,
                        annotation_aliases=annotation_aliases,
                    )
                    is not None
                }
            ),
        },
        "printed_form_truth": (
            "PDF-bound header keys plus headers without visible widgets do not "
            "account for every bindable dataset column"
        ),
        "dataset_column_binding": {
            "bindable_column_count": len(bindable),
            "unaccounted_headers": sorted(bindable - accounted),
        },
        "resolution": "Held for human review before policy auto-publish",
    }


def _annotation_aliases_for(form: str, header_set: set[str]) -> dict[str, str]:
    aliases = dict(ANNOTATION_ALIASES.get(form, {}))
    # Case-only mismatches are aliases, not missing variables.
    lower_to_header = {header.lower(): header for header in header_set}
    for label in list(aliases):
        if aliases[label] not in header_set:
            aliases.pop(label)
    return {
        label: target
        for label, target in aliases.items()
        if target in header_set or label.lower() in lower_to_header
    }


def _apply_tst_mutex(variables: dict[str, dict[str, Any]]) -> None:
    for row in range(1, 7):
        nd = f"TST_ND{row}"
        if nd not in variables:
            continue
        partners = [
            name
            for name in variables
            if name != nd
            and name.startswith("TST_")
            and FORM_10_TST_ROW_RE.match(name)
            and name.endswith(str(row))
        ]
        if not partners:
            continue
        variables[nd]["skip_logic"] = "; ".join(
            f"mutually exclusive with {partner} (inferred from row layout)" for partner in partners
        )
        for partner in partners:
            prior = variables[partner].get("skip_logic")
            addition = f"mutually exclusive with {nd} (inferred from row layout)"
            variables[partner]["skip_logic"] = (
                f"{prior}; {addition}" if isinstance(prior, str) and prior else addition
            )


def _append_skip(meta: dict[str, Any], text: str) -> None:
    prior = meta.get("skip_logic")
    meta["skip_logic"] = f"{prior}; {text}" if isinstance(prior, str) and prior else text


def _apply_hiv_calibration(variables: dict[str, dict[str, Any]]) -> None:
    """Preserve the 6_HIV repeatable skip/mutex calibration in fresh runs."""
    if "HIV_HIV" in variables:
        _append_skip(
            variables["HIV_HIV"],
            "if 'Negative (-)', skip to completion block per instruction I1",
        )

    pairs = [
        ("HIV_ARTDAT", "HIV_ARTND"),
        ("HIV_CD4", "HIV_CD4ND"),
        ("HIV_CD4LY", "HIV_CD4LYND"),
    ]
    for left, right in pairs:
        if left in variables:
            _append_skip(variables[left], f"mutually exclusive with {right} (inferred)")
        if right in variables:
            _append_skip(variables[right], f"mutually exclusive with {left} (inferred)")


def build_candidate(repo_root: Path, form: str, pack_path: Path) -> dict[str, Any]:
    pack = json.loads(pack_path.read_text(encoding="utf-8"))
    headers: list[str] = pack["headers"]
    pdf_path = repo_root / pack["pdf"]
    page_lines = _extract_page_lines(pdf_path)
    exact, folded, annotation_labels = _annotation_maps(pack)
    annotation_counts = Counter(annotation_labels)
    header_set = set(headers)
    all_lines = [line for page in pack.get("pages", []) for line in page.get("lines", [])]

    variables: dict[str, dict[str, Any]] = {}
    missing_headers: list[str] = []
    case_mismatch: list[dict[str, str]] = []

    for name in headers:
        if _is_system(name):
            matched = False
            prompt = None
            ann = None
        else:
            ann = exact.get(name, [None])[0]
            matched = ann is not None
            prompt = None
            if not matched:
                folded_matches = folded.get(name.lower(), [])
                if len(folded_matches) == 1:
                    ann = folded_matches[0]
                    matched = True
                    case_mismatch.append(
                        {"dataset_column": name, "pdf_annotation_says": str(ann.get("text"))}
                    )
            if matched and ann is not None:
                if _is_not_done_var(name):
                    row = re.search(r"(\d+)$", name)
                    prompt = f"Not Done (row {row.group(1)})" if row else "Not Done"
                else:
                    prompt, _prompt_location = _best_prompt_from_geometry(name, ann, page_lines)
                    if prompt is None:
                        matched = False
                prompt = _known_prompt_override(name, prompt)
        section = _section_for(name, matched)
        meta: dict[str, Any] = {"section": section}
        if matched:
            meta["pdf_question"] = prompt
        elif not _is_system(name):
            meta["pdf_question"] = None
            missing_headers.append(name)
        meta.update(_type_and_phi(name, matched))
        fmt = (
            _format_from_nearby_text(name, ann, page_lines) if matched and ann is not None else None
        )
        if fmt:
            meta["format"] = fmt
        options = _known_options_override(name)
        if options:
            meta["options"] = options
        meta["widget"] = _widget_for(name, prompt, fmt, matched)
        variables[name] = meta

    if form == "10_TST":
        _apply_tst_mutex(variables)
    if form == "6_HIV":
        _apply_hiv_calibration(variables)

    used_sections = {meta["section"] for meta in variables.values()}
    sections: dict[str, dict[str, str | None]] = {}
    if "header" in used_sections:
        sections["header"] = {"label": None, "note": "form-header band and top-form identifiers"}
    if "form_body" in used_sections:
        sections["form_body"] = {"label": _first_title(all_lines)}
    if "completion" in used_sections:
        sections["completion"] = {
            "label": None,
            "note": "form-completion signature, initials, and date fields",
        }
    if "unmatched_dataset" in used_sections:
        sections["unmatched_dataset"] = {
            "label": None,
            "note": "dataset row-1 headers with no visible printed PDF widget; see discrepancies",
        }
    if "system" in used_sections:
        sections["system"] = {
            "label": None,
            "note": "dataset-only system-generated columns with no printed section",
        }

    discrepancies: list[dict[str, Any]] = []
    header_duplicates = pack.get("header_duplicates") or {}
    if header_duplicates:
        discrepancies.append(
            {
                "kind": "dataset_duplicate_header_binding_conflict",
                "where": "dataset row-1 headers",
                "pdf_annotation_says": None,
                "printed_form_truth": "Duplicate row-1 header names require source-level review before final policy collapse",
                "dataset_column_binding": header_duplicates,
                "resolution": "Not automatically collapsed",
            }
        )
    if missing_headers:
        discrepancies.append(
            {
                "kind": "dataset_header_without_visible_pdf_widget",
                "where": "PDF annotation geometry and printed-text proximity sweep",
                "pdf_annotation_says": None,
                "printed_form_truth": "No matching visible PDF annotation and nearby printed prompt could be verified for these row-1 headers",
                "dataset_column_binding": missing_headers,
                "resolution": "Variables retained for binding only with pdf_question: null; printed PDF meaning was not invented",
            }
        )
    extra_annotations = sorted(label for label in set(annotation_labels) if label not in header_set)
    lower_to_header = {header.lower(): header for header in header_set}
    annotation_aliases = _annotation_aliases_for(form, header_set)
    for label in extra_annotations:
        if label.lower() in lower_to_header:
            annotation_aliases[label] = lower_to_header[label.lower()]
    non_variable_annotations = set(NON_VARIABLE_ANNOTATIONS.get(form, set()))
    true_missing_annotations = set(TRUE_PDF_VARIABLES_WITHOUT_DATASET_HEADER.get(form, set()))
    alias_labels = set(annotation_aliases)
    if not header_duplicates:
        mismatch = _pdf_field_count_mismatch_discrepancy(
            headers=headers,
            missing_headers=missing_headers,
            annotation_labels=annotation_labels,
            header_set=header_set,
            lower_to_header=lower_to_header,
            annotation_aliases=annotation_aliases,
            non_variable_annotations=non_variable_annotations,
            true_missing_annotations=true_missing_annotations,
        )
        if mismatch is not None:
            discrepancies.append(mismatch)
    extra_for_generic_discrepancy = [
        label
        for label in extra_annotations
        if label not in alias_labels
        and label not in non_variable_annotations
        and label not in true_missing_annotations
    ]
    repeated_expected = sorted(
        label
        for label, count in annotation_counts.items()
        if count > 1
        and label not in header_set
        and label not in alias_labels
        and label not in non_variable_annotations
        and label not in true_missing_annotations
        and _is_expected_repeated_annotation(label)
    )
    binding_duplicates = {
        label: count
        for label, count in annotation_counts.items()
        if count > 1
        and label not in alias_labels
        and label not in non_variable_annotations
        and label not in true_missing_annotations
        and (label in header_set or not _is_expected_repeated_annotation(label))
    }
    if annotation_aliases:
        curated_aliases = ANNOTATION_ALIASES.get(form, {})
        discrepancies.append(
            {
                "kind": "pdf_annotation_alias_to_dataset_header",
                "where": "PDF annotations",
                "pdf_annotation_says": [
                    {
                        "label": label,
                        "dataset_column": target,
                        "curated": label in curated_aliases and curated_aliases[label] == target,
                    }
                    for label, target in sorted(annotation_aliases.items())
                ],
                "printed_form_truth": "PDF annotation label differs from the dataset row-1 binding name, but points to the same printed field",
                "dataset_column_binding": sorted(set(annotation_aliases.values())),
                "resolution": "Dataset row-1 header retained as the variable key; PDF annotation label treated as a locator alias",
            }
        )
    if non_variable_annotations:
        discrepancies.append(
            {
                "kind": "pdf_annotation_non_variable_label",
                "where": "PDF annotations",
                "pdf_annotation_says": sorted(non_variable_annotations),
                "printed_form_truth": "Annotation label is an option, artifact, or non-variable printed label rather than a dataset field",
                "dataset_column_binding": None,
                "resolution": "Not required as a dataset variable",
            }
        )
    if true_missing_annotations:
        discrepancies.append(
            {
                "kind": "printed_widget_without_dataset_header_reviewed",
                "where": "PDF annotations",
                "pdf_annotation_says": sorted(true_missing_annotations),
                "printed_form_truth": "PDF annotation appears to identify a real printed data-entry field with no matching dataset row-1 header",
                "dataset_column_binding": None,
                "resolution": (
                    "Maintainer-reviewed source/dataset discrepancy recorded in "
                    "TRUE_PDF_VARIABLES_WITHOUT_DATASET_HEADER; no policy variable added "
                    "without a dataset binding key"
                ),
            }
        )
    if repeated_expected:
        discrepancies.append(
            {
                "kind": "pdf_annotation_repeated_option_label",
                "where": "PDF annotations",
                "pdf_annotation_says": repeated_expected,
                "printed_form_truth": "Repeated annotation labels appear to be option/table markers rather than unique dataset bindings",
                "dataset_column_binding": None,
                "resolution": "Classified as repeated printed-label signal, not duplicate variable bindings",
            }
        )
    if extra_for_generic_discrepancy:
        discrepancies.append(
            {
                "kind": "pdf_annotation_not_in_dataset_headers",
                "where": "PDF annotations",
                "pdf_annotation_says": extra_for_generic_discrepancy,
                "printed_form_truth": "PDF annotation labels are present but are not dataset row-1 headers",
                "dataset_column_binding": None,
                "resolution": "Not added to variables unless a row-1 header exists",
            }
        )
    if binding_duplicates:
        discrepancies.append(
            {
                "kind": "pdf_annotation_duplicate_or_mislabel",
                "where": "PDF annotations",
                "pdf_annotation_says": binding_duplicates,
                "printed_form_truth": "Duplicate binding-like annotation labels require visual review against printed widgets",
                "dataset_column_binding": None,
                "resolution": "Dataset row-1 headers remain binding keys; duplicate annotation labels are not treated as separate variables",
            }
        )
    if case_mismatch:
        discrepancies.append(
            {
                "kind": "pdf_annotation_duplicate_or_mislabel",
                "where": "PDF annotations",
                "pdf_annotation_says": case_mismatch,
                "printed_form_truth": "Dataset header binding differs from PDF annotation capitalization",
                "dataset_column_binding": [entry["dataset_column"] for entry in case_mismatch],
                "resolution": "Dataset row-1 header spelling retained as variable key; printed geometry used only as a locator",
            }
        )

    data: dict[str, Any] = {
        "study": STUDY_NAME,
        "form": {
            "number": _form_number(form),
            "title": _first_title(all_lines),
            "version": _form_version(pack.get("pdf", "")),
            "page_count": pack.get("page_count") or len(pack.get("pages", [])),
        },
        "sections": sections,
        "variables": variables,
    }
    if form == "6_HIV":
        data["instructions"] = [
            {
                "id": "I1",
                "text": "If negative, skip to bottom of form, sign and enter date.",
                "location": "standalone italic line below the HIV Test Result row",
            }
        ]
    if discrepancies:
        data["discrepancies"] = discrepancies
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--form", required=True)
    parser.add_argument("--source-pack", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    pack_path = args.source_pack or Path(f"/tmp/sot_source_pack_{args.form}.json")
    data = build_candidate(repo_root, args.form, pack_path)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=False, width=120), encoding="utf-8"
    )
    print(f"candidate written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
