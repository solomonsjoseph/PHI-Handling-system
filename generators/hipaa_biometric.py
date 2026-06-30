"""
HIPAA biometric identifier generator.

Covers 45 CFR 164.514(b)(2)(i)(P): finger and voice prints, and by extension
all biometric identifiers (retinal scans, iris templates, DNA reference IDs).

Cross-reference: GDPR Article 4(14) classifies biometric data as a special
category when used to uniquely identify a natural person. This creates a
conflict case: a biometric template ID in a US/EU cross-jurisdiction record
is PHI under HIPAA (P) AND special-category data under GDPR.

What this generator covers that hipaa_safe_harbor.py does not:
- Dedicated sub-types per biometric modality (fingerprint, voice, retinal,
  iris, DNA, face-template) with realistic clinical text for each
- Biometric enrollment records (new patient identity setup)
- Biometric verification failure records (mismatch audit trail)
- Biometric revocation records (template invalidated, re-enrollment required)
- Multi-modality enrollment (fingerprint + iris at same session)
- DNA specimen reference IDs (clinical genetics, not forensics)
- GDPR Art. 4(14) cross-reference annotation on every span

Authority: 45 CFR 164.514(b)(2)(i)(P)
Cross-authority: GDPR Article 4(14) (biometric data as special category)
Detection regime: contextual_ner_required -- biometric template IDs require
clinical context to distinguish from generic hex/alphanumeric strings.
"""
from __future__ import annotations

import string
from typing import List

from .common import (
    AUTH_GDPR_ARTICLE_4_14,
    AUTH_HIPAA_SAFE_HARBOR,
    DETECTION_REGIME_NER,
    LAYER_HIPAA,
    DeterministicGenerator,
    GoldSpan,
    Record,
)

AUTH_HIPAA_P = "45 CFR 164.514(b)(2)(i)(P)"

# Biometric equipment vendors (realistic but not real product names)
_FINGERPRINT_READERS = [
    "Verident BioAccess 3", "SecuTouch ID-9", "DigiFprint Model 7C",
    "MedBio FP-100", "IdentaScan Pro",
]
_IRIS_SCANNERS = [
    "IrisScan Medica 3", "OcuVerify 2000", "RetinalPlus HD",
    "BioIris Clinical 5", "EyeID Medical",
]
_VOICE_SYSTEMS = [
    "VoxID Healthcare", "SpeechAuth Clinical", "VoiceVault MedAccess",
    "BioVoice 4.1", "SpeakSecure HS",
]


def _fp_template(rng) -> str:
    """Fingerprint template reference ID."""
    prefix = rng.choice(["FP", "FPRINT", "TMPL"])
    uid = "".join(rng.choices(string.hexdigits.upper()[:16], k=20))
    return f"{prefix}-{uid}"


def _voice_template(rng) -> str:
    """Voice print template reference ID."""
    uid = "".join(rng.choices(string.digits, k=16))
    return f"VT-{uid}"


def _iris_template(rng) -> str:
    """Iris/retinal template reference ID."""
    modality = rng.choice(["IRIS", "RETINA", "OCULAR"])
    uid = "".join(rng.choices(string.hexdigits.upper()[:16], k=18))
    return f"{modality}-{uid}"


def _dna_specimen(rng) -> str:
    """DNA specimen reference ID (clinical genetics, not forensic STR)."""
    year = rng.randint(2020, 2025)
    seq = rng.randint(10000, 99999)
    return f"DNA-SPEC-{year}-{seq}"


def _face_template(rng) -> str:
    """Facial recognition enrollment template ID."""
    uid = "".join(rng.choices(string.hexdigits.upper()[:16], k=24))
    return f"FACE-EID-{uid}"


def _bio_enrollment_id(rng) -> str:
    """Generic biometric enrollment record ID."""
    digits = "".join(rng.choices(string.digits, k=12))
    return f"BIO-ENR-{digits}"


