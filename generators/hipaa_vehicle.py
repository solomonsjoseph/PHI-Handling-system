"""
HIPAA vehicle identifier generator.

Covers 45 CFR 164.514(b)(2)(i)(L): vehicle identifiers and serial numbers,
license plate numbers, and other vehicle identifiers.

What this generator covers that hipaa_safe_harbor.py does not:
- Structured VIN components per ISO 3779 / NHTSA rule:
    WMI (chars 1-3): World Manufacturer Identifier
    VDS (chars 4-9): Vehicle Descriptor Section (model, body style, engine type)
    VIS (chars 10-17): Vehicle Identifier Section (model year encoding in char 10)
- Six distinct US state plate formats (California, Texas, Florida,
  New York, Illinois, Michigan)
- Vanity / specialty plates (alphanumeric custom text)
- Ambulance and emergency transport records (most common clinical context)
- Motor vehicle accident (MVA) patient records
- Rideshare transport records (Uber/Lyft plate logged at arrival)
- Home health visit vehicle notation
- Fleet vehicle records (institutional vehicles linked to patient visits)
- VIN-only records (recall check, airbag deployment, crash data)

VIN character set: no I, O, Q (NHTSA 49 CFR Part 565).
Model year position (index 9): 1980=A ... 2000=Y, 2001=1 ... 2009=9,
2010=A (repeating every 30 years).

Authority: 45 CFR 164.514(b)(2)(i)(L)
Cross-authority: ISO 3779 (VIN structure); NHTSA 49 CFR Part 565
Detection regime: rule_applicable for 17-char VIN (structured pattern);
contextual_ner_required for license plates (state-specific, no universal format).
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

AUTH_HIPAA_L = "45 CFR 164.514(b)(2)(i)(L)"
AUTH_ISO_3779 = "ISO 3779 (VIN structure)"

# VIN allowed chars (no I, O, Q per NHTSA)
_VIN_CHARS = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"

# Model year encoding at VIN position index 9 (0-based)
# Cycle repeats every 30 years. 2010 = A, 2011 = B, ...
_MODEL_YEAR_CHARS = "ABCDEFGHJKLMNPRSTUVWXY123456789"  # 30 chars, 1980-2009 then 2010+


def _vin(rng) -> str:
    """17-character VIN per ISO 3779 / NHTSA 49 CFR Part 565.

    Structure:
      Positions 0-2: WMI (manufacturer)
      Positions 3-8: VDS (descriptor)
      Position 8: check digit (0-9 or X) -- we generate a plausible value
      Position 9: model year code
      Position 10: plant code
      Positions 11-16: sequential production number
    """
    # WMI: real-format manufacturer codes (not actual real manufacturer WMIs)
    wmi = rng.choice(["1HD", "1G1", "2T1", "3VW", "4T1", "5YJ", "JN1", "WBA", "KNA"])
    # VDS: 5 chars + check digit position (8)
    vds_body = "".join(rng.choices(_VIN_CHARS, k=5))
    check = rng.choice("0123456789X")  # simplified; real check requires weighted sum mod 11
    # VIS
    year_code = rng.choice(_MODEL_YEAR_CHARS)
    plant = rng.choice(_VIN_CHARS)
    seq = "".join(rng.choices(string.digits, k=6))
    return wmi + vds_body + check + year_code + plant + seq


def _plate_california(rng) -> str:
    """California: 1ABC234 (digit + 3 letters + 3 digits)."""
    digit = str(rng.randint(1, 9))
    letters = "".join(rng.choices(string.ascii_uppercase, k=3))
    digits = "".join(str(rng.randint(0, 9)) for _ in range(3))
    return f"{digit}{letters}{digits}"


def _plate_texas(rng) -> str:
    """Texas: ABC-1234 (3 letters + hyphen + 4 digits)."""
    letters = "".join(rng.choices(string.ascii_uppercase, k=3))
    digits = "".join(str(rng.randint(0, 9)) for _ in range(4))
    return f"{letters}-{digits}"


def _plate_florida(rng) -> str:
    """Florida: ABC-D12 (3 letters + hyphen + letter + 2 digits) -- county sticker variant."""
    letters = "".join(rng.choices(string.ascii_uppercase, k=3))
    code = rng.choice(string.ascii_uppercase)
    digits = "".join(str(rng.randint(0, 9)) for _ in range(2))
    return f"{letters}-{code}{digits}"


def _plate_new_york(rng) -> str:
    """New York: ABC-1234."""
    letters = "".join(rng.choices(string.ascii_uppercase, k=3))
    digits = "".join(str(rng.randint(0, 9)) for _ in range(4))
    return f"{letters}-{digits}"


def _plate_illinois(rng) -> str:
    """Illinois: AB 12345 (2 letters + space + 5 digits)."""
    letters = "".join(rng.choices(string.ascii_uppercase, k=2))
    digits = "".join(str(rng.randint(0, 9)) for _ in range(5))
    return f"{letters} {digits}"


def _plate_michigan(rng) -> str:
    """Michigan: ABC 1234 (3 letters + space + 4 digits)."""
    letters = "".join(rng.choices(string.ascii_uppercase, k=3))
    digits = "".join(str(rng.randint(0, 9)) for _ in range(4))
    return f"{letters} {digits}"


def _vanity_plate(rng) -> str:
    """Vanity plate: 2-7 chars, letters/digits/spaces, no real meaning."""
    length = rng.randint(4, 7)
    chars = "".join(rng.choices(string.ascii_uppercase + string.digits, k=length))
    return chars


def _state_plate(rng):
    """Pick a random state plate format."""
    fns = [_plate_california, _plate_texas, _plate_florida,
           _plate_new_york, _plate_illinois, _plate_michigan]
    states = ["CA", "TX", "FL", "NY", "IL", "MI"]
    idx = rng.randint(0, len(fns) - 1)
    return fns[idx](rng), states[idx]


class HIPAAVehicleGenerator(DeterministicGenerator):
    """Dedicated vehicle identifier records for HIPAA category (L).

    Produces 10 context modes, each with 4 records by default.
    Covers ISO 3779 VIN structure and 6 US state plate formats.
    """

    _MODES = [
        "ambulance_transport",
        "mva_patient",
        "rideshare_arrival",
        "home_health_visit",
        "fleet_vehicle",
        "vin_recall",
        "vin_only",
        "plate_only",
        "vanity_plate",
        "commercial_vehicle",
    ]

    def generate_batch(self, count_per_mode: int = 4) -> List[Record]:
        records: List[Record] = []
        dispatch = {
            "ambulance_transport": self._gen_ambulance,
            "mva_patient": self._gen_mva,
            "rideshare_arrival": self._gen_rideshare,
            "home_health_visit": self._gen_home_health,
            "fleet_vehicle": self._gen_fleet,
            "vin_recall": self._gen_vin_recall,
            "vin_only": self._gen_vin_only,
            "plate_only": self._gen_plate_only,
            "vanity_plate": self._gen_vanity,
            "commercial_vehicle": self._gen_commercial,
        }
        for mode in self._MODES:
            fn = dispatch[mode]
            for i in range(count_per_mode):
                rng = self.fresh(f"veh_{mode}_{i}")
                records.append(fn(rng, mode, i))
        return records

    def _make(self, mode, index, text, spans_spec, context="treatment", metadata=None) -> Record:
        return Record(
            record_id=f"vehicle_{mode}_{index:04d}",
            text=text,
            gold_spans=self.annotate(text, spans_spec),
            layer=LAYER_HIPAA,
            jurisdiction="us",
            detection_regime=DETECTION_REGIME_NER,
            de_id_tier="identifiable",
            context=context,
            format="text",
            authority_citations=[AUTH_HIPAA_L, AUTH_HIPAA_SAFE_HARBOR, AUTH_ISO_3779],
            metadata=metadata or {"vehicle_context": mode, "hipaa_category": "L"},
        )

    def _gen_ambulance(self, rng, mode, i):
        vin = _vin(rng)
        plate, state = _state_plate(rng)
        unit = f"AMB-{rng.randint(1, 99):02d}"
        text = (
            f"Patient transported by ambulance unit {unit}. "
            f"Ambulance VIN: {vin}. "
            f"Unit license plate: {plate} ({state}). "
            f"Transport documentation attached to encounter."
        )
        spans = [
            (vin, "VIN", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_RULE),
            (plate, "LICENSE_PLATE", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"vehicle_context": mode, "hipaa_category": "L",
                                    "vehicle_type": "ambulance", "plate_state": state})

    def _gen_mva(self, rng, mode, i):
        vin = _vin(rng)
        plate, state = _state_plate(rng)
        impact = rng.choice(["frontal", "rear", "side", "rollover"])
        airbag = rng.choice(["deployed", "not deployed"])
        text = (
            f"Motor vehicle accident ({impact} impact). "
            f"Patient's vehicle: VIN {vin}, plate {plate} ({state}). "
            f"Airbag status: {airbag}. "
            f"Crash data retrieved for biomechanical injury assessment."
        )
        spans = [
            (vin, "VIN", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_RULE),
            (plate, "LICENSE_PLATE", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"vehicle_context": mode, "hipaa_category": "L",
                                    "mva": True, "plate_state": state})

    def _gen_rideshare(self, rng, mode, i):
        plate, state = _state_plate(rng)
        service = rng.choice(["rideshare", "medical transport", "paratransit"])
        color = rng.choice(["silver", "black", "white", "gray", "blue"])
        make = rng.choice(["sedan", "SUV", "minivan"])
        text = (
            f"Patient arrived via {service}. "
            f"Vehicle: {color} {make}, plate {plate} ({state}). "
            f"Driver confirmed destination as main hospital entrance. "
            f"Plate logged per transport policy."
        )
        spans = [
            (plate, "LICENSE_PLATE", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"vehicle_context": mode, "hipaa_category": "L",
                                    "plate_state": state})

    def _gen_home_health(self, rng, mode, i):
        vin = _vin(rng)
        plate, state = _state_plate(rng)
        service = rng.choice(["home health aide", "visiting nurse", "physical therapist"])
        text = (
            f"Home health visit documentation. "
            f"{service.capitalize()} vehicle: VIN {vin}, "
            f"plate {plate} ({state}) parked outside residence. "
            f"Visit duration and mileage logged for reimbursement."
        )
        spans = [
            (vin, "VIN", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_RULE),
            (plate, "LICENSE_PLATE", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"vehicle_context": mode, "hipaa_category": "L",
                                    "home_health": True, "plate_state": state})

    def _gen_fleet(self, rng, mode, i):
        vin = _vin(rng)
        fleet_id = "FLT-" + "".join(str(rng.randint(0, 9)) for _ in range(5))
        dept = rng.choice(["Patient Transport Services", "Environmental Services",
                            "Mobile Phlebotomy", "Discharge Transport"])
        text = (
            f"Institutional fleet vehicle {fleet_id}. "
            f"VIN: {vin}. "
            f"Assigned to: {dept}. "
            f"Vehicle linked to patient transport record."
        )
        spans = [
            (vin, "VIN", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_RULE),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"vehicle_context": mode, "hipaa_category": "L",
                                    "fleet_id": fleet_id})

    def _gen_vin_recall(self, rng, mode, i):
        vin = _vin(rng)
        nhtsa_id = f"NHTSA-{rng.randint(10, 99)}-{rng.randint(100, 999)}"
        component = rng.choice(["Takata airbag inflator", "fuel pump",
                                 "rear camera software", "side curtain airbag"])
        text = (
            f"Safety recall check performed. "
            f"Patient vehicle VIN: {vin}. "
            f"NHTSA recall reference: {nhtsa_id}. "
            f"Affected component: {component}. "
            f"Patient notified to schedule dealer repair."
        )
        spans = [
            (vin, "VIN", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_RULE),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"vehicle_context": mode, "hipaa_category": "L",
                                    "recall": True, "nhtsa_id": nhtsa_id})

    def _gen_vin_only(self, rng, mode, i):
        vin = _vin(rng)
        context_text = rng.choice([
            "Patient's vehicle towed from accident scene",
            "Vehicle identification for workman's compensation claim",
            "Crash reconstruction data subpoenaed",
            "Vehicle EDR (event data recorder) data retrieved for trauma assessment",
        ])
        text = f"{context_text}. Vehicle VIN: {vin}."
        spans = [
            (vin, "VIN", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_RULE),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"vehicle_context": mode, "hipaa_category": "L",
                                    "vin_only": True})

    def _gen_plate_only(self, rng, mode, i):
        plate, state = _state_plate(rng)
        context_text = rng.choice([
            "Vehicle in hospital parking reported blocking emergency access",
            "Patient vehicle plate recorded for security incident report",
            "Parking validation plate number captured",
            "Visitor plate logged per campus security policy",
        ])
        text = f"{context_text}. Plate: {plate} ({state})."
        spans = [
            (plate, "LICENSE_PLATE", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="operations",
                          metadata={"vehicle_context": mode, "hipaa_category": "L",
                                    "plate_only": True, "plate_state": state})

    def _gen_vanity(self, rng, mode, i):
        vanity = _vanity_plate(rng)
        state = rng.choice(["CA", "TX", "FL", "NY", "VA", "MD"])
        text = (
            f"Patient's vanity license plate: {vanity} ({state}). "
            f"Distinctive identifier noted in emergency contact record. "
            f"Vanity plates are vehicle identifiers within HIPAA Safe Harbor (L) scope."
        )
        spans = [
            (vanity, "LICENSE_PLATE_VANITY", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_NER),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"vehicle_context": mode, "hipaa_category": "L",
                                    "vanity_plate": True, "plate_state": state})

    def _gen_commercial(self, rng, mode, i):
        vin = _vin(rng)
        dot_number = "DOT" + "".join(str(rng.randint(0, 9)) for _ in range(7))
        vehicle_type = rng.choice(["semi-truck", "delivery van", "bus"])
        text = (
            f"Patient employed as {vehicle_type} driver; occupational injury. "
            f"Vehicle VIN: {vin}. "
            f"USDOT number: {dot_number}. "
            f"Vehicle inspection records obtained for occupational medicine review."
        )
        spans = [
            (vin, "VIN", "L", "us", AUTH_HIPAA_L, DETECTION_REGIME_RULE),
        ]
        return self._make(mode, i, text, spans, context="treatment",
                          metadata={"vehicle_context": mode, "hipaa_category": "L",
                                    "commercial_vehicle": True, "dot_number": dot_number})
