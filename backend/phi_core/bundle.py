"""Bundle builder: assemble the operator's shareable ZIP.

Two tiers:

* **Default (safe_to_share)** — the redacted study exports plus a signed
  attestation (JSON + human-readable TXT) and a README that names the
  jurisdiction and cites the exact HIPAA clauses this run satisfied.

* **Publication add-on** (opt-in) — everything above plus a ``publication/``
  folder with the coverage matrix rendered as CSV + PNG, the Herald paper
  drafts (abstract/methods/results), the real per-dataset benchmark report
  (markdown/JSON/CSV/figures) for corpus runs, and a manifest.

The bundle is streamed via a normal ZIP so the operator gets one artefact
they can hand to a reviewer.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .coverage_matrix import COVERAGE, TOOLS, coverage_counts


BUNDLE_VERSION = "1.0.0"


@dataclass
class BundleOptions:
    include_publication: bool = False
    include_attestation_pdf: bool = False


def _sha256_of_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _sha256_of_path(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _write_coverage_csv() -> bytes:
    """Table 1: category coverage vs. every established competitor."""
    out = io.StringIO()
    w = csv.writer(out)
    header = ["hipaa_letter", "category", "group"] + [t["id"] for t in TOOLS]
    w.writerow(header)
    for row in COVERAGE:
        w.writerow([row["hipaa_letter"] or "-", row["category"], row["group"]]
                   + [1 if row[t["id"]] else 0 for t in TOOLS])
    return out.getvalue().encode("utf-8")


def _render_coverage_png() -> bytes:
    """Coverage heatmap: rows = categories, cols = tools. Publication-ready."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    tool_ids = [t["id"] for t in TOOLS]
    tool_labels = [t["label"] for t in TOOLS]
    rows = COVERAGE
    row_labels = [f"({r['hipaa_letter']}) {r['category'][:52]}" if r["hipaa_letter"]
                  else f"[+] {r['category'][:52]}" for r in rows]
    data = np.array([[1 if r[tid] else 0 for tid in tool_ids] for r in rows], dtype=float)

    fig_h = max(6, 0.42 * len(rows))
    fig, ax = plt.subplots(figsize=(11, fig_h))

    # Oxblood/paper palette to match the app.
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list(
        "phi", [(0.0, "#EFEBE3"), (1.0, "#8C2135")], N=32)
    ax.imshow(data, cmap=cmap, aspect="auto", vmin=0, vmax=1)

    # Highlight the phi_console column with a bold black outline
    console_col = tool_ids.index("phi_console")
    for r in range(len(rows)):
        rect = plt.Rectangle((console_col - 0.5, r - 0.5), 1, 1,
                             fill=False, edgecolor="#12141A", linewidth=1.4)
        ax.add_patch(rect)

    ax.set_xticks(range(len(tool_labels)))
    ax.set_xticklabels(tool_labels, rotation=35, ha="right", fontsize=9, color="#12141A")
    ax.set_yticks(range(len(row_labels)))
    ax.set_yticklabels(row_labels, fontsize=8, color="#12141A")
    ax.tick_params(axis="both", length=0)

    ax.set_title("HIPAA-Category Coverage: PHI Console vs. Established Tools",
                 loc="left", fontsize=13, pad=14, color="#12141A", weight="bold")

    # Counts summary below
    counts = coverage_counts()
    caption = "  ".join(f"{t['label'].split(' (')[0]}: {counts[t['id']]}/{len(rows)}"
                        for t in TOOLS)
    fig.text(0.5, -0.02, caption, ha="center", fontsize=8, color="#6B6E76")

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor="#F7F5F0")
    plt.close(fig)
    return buf.getvalue()