class HIPAABiometricGenerator(DeterministicGenerator):
    """Dedicated biometric identifier records for HIPAA category (P).

    Produces 10 sub-type modes, each with 4 records by default:
    fingerprint_enrollment, fingerprint_verify_fail, voice_print,
    iris_scan, retinal_scan, dna_specimen, face_template,
    multi_modality, biometric_revocation, biometric_audit_log.
    """

    _MODES = [
        "fingerprint_enrollment",
        "fingerprint_verify_fail",
        "voice_print",
        "iris_scan",
        "retinal_scan",
        "dna_specimen",
        "face_template",
        "multi_modality",
        "biometric_revocation",
        "biometric_audit_log",
    ]

    def generate_batch(self, count_per_mode: int = 4) -> List[Record]:
        records: List[Record] = []
        dispatch = {
            "fingerprint_enrollment": self._gen_fp_enrollment,
            "fingerprint_verify_fail": self._gen_fp_verify_fail,
            "voice_print": self._gen_voice_print,
            "iris_scan": self._gen_iris_scan,
            "retinal_scan": self._gen_retinal_scan,
            "dna_specimen": self._gen_dna_specimen,
            "face_template": self._gen_face_template,
            "multi_modality": self._gen_multi_modality,
            "biometric_revocation": self._gen_revocation,
            "biometric_audit_log": self._gen_audit_log,
        }
        for mode in self._MODES:
            fn = dispatch[mode]
            for i in range(count_per_mode):
                rng = self.fresh(f"bio_{mode}_{i}")
                records.append(fn(rng, mode, i))
        return records

    def _make(
        self,
        mode: str,
        index: int,
        text: str,
        spans_spec,
        context: str = "operations",
        metadata: dict = None,
    ) -> Record:
        return Record(
            record_id=f"bio_{mode}_{index:04d}",
            text=text,
            gold_spans=self.annotate(text, spans_spec),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context=context,
            format="text",
            authority_citations=[AUTH_HIPAA_P, AUTH_HIPAA_SAFE_HARBOR, AUTH_GDPR_ARTICLE_4_14],
            metadata=metadata or {"biometric_modality": mode, "hipaa_category": "P"},
        )

    def _gen_fp_enrollment(self, rng, mode, i):
        tmpl = _fp_template(rng)
        reader = rng.choice(_FINGERPRINT_READERS)
        finger = rng.choice(["right index", "left index", "right thumb", "left thumb"])
        quality = rng.randint(72, 99)
        text = (
            f"Patient biometric enrollment: fingerprint captured via {reader}. "
            f"Template stored as {tmpl} ({finger}, quality score {quality}/100). "
            f"Enrollment date recorded in identity management system."
        )
        spans = [
            (tmpl, "BIOMETRIC_FINGERPRINT_TEMPLATE", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"biometric_modality": "fingerprint", "hipaa_category": "P",
                                    "gdpr_cross_ref": "Article 4(14)"})

    def _gen_fp_verify_fail(self, rng, mode, i):
        tmpl = _fp_template(rng)
        attempt = rng.randint(1, 3)
        score = rng.randint(20, 49)
        text = (
            f"Biometric verification failed (attempt {attempt} of 3). "
            f"Stored fingerprint template: {tmpl}. Match score {score}/100 below "
            f"threshold 60. Staff override required for patient access."
        )
        spans = [
            (tmpl, "BIOMETRIC_FINGERPRINT_TEMPLATE", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"biometric_modality": "fingerprint", "hipaa_category": "P",
                                    "verification_outcome": "failed"})

    def _gen_voice_print(self, rng, mode, i):
        tmpl = _voice_template(rng)
        system = rng.choice(_VOICE_SYSTEMS)
        channel = rng.choice(["inbound phone triage", "telehealth portal", "automated refill line"])
        text = (
            f"Patient authenticated via voice biometric on {channel}. "
            f"Voice template reference: {tmpl} enrolled in {system}. "
            f"Authentication confidence: high."
        )
        spans = [
            (tmpl, "BIOMETRIC_VOICE_TEMPLATE", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"biometric_modality": "voice", "hipaa_category": "P",
                                    "gdpr_cross_ref": "Article 4(14)"})

    def _gen_iris_scan(self, rng, mode, i):
        tmpl = _iris_template(rng)
        scanner = rng.choice(_IRIS_SCANNERS)
        eye = rng.choice(["left", "right", "both"])
        text = (
            f"Iris scan completed at registration kiosk ({scanner}). "
            f"Template enrolled: {tmpl} ({eye} eye). "
            f"Identity confirmed against patient record."
        )
        spans = [
            (tmpl, "BIOMETRIC_IRIS_TEMPLATE", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"biometric_modality": "iris", "hipaa_category": "P"})

    def _gen_retinal_scan(self, rng, mode, i):
        tmpl = _iris_template(rng)
        text = (
            f"Retinal scan on file for high-security medication dispensing. "
            f"Retinal template ID: {tmpl}. Scan performed at pharmacy counter. "
            f"Controlled substance access granted per retinal match."
        )
        spans = [
            (tmpl, "BIOMETRIC_RETINAL_TEMPLATE", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"biometric_modality": "retinal", "hipaa_category": "P"})

    def _gen_dna_specimen(self, rng, mode, i):
        specimen_id = _dna_specimen(rng)
        gene = rng.choice(["BRCA1", "BRCA2", "MLH1", "CFTR", "HTT", "FMR1"])
        lab = rng.choice(["Clinical Genetics Lab", "Molecular Pathology", "Genomics Reference Lab"])
        text = (
            f"DNA specimen submitted to {lab} for {gene} variant analysis. "
            f"Specimen reference: {specimen_id}. "
            f"Patient consent obtained per HIPAA authorization. "
            f"Results to be linked to MRN on return from external laboratory."
        )
        spans = [
            (specimen_id, "BIOMETRIC_DNA_SPECIMEN", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
        ]
        # DNA is genetic data under GDPR Art. 4(13), distinct from biometric (Art. 4(14))
        return Record(
            record_id=f"bio_{mode}_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context="treatment",
            format="text",
            authority_citations=[AUTH_HIPAA_P, AUTH_HIPAA_SAFE_HARBOR,
                                  AUTH_GDPR_ARTICLE_4_14, "GDPR Article 4(13) (genetic data)"],
            metadata={"biometric_modality": "dna", "hipaa_category": "P",
                      "gdpr_cross_ref": "Article 4(13)"},
        )

    def _gen_face_template(self, rng, mode, i):
        tmpl = _face_template(rng)
        confidence = rng.randint(85, 99)
        text = (
            f"Facial recognition enrollment completed. "
            f"Template ID: {tmpl} stored in identity system. "
            f"Enrollment confidence {confidence}%. "
            f"Note: distinct from patient photograph (HIPAA cat Q); "
            f"this is a mathematical template, not an image."
        )
        spans = [
            (tmpl, "BIOMETRIC_FACE_TEMPLATE", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"biometric_modality": "face_template", "hipaa_category": "P",
                                    "distinct_from_category_Q": True})

    def _gen_multi_modality(self, rng, mode, i):
        fp_tmpl = _fp_template(rng)
        iris_tmpl = _iris_template(rng)
        enr_id = _bio_enrollment_id(rng)
        text = (
            f"Multi-modal biometric enrollment record {enr_id}. "
            f"Fingerprint template: {fp_tmpl}. "
            f"Iris template: {iris_tmpl}. "
            f"Both modalities required for surgical suite access."
        )
        spans = [
            (enr_id, "BIOMETRIC_ENROLLMENT_ID", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
            (fp_tmpl, "BIOMETRIC_FINGERPRINT_TEMPLATE", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
            (iris_tmpl, "BIOMETRIC_IRIS_TEMPLATE", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"biometric_modality": "multi", "hipaa_category": "P",
                                    "modalities": ["fingerprint", "iris"]})

    def _gen_revocation(self, rng, mode, i):
        tmpl = _fp_template(rng)
        reason = rng.choice([
            "finger injury requiring re-enrollment",
            "template quality degraded",
            "patient request per privacy rights",
            "unauthorized template duplication suspected",
        ])
        text = (
            f"Biometric template revocation initiated. "
            f"Template ID {tmpl} has been invalidated. "
            f"Reason: {reason}. "
            f"Re-enrollment required at next visit. "
            f"All downstream system references to this template ID are now invalid."
        )
        spans = [
            (tmpl, "BIOMETRIC_FINGERPRINT_TEMPLATE", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"biometric_modality": "fingerprint", "hipaa_category": "P",
                                    "action": "revocation"})

    def _gen_audit_log(self, rng, mode, i):
        tmpl = _iris_template(rng)
        match_score = rng.randint(88, 99)
        location = rng.choice(["OR-3", "ICU North", "Pharmacy Dispensing", "Radiology Control"])
        hour = rng.randint(0, 23)
        minute = rng.randint(0, 59)
        text = (
            f"Biometric access log: template {tmpl} matched at {location} "
            f"at {hour:02d}:{minute:02d}. Match score {match_score}. "
            f"Access granted. Event recorded per HIPAA minimum necessary standard."
        )
        spans = [
            (tmpl, "BIOMETRIC_IRIS_TEMPLATE", "P", "us", AUTH_HIPAA_P, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"biometric_modality": "iris", "hipaa_category": "P",
                                    "log_type": "access_audit"})
