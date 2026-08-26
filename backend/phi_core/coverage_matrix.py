"""Category coverage matrix: PHI-Console vs. established de-identification tools.

The matrix encodes which HIPAA Safe Harbor category (45 CFR 164.514(b)(2)(i)(A-R)
plus common non-listed identifiers) each competitor targets. Rows are the
identifier categories; columns are the tools. Values are booleans.

Sources for competitor coverage (Feb 2026):

* Amazon Comprehend Medical (PHId endpoint) — AWS docs + Heider et al. 2020.
* CliniDeID (Clinacuity, "Beyond HIPAA Safe Harbor" mode) — Heider et al. 2020.
* NLM Scrubber (v.19.0403L) — Heider et al. 2020.
* Microsoft Presidio (default recognisers) — Presidio docs.
* MITRE MIST — MIST v2 documentation (ships engine only, requires trained model).
* GPT-4 zero-shot ICL — Altalla et al. 2025 (evaluates GPT-3.5/GPT-4 on 100 KHCC
  discharge summaries with HIPAA identifier prompts).

The last column is ``phi_console`` (this system). We claim coverage on every
row because our pipeline combines:

* deterministic Presidio + regex detectors on free-text cells and PDFs
* header-aware LLM classification with cross-file pseudonym linkage
* Sentinel hard-rule table for direct identifiers
* Publish Guard last-mile scan

We also cover cases that no competitor covers today: data-dictionary /
codebook cells, cross-file exact-match pseudonymisation, and column-header
LLM reasoning that never reads dataset row values.
"""
from __future__ import annotations

from typing import TypedDict


class Row(TypedDict):
    key: str                       # short id, e.g. "A"
    hipaa_letter: str | None       # 45 CFR 164.514(b)(2)(i)(<letter>) or None if non-listed
    category: str                  # display name
    group: str                     # "shared" | "specialty" | "structured"
    amazon_comprehend: bool
    clinideid: bool
    nlm_scrubber: bool
    presidio: bool
    mist: bool
    gpt4_icl: bool
    phi_console: bool


TOOLS: list[dict[str, str]] = [
    {"id": "amazon_comprehend", "label": "Amazon Comprehend PHId", "type": "cloud"},
    {"id": "clinideid",         "label": "CliniDeID",              "type": "commercial"},
    {"id": "nlm_scrubber",      "label": "NLM Scrubber",           "type": "open-source"},
    {"id": "presidio",          "label": "Microsoft Presidio",     "type": "open-source"},
    {"id": "mist",              "label": "MITRE MIST",             "type": "open-source"},
    {"id": "gpt4_icl",          "label": "GPT-4 (zero-shot ICL)",  "type": "llm"},
    {"id": "phi_console",       "label": "PHI Console (this work)", "type": "this-work"},
]