def _render_coverage_counts_bar() -> bytes:
    """Bar chart of total categories covered per tool — the headline claim."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    counts = coverage_counts()
    labels = [t["label"] for t in TOOLS]
    values = [counts[t["id"]] for t in TOOLS]
    colors = ["#B8B0A0"] * len(TOOLS)
    colors[-1] = "#8C2135"  # our system

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.barh(labels, values, color=colors, edgecolor="#12141A", linewidth=0.6)
    ax.set_xlim(0, len(COVERAGE) + 2)
    ax.set_xlabel(f"Categories covered (of {len(COVERAGE)} total)",
                  fontsize=10, color="#12141A")
    ax.set_title("Total identifier-category coverage",
                 loc="left", fontsize=13, pad=14, color="#12141A", weight="bold")
    ax.invert_yaxis()
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(axis="both", colors="#12141A")

    for bar, v in zip(bars, values):
        ax.text(v + 0.3, bar.get_y() + bar.get_height() / 2, f"{v}",
                va="center", fontsize=10, color="#12141A")

    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight",
                facecolor="#F7F5F0")
    plt.close(fig)
    return buf.getvalue()


# ------------------------------------------------------------------------
# Attestation
# ------------------------------------------------------------------------

def _attestation_payload(session: dict[str, Any], file_hashes: dict[str, str]) -> dict[str, Any]:
    """Machine-readable attestation. All values plain JSON."""
    review_history = session.get("session_review") or {}
    # session_review is append-only (list of per-submission entries) as of
    # the conversational human-review redesign; a bare dict is the legacy
    # single-submission shape from before that change.
    if isinstance(review_history, list):
        review = review_history[-1] if review_history else {}
    else:
        review_history = [review_history] if review_history else []
        review = review_history[-1] if review_history else {}
    guard = session.get("guard_report") or {}
    # Reviewer trail: prefer session_review; fall back to the most recent
    # per-decision reviewer for older sessions run before the session-level
    # invariant landed. `reviewer`/`comment`/`reviewed_at` reflect the most
    # recent round for display; `actual_knowledge_ack` is a compliance
    # boolean that must NOT reset to false just because a later round only
    # deferred more columns (session_human_review only ever stores ack=True
    # on a round that actually resolved something, and only after the
    # client's own ack was validated -- so any historical True is still a
    # true attestation for the columns that round exported).
    from .security import scrub_persisted_text as _scrub_text
    reviewer = review.get("reviewer") or None
    reviewer_comment = review.get("comment") or None
    reviewed_at = review.get("reviewed_at") or None
    actual_knowledge_ack = any(
        bool(entry.get("actual_knowledge_ack")) for entry in review_history if isinstance(entry, dict)
    )
    if not reviewer:
        for d in reversed(session.get("agent_decisions") or []):
            if isinstance(d, dict) and d.get("reviewer"):
                reviewer = d.get("reviewer")
                reviewer_comment = d.get("reviewer_comment") or reviewer_comment
                reviewed_at = d.get("reviewed_at") or reviewed_at
                if d.get("actual_knowledge_ack") is True:
                    actual_knowledge_ack = True
                break
    if reviewer_comment:
        reviewer_comment = _scrub_text(reviewer_comment)
    # Columns still awaiting review are never silently omitted from the
    # export -- they are named explicitly here so a partial bundle's
    # attestation cannot be mistaken for a complete run's once the
    # human-readable manifest/README is separated from it.
    withheld_columns = [
        {"file_id": entry.get("file_id"), "column": entry.get("column")}
        for entry in (session.get("pending_review") or [])
        if isinstance(entry, dict)
    ]
    is_partial = bool(withheld_columns) or session.get("status") == "partially_complete"
    if is_partial:
        actual_knowledge_statement = (
            "The reviewer has attested that they have no actual knowledge that the "
            "information resolved in this submission -- excluding the columns listed "
            "under `withheld_columns`, which remain pending human review and are not "
            "covered by this statement -- alone or in combination with other reasonably "
            "available information could be used to identify an individual."
        )
    else:
        actual_knowledge_statement = (
            "The reviewer has attested that they have no actual knowledge that "
            "the remaining information alone or in combination with other reasonably "
            "available information could be used to identify an individual."
        )
    return {
        "attestation_version": BUNDLE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session.get("id"),
        "jurisdiction": session.get("jurisdiction") or "us",
        "regulation": "HIPAA Privacy Rule 45 CFR 164.514(b)(2)(i)",
        "method": "Safe Harbor + deterministic Publish Guard",
        "publish_guard": {
            "status": guard.get("status"),
            "scanned": guard.get("scanned", 0),
            "blocked": guard.get("blocked", 0),
        },
        "reviewer": reviewer,
        "reviewer_comment": reviewer_comment,
        "reviewed_at": reviewed_at,
        "actual_knowledge_ack": actual_knowledge_ack,
        "actual_knowledge_cite": "45 CFR 164.514(b)(2)(ii)",
        "actual_knowledge_statement": actual_knowledge_statement,
        "is_partial": is_partial,
        "withheld_columns": withheld_columns,
        "files": file_hashes,
        "system": {
            "name": "PHI Console",
            "version": BUNDLE_VERSION,
            "notes": (
                "LLM only reads column headers of structured datasets; "
                "free-text cells scrubbed via Presidio + regex; "
                "cross-file pseudonym linkage salted per study."
            ),
        },
    }


def _attestation_text(att: dict[str, Any]) -> str:
    ak_ack = "YES" if att.get("actual_knowledge_ack") else "NO"
    lines = [
        "PHI CONSOLE — ATTESTATION OF DE-IDENTIFICATION",
        "=" * 54,
        "",
        f"Session id       : {att['session_id']}",
        f"Generated at     : {att['generated_at']}",
        f"Jurisdiction     : {att['jurisdiction']}",
        f"Method           : {att['method']}",
        f"Regulation cite  : {att['regulation']}",
        "",
        "Publish Guard    : {status}  ({scanned} file(s) scanned, {blocked} blocked)".format(
            **att["publish_guard"]),
        "",
        "Reviewer         : {}".format(att["reviewer"] or "(none)"),
        "Reviewer comment : {}".format(att["reviewer_comment"] or "(none)"),
        "Reviewed at      : {}".format(att["reviewed_at"] or "(none)"),
        "",
        f"Actual-knowledge attestation ({att.get('actual_knowledge_cite','45 CFR 164.514(b)(2)(ii)')}): {ak_ack}",
        f"  Statement: {att.get('actual_knowledge_statement','')}",
    ]
    if att.get("is_partial"):
        lines += [
            "",
            "PARTIAL BUNDLE — the following column(s) are still pending human review",
            "and are withheld entirely from this export (not defaulted, not blanked):",
        ]
        for w in att.get("withheld_columns") or []:
            lines.append(f"  {w.get('file_id')} :: {w.get('column')}")
    lines += [
        "",
        "Files included in this bundle (SHA-256):",
    ]
    for path, digest in att["files"].items():
        lines.append(f"  {digest}  {path}")
    lines += [
        "",
        "This attestation is a machine-checkable receipt. Consumers of this bundle",
        "may re-hash every file and match against `attestation.json` to verify.",
        "",
        "GOAL invariants for this bundle:",
        "  (a) The LLM never read dataset row values — headers only.",
        "  (b) Every emitted file passed the deterministic Publish Guard.",
        "  (c) BYO API keys used during processing are Fernet-encrypted at rest.",
        "  (d) Clinical / epidemiological signal preserved by Safe Harbor transforms.",
        "  (e) Cross-file pseudonym linkage is exact-match, salted per study under a",
        "      server-held key that is never included in this bundle.",
    ]
    return "\n".join(lines) + "\n"


def _readme(session_id: str, jurisdiction: str) -> str:
    return f"""# PHI-handled study bundle

