"""
Modified Deidentify Benchmark Adapter
======================================
Authority: arXiv 2509.14464v1 (EMNLP 2025)
  "Modified Deidentify: A Fine-Tuned Specialist Model for Clinical PHI
  De-identification"

Key performance claim from the paper (Table 3, AHS evaluation set, n=500):
  Modified Deidentify: 22/500 (4%) clinically dangerous false positives
  Llama-3.3 70B baseline:  89/500 (18%) clinically dangerous false positives
  => 4x reduction in clinically dangerous false positives over Llama-3.3 70B

This makes Modified Deidentify the current state-of-the-art open-source
fine-tuned specialist comparator for PHI de-identification as of 2025.

LICENSE STATUS: PENDING VERIFICATION.
Do not use in production until the model license has been confirmed.
The adapter stub is provided for interface-compatibility and corpus
benchmarking scaffolding only. No model weights are bundled here.

Mapping note:
  The paper uses the i2b2 2014 PHI taxonomy as its output schema.
  Our corpus uses an extended taxonomy (see authorities/AUTHORITY_MATRIX.md).
  The entity_type_map inside this adapter must be kept in sync with any
  taxonomy changes.

Usage (once model weights are available and license is confirmed):
  adapter = ModifiedDeidentifyAdapter(model_path="/path/to/weights")
  results = adapter.run_corpus(Path("corpus/structured/records.jsonl"))
  scores  = adapter.score(results, Path("corpus/structured/gold.jsonl"))
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ModifiedDeidentifyResult:
    """Single-record output from the Modified Deidentify model.

    Fields
    ------
    record_id : str
        Matches the ``id`` field in the JSONL corpus record.
    predicted_spans : list of dict
        Each dict has keys: ``start`` (int), ``end`` (int),
        ``entity_type`` (str, i2b2 taxonomy label).
        Example: {"start": 12, "end": 24, "entity_type": "PATIENT"}
    runtime_ms : float
        Wall-clock inference time for this record in milliseconds.
    model_version : str
        Model checkpoint identifier for reproducibility.
    """

    record_id: str
    predicted_spans: List[Dict[str, Any]] = field(default_factory=list)
    runtime_ms: float = 0.0
    model_version: str = "modified_deidentify_emnlp2025"


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class ModifiedDeidentifyAdapter:
    """Benchmark adapter for the Modified Deidentify model (arXiv 2509.14464v1).

    Designed for air-gapped / HPC environments: all inference is local, no
    network calls are made after model loading.

    TODO -- implementation checklist (complete before enabling in CI):
    ----------------------------------------------------------------
    [ ] 1. MODEL LOADING
            Load HuggingFace-compatible checkpoint from self.model_path.
            Confirm license allows derivative benchmarking before loading.
            Suggested: AutoModelForTokenClassification / AutoTokenizer.

    [ ] 2. TOKENIZER
            Load matching tokenizer. Handle sentencepiece vs. BPE depending on
            base architecture (the paper fine-tunes on a transformer encoder).

    [ ] 3. SLIDING WINDOW FOR LONG DOCUMENTS
            Clinical notes routinely exceed 512 tokens.
            Implement stride-based chunking (stride <= 256 tokens recommended)
            with span re-alignment to original character offsets. Overlapping
            predictions in stride region should be merged by highest logit.

    [ ] 4. ENTITY TYPE MAPPING
            Map raw model output labels to our extended corpus taxonomy
            (authorities/AUTHORITY_MATRIX.md Table A).
            Start from the i2b2 2014 label set used in the paper:
              PATIENT, DOCTOR, USERNAME, PROFESSION, ROOM, DEPARTMENT,
              HOSPITAL, ORGANIZATION, STREET, CITY, STATE, ZIP, COUNTRY,
              LOCATION-OTHER, AGE, DATE, TIME, DURATION, SET, PHONE, FAX,
              EMAIL, PAGER, URL, IPADDR, SSN, MEDICALRECORD, HEALTHPLAN,
              ACCOUNT, LICENSE, VEHICLE, DEVICE, BIOID, IDNUM
            Our corpus also includes extended US identifiers beyond the base
            i2b2 set (e.g. VIN and other HIPAA Safe Harbor identifiers).

    [ ] 5. CLINICAL DANGER SCORE
            The paper defines "clinically dangerous false positive" using the
            AHS (Adversarial Hard Set) dataset methodology: a false positive is
            clinically dangerous if the redacted token is a medication name,
            dosage, allergy, or critical clinical qualifier (e.g., "not",
            "no", "negative"). Implement the AHS classifier or an equivalent
            lexicon-based tagger to reproduce the 22/500 claim.
            See: arXiv 2509.14464v1, Section 3.2 and Table 3.
    """

    def __init__(self, model_path: str, device: str = "cpu") -> None:
        """Initialise the adapter.

        Parameters
        ----------
        model_path : str
            Local filesystem path to the model checkpoint directory.
            Must contain config.json, tokenizer files, and model weights.
            No default is provided -- this is intentional: the caller must
            supply a verified, licensed checkpoint.
        device : str
            PyTorch device string, e.g. "cpu", "cuda:0", "cuda", "mps".
            Defaults to "cpu" for HPC nodes without GPU allocation.

        Raises
        ------
        FileNotFoundError
            If model_path does not exist at construction time.
        """
        self.model_path = Path(model_path)
        self.device = device
        self.model = None       # set in _load_model() -- not yet implemented
        self.tokenizer = None   # set in _load_model() -- not yet implemented
        self.model_version = "modified_deidentify_emnlp2025"

        # Do not raise on missing weights at construction -- allows adapter
        # instantiation for interface testing without weights present.
        if not self.model_path.exists():
            import warnings
            warnings.warn(
                f"model_path does not exist: {self.model_path}. "
                "predict() will raise NotImplementedError until weights are "
                "present and _load_model() is implemented.",
                stacklevel=2,
            )

    # ------------------------------------------------------------------
    # Internal helpers (stubs)
    # ------------------------------------------------------------------

    def _load_model(self) -> None:
        """Load model weights and tokenizer from self.model_path.

        Not implemented -- see class-level TODO item 1 and 2.
        """
        raise NotImplementedError(
            "Modified Deidentify adapter requires model weights. "
            "Download from [LICENSE PENDING] and set model_path. "
            "See class docstring TODO items 1-2."
        )

    def _sliding_window_predict(self, text: str) -> List[Dict[str, Any]]:
        """Run token classification with stride-based chunking.

        Not implemented -- see class-level TODO item 3.
        """
        raise NotImplementedError(
            "Sliding window inference not implemented. "
            "See class docstring TODO item 3."
        )

    def _map_entity_types(
        self, raw_spans: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Translate i2b2 2014 labels to our extended corpus taxonomy.

        Not implemented -- see class-level TODO item 4.
        """
        raise NotImplementedError(
            "Entity type mapping not implemented. "
            "See class docstring TODO item 4."
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def predict(self, text: str) -> ModifiedDeidentifyResult:
        """Run PHI de-identification on a single text string.

        Parameters
        ----------
        text : str
            Plain-text clinical note or document. UTF-8 assumed.

        Returns
        -------
        ModifiedDeidentifyResult
            Predicted PHI spans with start/end character offsets and
            entity type labels (corpus taxonomy).

        Raises
        ------
        NotImplementedError
            Always, until _load_model() and _sliding_window_predict() are
            implemented and valid model weights are available.
        """
        raise NotImplementedError(
            "Modified Deidentify adapter requires model weights. "
            "Download from [LICENSE PENDING] and set model_path. "
            "See class docstring TODO items 1-5 for full implementation plan."
        )

    def run_corpus(
        self, corpus_path: Path
    ) -> List[ModifiedDeidentifyResult]:
        """Run predict() over every record in a JSONL corpus file.

        Parameters
        ----------
        corpus_path : Path
            Path to a JSONL file where each line is a JSON object with at
            minimum the fields ``id`` (str) and ``text`` (str).

        Returns
        -------
        list of ModifiedDeidentifyResult
            One result per corpus record, in file order.

        Raises
        ------
        FileNotFoundError
            If corpus_path does not exist.
        json.JSONDecodeError
            If a line is not valid JSON.
        NotImplementedError
            Propagated from predict() until model weights are available.
        """
        corpus_path = Path(corpus_path)
        if not corpus_path.exists():
            raise FileNotFoundError(f"Corpus file not found: {corpus_path}")

        results: List[ModifiedDeidentifyResult] = []
        with corpus_path.open("r", encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                record = json.loads(raw_line)
                record_id: str = record.get("id", f"record_{line_no}")
                text: str = record["text"]

                t0 = time.perf_counter()
                result = self.predict(text)
                result.runtime_ms = (time.perf_counter() - t0) * 1000.0
                result.record_id = record_id
                results.append(result)

        return results

    def score(
        self,
        results: List[ModifiedDeidentifyResult],
        gold_path: Path,
    ) -> Dict[str, Any]:
        """Compute precision, recall, and F1 against gold-standard spans.

        Token-level exact-match scoring using character offset overlap >= 0.5
        (partial credit threshold consistent with i2b2 2014 evaluation).

        Parameters
        ----------
        results : list of ModifiedDeidentifyResult
            Output of run_corpus().
        gold_path : Path
            Path to a JSONL file where each record has ``id`` and
            ``gold_spans`` (list of dicts with start/end/entity_type).

        Returns
        -------
        dict with keys:
            precision       (float, macro-averaged across entity types)
            recall          (float, macro-averaged across entity types)
            f1              (float, macro-averaged across entity types)
            per_entity_type (dict: entity_type -> {precision, recall, f1})
            total_tp        (int)
            total_fp        (int)
            total_fn        (int)
            clinical_danger_fp_count (int, see TODO item 5 -- always 0 until
                             AHS scorer is implemented)

        Raises
        ------
        NotImplementedError
            Scoring logic is a stub; raises until implemented.
        """
        # TODO: implement span-level scoring
        # Steps:
        #   1. Load gold_path into a dict keyed by record_id
        #   2. For each result, align predicted_spans against gold_spans
        #      using character-offset overlap >= 0.5
        #   3. Accumulate TP / FP / FN per entity_type
        #   4. Compute per-entity precision/recall/F1
        #   5. Compute macro-average across entity types
        #   6. Run AHS clinical-danger classifier on FP spans (TODO item 5)
        raise NotImplementedError(
            "score() is not yet implemented. "
            "See inline TODO comments in this method body."
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Modified Deidentify benchmark adapter "
            "(arXiv 2509.14464v1, EMNLP 2025). "
            "Requires local model weights -- see LICENSE STATUS in module "
            "docstring before use."
        )
    )
    parser.add_argument(
        "--model-path",
        required=True,
        help="Local path to Modified Deidentify checkpoint directory.",
    )
    parser.add_argument(
        "--corpus",
        required=True,
        type=Path,
        help="Path to JSONL corpus file (fields: id, text).",
    )
    parser.add_argument(
        "--gold",
        required=False,
        type=Path,
        default=None,
        help=(
            "Path to JSONL gold file (fields: id, gold_spans). "
            "If omitted, scoring step is skipped."
        ),
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help='PyTorch device string, e.g. "cpu" or "cuda:0". Default: cpu.',
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write results as JSONL to this path. Default: stdout.",
    )
    args = parser.parse_args()

    adapter = ModifiedDeidentifyAdapter(
        model_path=args.model_path,
        device=args.device,
    )

    print(f"Running corpus: {args.corpus}")
    results = adapter.run_corpus(args.corpus)
    print(f"Processed {len(results)} records.")

    if args.gold is not None:
        scores = adapter.score(results, args.gold)
        print("Scores:")
        print(json.dumps(scores, indent=2))

    serialised = [
        {
            "record_id": r.record_id,
            "predicted_spans": r.predicted_spans,
            "runtime_ms": r.runtime_ms,
            "model_version": r.model_version,
        }
        for r in results
    ]

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8") as out_fh:
            for obj in serialised:
                out_fh.write(json.dumps(obj) + "\n")
        print(f"Results written to {args.output}")
    else:
        for obj in serialised:
            print(json.dumps(obj))