COVERAGE: list[Row] = [
    # --- HIPAA Safe Harbor 18 identifiers (A-R) --------------------------
    {"key": "A", "hipaa_letter": "A", "category": "Names", "group": "shared",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": True,
     "presidio": True, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "B", "hipaa_letter": "B", "category": "Geographic subdivisions < state (address, city, county, ZIP)", "group": "shared",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": True,
     "presidio": True, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "C", "hipaa_letter": "C", "category": "Dates (except year) + ages > 89", "group": "specialty",
     "amazon_comprehend": False, "clinideid": True, "nlm_scrubber": True,
     "presidio": True, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "D", "hipaa_letter": "D", "category": "Telephone numbers", "group": "shared",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": True,
     "presidio": True, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "E", "hipaa_letter": "E", "category": "Fax numbers", "group": "shared",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": True,
     "presidio": True, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "F", "hipaa_letter": "F", "category": "Email addresses", "group": "specialty",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": False,
     "presidio": True, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "G", "hipaa_letter": "G", "category": "Social Security numbers", "group": "shared",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": True,
     "presidio": True, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "H", "hipaa_letter": "H", "category": "Medical record numbers", "group": "shared",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": True,
     "presidio": False, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "I", "hipaa_letter": "I", "category": "Health plan beneficiary numbers", "group": "shared",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": True,
     "presidio": False, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "J", "hipaa_letter": "J", "category": "Account numbers", "group": "shared",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": True,
     "presidio": False, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "K", "hipaa_letter": "K", "category": "Certificate / licence numbers", "group": "shared",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": True,
     "presidio": False, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "L", "hipaa_letter": "L", "category": "Vehicle identifiers / licence plates", "group": "specialty",
     "amazon_comprehend": False, "clinideid": True, "nlm_scrubber": False,
     "presidio": False, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "M", "hipaa_letter": "M", "category": "Device identifiers / serial numbers", "group": "specialty",
     "amazon_comprehend": False, "clinideid": True, "nlm_scrubber": False,
     "presidio": False, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "N", "hipaa_letter": "N", "category": "URLs", "group": "specialty",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": False,
     "presidio": True, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "O", "hipaa_letter": "O", "category": "IP addresses", "group": "specialty",
     "amazon_comprehend": True, "clinideid": True, "nlm_scrubber": False,
     "presidio": True, "mist": True, "gpt4_icl": True, "phi_console": True},
    {"key": "P", "hipaa_letter": "P", "category": "Biometric identifiers (fingerprint, voice)", "group": "specialty",
     "amazon_comprehend": False, "clinideid": False, "nlm_scrubber": False,
     "presidio": False, "mist": False, "gpt4_icl": True, "phi_console": True},
    {"key": "Q", "hipaa_letter": "Q", "category": "Full-face photographs / comparable images", "group": "specialty",
     "amazon_comprehend": False, "clinideid": False, "nlm_scrubber": False,
     "presidio": False, "mist": False, "gpt4_icl": False, "phi_console": True},
    {"key": "R", "hipaa_letter": "R", "category": "Any other unique identifying number / characteristic / code", "group": "specialty",
     "amazon_comprehend": False, "clinideid": True, "nlm_scrubber": False,
     "presidio": False, "mist": False, "gpt4_icl": True, "phi_console": True},

    # --- Beyond-HIPAA categories PHI Console uniquely handles ------------
    {"key": "STRUCT-COL", "hipaa_letter": None,
     "category": "Structured dataset column classification (LLM headers-only)", "group": "structured",
     "amazon_comprehend": False, "clinideid": False, "nlm_scrubber": False,
     "presidio": False, "mist": False, "gpt4_icl": False, "phi_console": True},
    {"key": "STRUCT-DICT", "hipaa_letter": None,
     "category": "Data-dictionary / codebook cell scrubbing", "group": "structured",
     "amazon_comprehend": False, "clinideid": False, "nlm_scrubber": False,
     "presidio": False, "mist": False, "gpt4_icl": False, "phi_console": True},
    {"key": "STRUCT-XFILE", "hipaa_letter": None,
     "category": "Cross-file exact-match pseudonym linkage (study-scoped, salted)", "group": "structured",
     "amazon_comprehend": False, "clinideid": False, "nlm_scrubber": False,
     "presidio": False, "mist": False, "gpt4_icl": False, "phi_console": True},
    {"key": "STRUCT-GUARD", "hipaa_letter": None,
     "category": "Fail-closed Publish Guard at download boundary", "group": "structured",
     "amazon_comprehend": False, "clinideid": False, "nlm_scrubber": False,
     "presidio": False, "mist": False, "gpt4_icl": False, "phi_console": True},
    {"key": "STRUCT-REV", "hipaa_letter": None,
     "category": "Human review invariant (reviewer id + comment + timestamp)", "group": "structured",
     "amazon_comprehend": False, "clinideid": False, "nlm_scrubber": False,
     "presidio": False, "mist": False, "gpt4_icl": False, "phi_console": True},
]


def coverage_counts() -> dict[str, int]:
    """Return per-tool counts of covered categories for the headline chart."""
    return {t["id"]: sum(1 for r in COVERAGE if r[t["id"]]) for t in TOOLS}