**Session**: `{session_id}`
**Jurisdiction**: {jurisdiction.upper()}
**Regulation**: HIPAA Privacy Rule 45 CFR 164.514(b)(2)(i) — Safe Harbor.

## Contents

```
safe_to_share/
├── datasets/             # PHI-handled datasets (CSV/XLSX)
├── forms/                # PHI-handled forms (redacted text)
├── dictionary/           # PHI-handled data dictionary / mapping
├── attestation.json      # machine-readable attestation
├── attestation.txt       # human-readable attestation
└── README.md             # this file
```

If the bundle contains a `publication/` folder, it holds paper-ready tables,
figures and drafts that describe how these outputs were produced and how
this system compares to established de-identification tools.

## How this bundle was produced

1. **Intake v3** validates the study package structure (datasets / forms /
   dictionary components).
2. Twelve agents classify each dataset column using **only the column
   header** plus the data dictionary and any accompanying forms — never a
   row value.
3. The **Executor** applies the chosen action per column (drop /
   pseudonymize / cap_age_90 / year_only / zip3_truncate / scrub_text).
4. The **Publish Guard** runs a deterministic PHI scan (SSN, phone,
   email, full DOB, restricted ZIP3, age > 89) on every output; downloads
   are refused unless the guard clears.
5. Every changed decision carries a reviewer id + comment + timestamp.

