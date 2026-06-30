"""
HIPAA fax number generator.

Covers 45 CFR 164.514(b)(2)(i)(E): fax numbers.

Fax and telephone share the same digit format (NXX-NXX-XXXX). The category
distinction is purely contextual: "fax:" / "send to fax" / "fax number" signals
category E; "phone:" / "call at" signals category D. This is the core challenge
for automated detection: rule-based phone matchers cannot distinguish (E) from
(D) without context. This generator provides records that test that distinction.

What this generator covers that hipaa_safe_harbor.py does not:
- Referral cover sheet (sending fax + receiving fax + patient identifiers)
- Pharmacy prescription fax (Rx fax number, patient demographics)
- Insurance prior authorization fax (payer fax, auth number)
- Lab result fax to ordering provider (two fax numbers in one record)
- Hospital discharge summary fax header (fax timestamp + recipient fax)
- Radiology report fax routing
- Electronic fax (eFax) system reference numbers
- Fax vs. phone disambiguation records (same number appears as both)
- International fax (E.164 format: +1-NXX-NXX-XXXX)
- Fax broadcast list (multiple recipient fax numbers in one record)

Authority: 45 CFR 164.514(b)(2)(i)(E)
Detection regime: rule_applicable for the phone-format digit pattern, but
contextual_ner_required to distinguish fax (E) from phone (D).
"""
from __future__ import annotations

import string
from typing import List

from .common import (
    AUTH_HIPAA_SAFE_HARBOR,
    DETECTION_REGIME_NER,
    DETECTION_REGIME_RULE,
    LAYER_HIPAA,
    DeterministicGenerator,
    Record,
)

AUTH_HIPAA_E = "45 CFR 164.514(b)(2)(i)(E)"
AUTH_HIPAA_D = "45 CFR 164.514(b)(2)(i)(D)"


def _fax_number(rng) -> str:
    """US fax number, NXX-NXX-XXXX format."""
    area = rng.randint(200, 989)
    exch = rng.randint(200, 999)
    line = rng.randint(0, 9999)
    return f"({area:03d}) {exch:03d}-{line:04d}"


def _phone_number(rng) -> str:
    """US phone number, same format as fax."""
    return _fax_number(rng)


def _intl_fax(rng) -> str:
    """International fax in E.164 notation."""
    area = rng.randint(200, 989)
    exch = rng.randint(200, 999)
    line = rng.randint(0, 9999)
    return f"+1-{area:03d}-{exch:03d}-{line:04d}"


def _prior_auth_number(rng) -> str:
    digits = "".join(str(rng.randint(0, 9)) for _ in range(10))
    return f"PA{digits}"


def _rx_number(rng) -> str:
    return "RX" + "".join(str(rng.randint(0, 9)) for _ in range(9))


