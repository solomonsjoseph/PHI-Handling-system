"""
HIPAA device identifier generator.

Covers 45 CFR 164.514(b)(2)(i)(M): device identifiers and serial numbers.

What this generator covers that hipaa_safe_harbor.py does not:
- Three FDA-approved UDI issuing agencies: GS1, HIBCC, ICCBBA
- UDI-DI (Device Identifier) vs UDI-PI (Production Identifier) distinction
  per FDA UDI Final Rule (21 CFR 830.3)
- Device recall check context (UDI as linkage in safety notices)
- Implanted device records (pacemaker, cochlear implant, hip/knee replacement)
- ICU and infusion pump serial number records
- Blood/tissue product records (ICCBBA/ISBT 128 format)
- IVD (in vitro diagnostic) lot/reagent identifiers
- Software as a Medical Device (SaMD) version identifiers
- Endoscope reprocessing records (serial required for infection traceability)

UDI format references:
- GS1:   (01){14-digit-GTIN}                   -- DI portion
- HIBCC: +{4-char-LabelerID}/{Product}/{UoM}    -- simplified
- ICCBBA/ISBT 128: =R{facility-5}{donation-8}  -- blood/tissue

Authority: 45 CFR 164.514(b)(2)(i)(M)
Cross-authority: FDA UDI Final Rule 21 CFR 830 (2013)
Detection regime: contextual_ner_required -- UDI formats vary by agency;
GS1 "(01)" prefix provides a rule hook, but HIBCC and ICCBBA require context.
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

AUTH_HIPAA_M = "45 CFR 164.514(b)(2)(i)(M)"
AUTH_FDA_UDI = "FDA UDI Final Rule 21 CFR 830"


def _udi_gs1(rng) -> str:
    """GS1 UDI-DI: (01) + 14-digit GTIN.

    The GTIN-14 ends with a Luhn check digit. We generate the DI only
    (not the PI/serial portion) as that is the device identifier.
    """
    gtin_body = "".join(str(rng.randint(0, 9)) for _ in range(13))
    # Luhn for the 13-digit body
    digits = [int(d) for d in gtin_body]
    s = sum(
        d * (3 if i % 2 == 0 else 1)
        for i, d in enumerate(reversed(digits))
    )
    check = (10 - (s % 10)) % 10
    return f"(01){gtin_body}{check}"


def _udi_hibcc(rng) -> str:
    """HIBCC UDI-DI: +{LabelerID}/{ProductCode}/{PackageLevel}.

    LabelerID: 4 uppercase alphanumeric. ProductCode: 6-10 chars.
    PackageLevel: 1 digit. CheckChar: 1 char (we skip the calculation).
    """
    labeler = "".join(rng.choices(string.ascii_uppercase + string.digits, k=4))
    product = "".join(rng.choices(string.ascii_uppercase + string.digits, k=rng.randint(6, 10)))
    pkg = str(rng.randint(1, 5))
    return f"+{labeler}/{product}/{pkg}"


def _udi_iccbba(rng) -> str:
    """ICCBBA/ISBT 128 DI: =R + 5-char facility code + 8-char donation ID.

    Used for blood, tissue, and cellular products.
    """
    facility = "".join(rng.choices(string.ascii_uppercase + string.digits, k=5))
    donation = "".join(rng.choices(string.ascii_uppercase + string.digits, k=8))
    return f"=R{facility}{donation}"


def _device_serial(rng) -> str:
    """Generic device serial number (manufacturer format varies)."""
    prefix = rng.choice(["SN", "S/N", "SERIAL"])
    body = "".join(rng.choices(string.ascii_uppercase + string.digits, k=10))
    return f"{prefix}{body}"


def _lot_number(rng) -> str:
    """Manufacturing lot number for IVD and pharmaceuticals."""
    year = rng.randint(23, 26)
    lot = "".join(rng.choices(string.digits, k=6))
    return f"LOT{year}{lot}"


def _samd_version(rng) -> str:
    """Software as a Medical Device version identifier."""
    major = rng.randint(2, 9)
    minor = rng.randint(0, 15)
    patch = rng.randint(0, 99)
    build = "".join(rng.choices(string.hexdigits.lower()[:16], k=8))
    return f"v{major}.{minor}.{patch}+{build}"


class HIPAADeviceGenerator(DeterministicGenerator):
    """Dedicated device identifier records for HIPAA category (M).

    Produces 10 device context modes, each with 4 records by default.
    Covers GS1, HIBCC, ICCBBA UDI formats plus manufacturer serial numbers.
    """

    _MODES = [
        "cardiac_implant",
        "orthopedic_implant",
        "infusion_pump",
        "icu_monitor",
        "blood_product",
        "ivd_reagent",
        "samd",
        "endoscope",
        "cochlear_implant",
        "device_recall",
    ]

    def generate_batch(self, count_per_mode: int = 4) -> List[Record]:
        records: List[Record] = []
        dispatch = {
            "cardiac_implant": self._gen_cardiac,
            "orthopedic_implant": self._gen_orthopedic,
            "infusion_pump": self._gen_infusion_pump,
            "icu_monitor": self._gen_icu_monitor,
            "blood_product": self._gen_blood_product,
            "ivd_reagent": self._gen_ivd_reagent,
            "samd": self._gen_samd,
            "endoscope": self._gen_endoscope,
            "cochlear_implant": self._gen_cochlear,
            "device_recall": self._gen_recall,
        }
        for mode in self._MODES:
            fn = dispatch[mode]
            for i in range(count_per_mode):
                rng = self.fresh(f"dev_{mode}_{i}")
                records.append(fn(rng, mode, i))
        return records

    def _make(self, mode, index, text, spans_spec, context="treatment", metadata=None) -> Record:
        return Record(
            record_id=f"device_{mode}_{index:04d}",
            text=text,
            gold_spans=self.annotate(text, spans_spec),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context=context,
            format="text",
            authority_citations=[AUTH_HIPAA_M, AUTH_HIPAA_SAFE_HARBOR, AUTH_FDA_UDI],
            metadata=metadata or {"device_context": mode, "hipaa_category": "M"},
        )

    def _gen_cardiac(self, rng, mode, i):
        udi = _udi_gs1(rng)
        serial = _device_serial(rng)
        device_type = rng.choice(["pacemaker", "implantable cardioverter-defibrillator (ICD)",
                                   "cardiac resynchronization therapy device"])
        text = (
            f"Implanted {device_type}. "
            f"UDI: {udi}. "
            f"Device serial number: {serial}. "
            f"Implant date recorded. FDA MAUDE adverse event reporting required "
            f"if device malfunction observed."
        )
        spans = [
            (udi, "DEVICE_UDI_GS1", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_RULE),
            (serial, "DEVICE_SERIAL", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"device_context": mode, "hipaa_category": "M",
                                    "udi_agency": "GS1", "implanted": True})

    def _gen_orthopedic(self, rng, mode, i):
        udi = _udi_hibcc(rng)
        serial = _device_serial(rng)
        joint = rng.choice(["total hip replacement", "total knee replacement",
                             "partial knee replacement", "shoulder arthroplasty"])
        side = rng.choice(["left", "right"])
        text = (
            f"Orthopedic implant ({side} {joint}). "
            f"UDI (HIBCC): {udi}. "
            f"Component serial: {serial}. "
            f"Implant registry submission required per hospital policy."
        )
        spans = [
            (udi, "DEVICE_UDI_HIBCC", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
            (serial, "DEVICE_SERIAL", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"device_context": mode, "hipaa_category": "M",
                                    "udi_agency": "HIBCC", "implanted": True})

    def _gen_infusion_pump(self, rng, mode, i):
        udi = _udi_gs1(rng)
        serial = _device_serial(rng)
        drug = rng.choice(["heparin", "insulin", "morphine", "vancomycin", "dopamine"])
        rate = f"{rng.randint(1, 50)} mL/hr"
        text = (
            f"Infusion pump programmed for {drug} at {rate}. "
            f"Pump UDI: {udi}. Serial: {serial}. "
            f"Pump checked out from central supply and assigned to patient room."
        )
        spans = [
            (udi, "DEVICE_UDI_GS1", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_RULE),
            (serial, "DEVICE_SERIAL", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"device_context": mode, "hipaa_category": "M",
                                    "udi_agency": "GS1"})

    def _gen_icu_monitor(self, rng, mode, i):
        serial = _device_serial(rng)
        bed = f"ICU-{rng.randint(1, 24):02d}"
        params = rng.choice(["HR, SpO2, ETCO2", "HR, BP, SpO2", "HR, SpO2, RR, temp"])
        text = (
            f"Bedside monitor at {bed}. "
            f"Monitor serial: {serial}. "
            f"Parameters monitored: {params}. "
            f"Device ID linked to patient EMR for continuous waveform archival."
        )
        spans = [
            (serial, "DEVICE_SERIAL", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"device_context": mode, "hipaa_category": "M"})

    def _gen_blood_product(self, rng, mode, i):
        udi = _udi_iccbba(rng)
        product = rng.choice(["packed red blood cells", "fresh frozen plasma",
                               "platelets", "cryoprecipitate", "whole blood"])
        blood_type = rng.choice(["A+", "A-", "B+", "B-", "O+", "O-", "AB+", "AB-"])
        text = (
            f"Blood product transfused: {product}, type {blood_type}. "
            f"ISBT 128 DI: {udi}. "
            f"Transfusion reaction: none observed. "
            f"Blood bank traceability record retained per AABB standards."
        )
        spans = [
            (udi, "DEVICE_UDI_ICCBBA", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"device_context": mode, "hipaa_category": "M",
                                    "udi_agency": "ICCBBA", "product_type": "blood"})

    def _gen_ivd_reagent(self, rng, mode, i):
        udi = _udi_gs1(rng)
        lot = _lot_number(rng)
        analyte = rng.choice(["HbA1c", "troponin I", "BNP", "creatinine", "glucose", "INR"])
        text = (
            f"IVD reagent used for {analyte} assay. "
            f"Reagent kit UDI: {udi}. Lot number: {lot}. "
            f"Quality control passed. Result linked to patient encounter."
        )
        spans = [
            (udi, "DEVICE_UDI_GS1", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_RULE),
            (lot, "DEVICE_LOT_NUMBER", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"device_context": mode, "hipaa_category": "M",
                                    "udi_agency": "GS1", "product_type": "IVD"})

    def _gen_samd(self, rng, mode, i):
        version = _samd_version(rng)
        product = rng.choice(["Clinical Decision Support Module", "AI Radiology Assist",
                               "Sepsis Early Warning System", "Continuous Glucose Monitor App"])
        text = (
            f"Software as a Medical Device (SaMD): {product}. "
            f"Deployed version: {version}. "
            f"FDA 510(k) clearance on file. "
            f"Version identifier must be retained in patient encounter records "
            f"where SaMD output influenced clinical decision."
        )
        spans = [
            (version, "DEVICE_SAMD_VERSION", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"device_context": mode, "hipaa_category": "M",
                                    "samd": True})

    def _gen_endoscope(self, rng, mode, i):
        serial = _device_serial(rng)
        scope_type = rng.choice(["colonoscope", "gastroscope", "bronchoscope", "duodenoscope"])
        channel = rng.choice(["GI lab room 3", "bronchoscopy suite", "endoscopy unit B"])
        text = (
            f"{scope_type.capitalize()} reprocessing record. "
            f"Scope serial: {serial}. Used in {channel}. "
            f"Reprocessing cycle completed and logged. "
            f"Serial number retained for infection traceability per CDC guidelines."
        )
        spans = [
            (serial, "DEVICE_SERIAL", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"device_context": mode, "hipaa_category": "M",
                                    "reprocessing_record": True})

    def _gen_cochlear(self, rng, mode, i):
        udi = _udi_hibcc(rng)
        serial = _device_serial(rng)
        side = rng.choice(["left", "right", "bilateral"])
        text = (
            f"Cochlear implant surgery ({side}). "
            f"Implant UDI (HIBCC): {udi}. "
            f"Electrode array serial: {serial}. "
            f"Audiological programming records linked to implant serial."
        )
        spans = [
            (udi, "DEVICE_UDI_HIBCC", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
            (serial, "DEVICE_SERIAL", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"device_context": mode, "hipaa_category": "M",
                                    "udi_agency": "HIBCC", "implanted": True})

    def _gen_recall(self, rng, mode, i):
        udi = _udi_gs1(rng)
        recall_class = rng.choice(["Class I", "Class II", "Class III"])
        serial = _device_serial(rng)
        text = (
            f"FDA device recall notice ({recall_class}). "
            f"Affected UDI: {udi}. "
            f"Patient has implanted device serial {serial}. "
            f"Patient notified. Follow-up scheduled. "
            f"MAUDE report filed."
        )
        spans = [
            (udi, "DEVICE_UDI_GS1", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_RULE),
            (serial, "DEVICE_SERIAL", "M", "us", AUTH_HIPAA_M, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"device_context": mode, "hipaa_category": "M",
                                    "recall_class": recall_class, "udi_agency": "GS1"})