## Verification

Re-hash any file with SHA-256 and match against `attestation.json`.

```
$ sha256sum datasets/*.csv
```

The value in `attestation.files["<relative_path>"]` must match.
"""


# ------------------------------------------------------------------------
# Publication add-on
# ------------------------------------------------------------------------

def _paper_readme() -> str:
    return """# Publication add-on

This folder holds artefacts for writing up a de-identification paper using
this run:

* `paper/tables/table_1_category_coverage.csv` — HIPAA identifier coverage
  vs. Amazon Comprehend PHId, CliniDeID, NLM Scrubber, Microsoft Presidio,
  MITRE MIST, and GPT-4 (zero-shot ICL).
* `paper/figures/fig1_category_coverage.png` — heatmap version of the same
  table with our system column highlighted.
* `paper/figures/fig2_category_totals.png` — bar chart of total categories
  covered per tool.
* `paper/methods.md`, `paper/results.md`, `paper/discussion.md` — draft
  paper sections composed by the Herald agent.
* `paper/references.bib` — BibTeX citations (HHS guidance, Heider 2020,
  Altalla 2025, Presidio, MIST).
* `benchmark/` — scaffolding for gold-annotated F1 comparisons; populated
  once the operator supplies a gold corpus.

Cite this bundle as: PHI Console, session `<session_id>`, generated on
`<generated_at>`. All artefacts are reproducible from the input study
package and the SHA-256 hashes in `attestation.json`.
"""


def _references_bib() -> str:
    return r"""@misc{hhs_deid,
  title  = {Guidance Regarding Methods for De-identification of Protected Health Information},
  author = {{U.S. Department of Health and Human Services, Office for Civil Rights}},
  year   = {2012, reviewed 2025},
  url    = {https://www.hhs.gov/hipaa/for-professionals/special-topics/de-identification/index.html},
}

@article{heider2020,
  title   = {A Comparative Analysis of Speed and Accuracy for Three Off-the-Shelf De-Identification Tools},
  author  = {Heider, Paul M. and Obeid, Jihad S. and Meystre, St{\'e}phane M.},
  journal = {AMIA Joint Summits on Translational Science Proceedings},
  volume  = {2020},
  pages   = {241--250},
  year    = {2020},
  pmid    = {32477643},
}

@article{altalla2025,
  title   = {Evaluating GPT models for clinical note de-identification},
  author  = {Altalla', Bayan and Al-Omari, Hadi and Alqasem, Rasha and others},
  journal = {Scientific Reports},
  year    = {2025},
  doi     = {10.1038/s41598-025-86890-3},
}

@misc{presidio,
  title  = {Microsoft Presidio},
  author = {{Microsoft}},
  url    = {https://github.com/microsoft/presidio},
}

@misc{mist,
  title  = {MIST: MITRE Identification Scrubber Toolkit},
  author = {{The MITRE Corporation}},
  url    = {http://mist-deid.sourceforge.net/},
}
"""


def _methods_md(session: dict[str, Any]) -> str:
    j = session.get("jurisdiction") or "us"
    return f"""# Methods

## System

PHI Console is a twelve-agent LLM pipeline for de-identifying study
packages that combine structured datasets, free-text forms, and a data
dictionary. The system enforces four invariants:

1. **Headers-only LLM on structured data.** The LLM receives column
   headers together with the data-dictionary row for that column and any
   accompanying form context. Row values are never sent to the LLM. Free
   text inside dataset cells is redacted by a deterministic pipeline of
   Presidio and category-specific regular expressions.
2. **Cross-file exact-match pseudonymisation.** A study-scoped salted
   registry ensures the same real value produces the same pseudonym in
   every dataset in the same study, and different values never collide.
3. **Fail-closed Publish Guard.** After the Executor emits files, a
   deterministic scanner (SSN, phone, email, full DOB, restricted ZIP3,
   age > 89) inspects every output; downloads are refused unless the
   guard clears.
4. **Human-review invariant.** Every changed decision carries a reviewer
   identity, an optional comment, and a UTC timestamp.

## Regulation

Jurisdiction: {j.upper()}. The de-identification method used is the HIPAA
Safe Harbor as defined at 45 CFR 164.514(b)(2)(i)(A)-(R):

* Ages > 89 aggregated to `90+` (Safe Harbor clause C).
* All dates directly related to an individual truncated to year.
* ZIP codes reduced to their initial three digits, with the seventeen
  restricted ZIP3 codes remapped to `000`.
* All eighteen categories A-R detected via a combination of Sentinel
  hard-rules, LLM classification on column headers, and deterministic
  scrubbing on free text.

## Comparators

The coverage matrix in Table 1 compares PHI Console against six
established de-identification tools: Amazon Comprehend PHId, Clinacuity
CliniDeID (Beyond HIPAA Safe Harbor mode), NLM Scrubber, Microsoft
Presidio, MITRE MIST, and GPT-4 in a zero-shot in-context-learning
setting.
"""


def _results_md(session: dict[str, Any]) -> str:
    guard = session.get("guard_report") or {}
    return f"""# Results

## Publish Guard verdict

Status: **{guard.get("status", "n/a")}**. {guard.get("scanned", 0)} file(s)
scanned; {guard.get("blocked", 0)} blocked.

## Category coverage vs. established tools

Figure 1 (`fig1_category_coverage.png`) and Table 1
(`table_1_category_coverage.csv`) present a side-by-side comparison of
which HIPAA identifier categories each tool targets. PHI Console covers
every A-R identifier plus five categories that no existing off-the-shelf
tool addresses today:

* Structured dataset column classification with LLM restricted to
  headers.
* Data-dictionary and codebook cell scrubbing.
* Cross-file exact-match pseudonymisation with per-study salting.
* Fail-closed Publish Guard at the download boundary.
* Machine-checkable reviewer invariant.

Figure 2 (`fig2_category_totals.png`) reports the total number of
categories covered per tool.

## Per-category precision / recall / F1

The benchmark harness in `benchmark/` computes precision, recall and F1
per HIPAA category once a gold-annotated corpus is provided. Reference
numbers for Amazon Comprehend PHId, CliniDeID and NLM Scrubber on the
2014 and 2016 i2b2 corpora are drawn from Heider et al. 2020 for context.
"""


def _discussion_md() -> str:
    return """# Discussion

The coverage advantage in Table 1 is driven by design choices that were
absent from prior work:

* **Header-only reasoning.** Existing free-text de-identifiers assume the
  input is unstructured clinical text. When the input is a structured
  dataset — the majority case for study packages — reading every row
  through an LLM both inflates cost and creates a leakage surface. PHI
  Console classifies at the header layer only.
* **Dictionary and codebook scrubbing.** Data dictionaries frequently
  quote a real patient name or contact detail as an "example". None of
  the reviewed tools scrub these files. PHI Console applies the same
  deterministic detectors used on free text.
* **Cross-file linkage.** Analytical utility depends on being able to
  join two anonymised tables on a shared identifier. Prior tools either
  drop the identifier (destroying joinability) or hash it without
  study-scoped salting (allowing cross-study linkage). PHI Console emits
  a stable per-study pseudonym.
* **Publish Guard.** No prior tool refuses to serve an export it just
  produced. PHI Console runs a deterministic residual-PHI scan on the
  emitted artefacts and requires either a clean verdict or an explicit
  operator override before releasing the download URL.
* **Human-review invariant.** Prior tools log decisions but do not carry
  reviewer identity into the artefact. PHI Console persists reviewer
  identity, comment, and timestamp on every changed decision and inside
  the signed attestation.
"""


# ------------------------------------------------------------------------
# Public entry point
# ------------------------------------------------------------------------

def build_bundle(session: dict[str, Any], opts: BundleOptions,
                  agent_log: list[dict[str, Any]] | None = None) -> tuple[bytes, str]:
    """Return (zip_bytes, filename). ``agent_log`` unlocks the real
    per-dataset benchmark's context_hygiene section in the publication
    add-on; omit it and that section reports itself unavailable."""
    sid = session.get("id") or "session"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    buf = io.BytesIO()

    file_hashes: dict[str, str] = {}

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # --- safe_to_share/ contents --------------------------------------
        export_paths = session.get("export_paths") or {}
        files_meta = {f["file_id"]: f for f in (session.get("files") or [])}
        guard_results = ((session.get("guard_report") or {}).get("results") or [])
        clean_ids = {
            r.get("file_id")
            for r in guard_results
            if r.get("status") == "clean"
        }
        clean_ids = {
            file_id
            for file_id in clean_ids
            if sum(r.get("file_id") == file_id for r in guard_results) == 1
        }
        for file_id, ep in export_paths.items():
            if file_id not in clean_ids:
                continue
            if not ep or not Path(ep).exists():
                continue
            src = Path(ep)
            meta = files_meta.get(file_id, {})
            kind = meta.get("kind", "narrative")
            folder = {"dataset": "datasets", "metadata": "dictionary",
                      "narrative": "forms"}.get(kind, "misc")
            arcname = f"safe_to_share/{folder}/{meta.get('original_name', src.name)}"
            data = src.read_bytes()
            zf.writestr(arcname, data)
            file_hashes[arcname] = _sha256_of_bytes(data)

        from .crypto import sign_bytes, signing_public_key_pem
        att = _attestation_payload(session, file_hashes)
        pubkey_pem = signing_public_key_pem()
        att["signed"] = pubkey_pem is not None
        att_json = json.dumps(att, indent=2).encode("utf-8")
        att_txt = _attestation_text(att).encode("utf-8")
        zf.writestr("safe_to_share/attestation.json", att_json)
        zf.writestr("safe_to_share/attestation.txt", att_txt)
        if pubkey_pem is not None:
            sig = sign_bytes(att_json)
            zf.writestr("safe_to_share/attestation.sig", sig)
            zf.writestr("safe_to_share/attestation_pubkey.pem", pubkey_pem)
        zf.writestr("safe_to_share/README.md",
                    _readme(sid, att["jurisdiction"]).encode("utf-8"))

        # --- publication/ add-on ----------------------------------------
        if opts.include_publication:
            zf.writestr("publication/README.md", _paper_readme().encode("utf-8"))
            zf.writestr("publication/paper/tables/table_1_category_coverage.csv",
                        _write_coverage_csv())
            zf.writestr("publication/paper/figures/fig1_category_coverage.png",
                        _render_coverage_png())
            zf.writestr("publication/paper/figures/fig2_category_totals.png",
                        _render_coverage_counts_bar())
            zf.writestr("publication/paper/methods.md",
                        _methods_md(session).encode("utf-8"))
            zf.writestr("publication/paper/results.md",
                        _results_md(session).encode("utf-8"))
            zf.writestr("publication/paper/discussion.md",
                        _discussion_md().encode("utf-8"))
            # Herald draft (best-effort — Herald may have timed out earlier)
            herald = session.get("agent_herald") or {}
            if isinstance(herald, dict) and herald.get("abstract"):
                zf.writestr("publication/paper/abstract.md",
                            str(herald["abstract"]).encode("utf-8"))
            zf.writestr("publication/paper/references.bib",
                        _references_bib().encode("utf-8"))
            # Benchmark: real per-column figures when this is a corpus run;
            # otherwise a one-line note, since the benchmark needs planted
            # ground truth to grade against.
            from phi_corpus.benchmark import report_from_session, write as _write_benchmark
            bench_report = report_from_session(session, agent_log)
            if bench_report is not None:
                import tempfile as _tempfile
                with _tempfile.TemporaryDirectory(prefix="phi-bundle-bench-") as _bdir:
                    written = _write_benchmark(bench_report, Path(_bdir))
                    for _key, _path in written.items():
                        zf.writestr(f"publication/benchmark/{Path(_path).name}", Path(_path).read_bytes())
            else:
                zf.writestr("publication/benchmark/README.md",
                            (
                                "This session has no planted ground truth (not a corpus run), "
                                "so the per-dataset benchmark report does not apply. Generate a "
                                "corpus via POST /api/corpus/study/run to unlock it.\n"
                            ).encode("utf-8"))

    return buf.getvalue(), f"phi_console_{sid[:12]}_{ts}.zip"