class HIPAAFaxGenerator(DeterministicGenerator):
    """Dedicated fax number records for HIPAA category (E).

    Produces 10 context modes, each with 4 records by default.
    Key design: each record labels FAX spans distinctly from PHONE spans
    so benchmark tools can be tested on disambiguation.
    """

    _MODES = [
        "referral_cover_sheet",
        "pharmacy_rx",
        "prior_auth",
        "lab_result_fax",
        "discharge_summary",
        "radiology_report",
        "efax_header",
        "fax_phone_disambiguation",
        "international_fax",
        "fax_broadcast",
    ]

    def generate_batch(self, count_per_mode: int = 4) -> List[Record]:
        records: List[Record] = []
        dispatch = {
            "referral_cover_sheet": self._gen_referral,
            "pharmacy_rx": self._gen_pharmacy,
            "prior_auth": self._gen_prior_auth,
            "lab_result_fax": self._gen_lab,
            "discharge_summary": self._gen_discharge,
            "radiology_report": self._gen_radiology,
            "efax_header": self._gen_efax,
            "fax_phone_disambiguation": self._gen_disambiguation,
            "international_fax": self._gen_intl,
            "fax_broadcast": self._gen_broadcast,
        }
        for mode in self._MODES:
            fn = dispatch[mode]
            for i in range(count_per_mode):
                rng = self.fresh(f"fax_{mode}_{i}")
                records.append(fn(rng, mode, i))
        return records

    def _make(self, mode, index, text, spans_spec, context="operations", metadata=None) -> Record:
        return Record(
            record_id=f"fax_{mode}_{index:04d}",
            text=text,
            gold_spans=self.annotate(text, spans_spec),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context=context,
            format="text",
            authority_citations=[AUTH_HIPAA_E, AUTH_HIPAA_SAFE_HARBOR],
            metadata=metadata or {"fax_context": mode, "hipaa_category": "E"},
        )

    def _gen_referral(self, rng, mode, i):
        sending_fax = _fax_number(rng)
        receiving_fax = _fax_number(rng)
        text = (
            f"REFERRAL COVER SHEET -- CONFIDENTIAL\n"
            f"Sending fax: {sending_fax}\n"
            f"Receiving fax: {receiving_fax}\n"
            f"Pages: {rng.randint(2, 8)} including this cover sheet.\n"
            f"Reason: specialist consultation request. "
            f"If received in error, notify sender immediately and destroy."
        )
        spans = [
            (sending_fax, "FAX_SENDER", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
            (receiving_fax, "FAX_RECEIVER", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"fax_context": mode, "hipaa_category": "E",
                                    "cover_sheet": True})

    def _gen_pharmacy(self, rng, mode, i):
        rx = _rx_number(rng)
        fax = _fax_number(rng)
        drug = rng.choice(["atorvastatin 20 mg", "metformin 500 mg", "lisinopril 10 mg",
                            "levothyroxine 50 mcg", "omeprazole 20 mg"])
        days = rng.choice([30, 60, 90])
        text = (
            f"Prescription faxed to pharmacy. "
            f"Pharmacy fax: {fax}. "
            f"Rx number: {rx}. "
            f"Medication: {drug}, {days}-day supply. "
            f"Prescriber signature on file. "
            f"HIPAA Notice: fax transmission may contain PHI."
        )
        spans = [
            (fax, "FAX_PHARMACY", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"fax_context": mode, "hipaa_category": "E"})

    def _gen_prior_auth(self, rng, mode, i):
        payer_fax = _fax_number(rng)
        provider_fax = _fax_number(rng)
        auth = _prior_auth_number(rng)
        procedure = rng.choice(["MRI lumbar spine", "bariatric surgery", "cardiac catheterization",
                                 "spinal cord stimulator", "robotic prostatectomy"])
        text = (
            f"Prior authorization request for {procedure}. "
            f"Payer fax: {payer_fax}. "
            f"Provider fax (for response): {provider_fax}. "
            f"Reference: {auth}. "
            f"Please fax determination within 72 hours."
        )
        spans = [
            (payer_fax, "FAX_PAYER", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
            (provider_fax, "FAX_PROVIDER", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="payment",
                          metadata={"fax_context": mode, "hipaa_category": "E",
                                    "prior_auth": True})

    def _gen_lab(self, rng, mode, i):
        lab_fax = _fax_number(rng)
        ordering_fax = _fax_number(rng)
        test = rng.choice(["comprehensive metabolic panel", "CBC with differential",
                            "thyroid panel", "urine culture", "blood culture x2"])
        text = (
            f"Laboratory result transmission. "
            f"Transmitting lab fax: {lab_fax}. "
            f"Ordering provider fax: {ordering_fax}. "
            f"Test: {test}. "
            f"Critical values called prior to fax transmission per lab policy."
        )
        spans = [
            (lab_fax, "FAX_LAB", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
            (ordering_fax, "FAX_PROVIDER", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"fax_context": mode, "hipaa_category": "E"})

    def _gen_discharge(self, rng, mode, i):
        hospital_fax = _fax_number(rng)
        pcp_fax = _fax_number(rng)
        los = rng.randint(1, 14)
        text = (
            f"Hospital discharge summary faxed to primary care provider. "
            f"Hospital fax header: {hospital_fax}. "
            f"PCP fax: {pcp_fax}. "
            f"Length of stay: {los} days. "
            f"Follow-up in 7-10 days requested."
        )
        spans = [
            (hospital_fax, "FAX_HOSPITAL", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
            (pcp_fax, "FAX_PCP", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"fax_context": mode, "hipaa_category": "E"})

    def _gen_radiology(self, rng, mode, i):
        rad_fax = _fax_number(rng)
        study = rng.choice(["CT chest with contrast", "MRI brain without", "X-ray chest PA/lateral",
                             "nuclear stress test", "PET scan"])
        text = (
            f"Radiology report routing. "
            f"Imaging study: {study}. "
            f"Report faxed to ordering provider. "
            f"Radiology group fax: {rad_fax}. "
            f"Addendum to follow if preliminary read changes."
        )
        spans = [
            (rad_fax, "FAX_RADIOLOGY", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"fax_context": mode, "hipaa_category": "E"})

    def _gen_efax(self, rng, mode, i):
        efax_num = _fax_number(rng)
        efax_id = "EFX" + "".join(str(rng.randint(0, 9)) for _ in range(12))
        text = (
            f"eFax system transmission log. "
            f"Virtual fax number: {efax_num}. "
            f"eFax reference ID: {efax_id}. "
            f"Delivery confirmed to recipient inbox. "
            f"Encrypted at rest per HIPAA Security Rule."
        )
        spans = [
            (efax_num, "FAX_EFAX_NUMBER", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"fax_context": mode, "hipaa_category": "E",
                                    "efax": True})

    def _gen_disambiguation(self, rng, mode, i):
        """Same digit format, different label: tests FAX (E) vs PHONE (D) detection."""
        phone = _phone_number(rng)
        fax = _fax_number(rng)
        text = (
            f"Contact information on file: "
            f"phone {phone}, "
            f"fax {fax}. "
            f"Preferred contact method: phone. "
            f"Documentation fax only."
        )
        spans = [
            (phone, "PHONE_HOME", "D", "us", AUTH_HIPAA_D, DETECTION_REGIME_NER),
            (fax, "FAX", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
        ]
        return Record(
            record_id=f"fax_{mode}_{i:04d}",
            text=text,
            gold_spans=self.annotate(text, spans),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context="operations",
            format="text",
            authority_citations=[AUTH_HIPAA_E, AUTH_HIPAA_D, AUTH_HIPAA_SAFE_HARBOR],
            metadata={"fax_context": mode, "hipaa_category": "E",
                      "disambiguation_test": True,
                      "note": "Same digit format; FAX vs PHONE distinguished by context label only"},
        )

    def _gen_intl(self, rng, mode, i):
        intl = _intl_fax(rng)
        country = rng.choice(["Canada", "Mexico", "United Kingdom"])
        text = (
            f"International referral correspondence. "
            f"Receiving fax (E.164 format): {intl} ({country}). "
            f"Cross-border PHI transmission requires HIPAA Business Associate Agreement "
            f"or equivalent data protection arrangement."
        )
        spans = [
            (intl, "FAX_INTERNATIONAL", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"fax_context": mode, "hipaa_category": "E",
                                    "international": True, "e164_format": True})

    def _gen_broadcast(self, rng, mode, i):
        fax1 = _fax_number(rng)
        fax2 = _fax_number(rng)
        fax3 = _fax_number(rng)
        notice_type = rng.choice(["network policy update", "formulary change notification",
                                   "prior authorization requirement change"])
        text = (
            f"Broadcast fax: {notice_type}. "
            f"Recipient 1 fax: {fax1}. "
            f"Recipient 2 fax: {fax2}. "
            f"Recipient 3 fax: {fax3}. "
            f"Each transmission is an independent HIPAA transmission event."
        )
        spans = [
            (fax1, "FAX_BROADCAST_1", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
            (fax2, "FAX_BROADCAST_2", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
            (fax3, "FAX_BROADCAST_3", "E", "us", AUTH_HIPAA_E, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"fax_context": mode, "hipaa_category": "E",
                                    "multi_recipient": True})
