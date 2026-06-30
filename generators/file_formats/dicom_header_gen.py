"""
DICOM header PHI corpus generator.

Produces synthetic DICOM datasets (header-only, no pixel data) covering the
tags enumerated in the DICOM PS3.15 Annex E Basic Confidentiality Profile.

Authority: DICOM PS3.15 Annex E Basic Confidentiality Profile
           (AUTH_DICOM_BACP in generators/common.py)

Each record's text field is json.dumps(header_dict, indent=2) so that PHI
values are findable by offset. Gold spans annotate the values as they appear
in the JSON string.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict, List

try:
    import pydicom
    from pydicom.dataset import Dataset
    PYDICOM_AVAILABLE = True
except ImportError:
    PYDICOM_AVAILABLE = False

from generators.common import (
    AUTH_DICOM_BACP,
    AUTH_HIPAA_SAFE_HARBOR,
    DETECTION_REGIME_RULE,
    DETECTION_REGIME_NER,
    LAYER_HIPAA,
    DeterministicGenerator,
    GoldSpan,
    Record,
    write_jsonl,
)

# Synthetic name pools -- no real individuals
_FIRST_NAMES = [
    "James", "Maria", "Robert", "Linda", "Michael", "Barbara",
    "William", "Patricia", "David", "Jennifer", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen",
    "Christopher", "Nancy",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia",
    "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez",
    "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore",
    "Jackson", "Martin",
]
_INSTITUTIONS = [
    "General Hospital", "University Medical Center", "Regional Health System",
    "Community Medical Center", "St. Francis Hospital", "Memorial Health",
    "Riverside Medical", "Lakeside Clinic", "Highland Medical Center",
    "Northside Hospital",
]
_MODALITIES = ["CT", "MR", "US", "XR", "NM", "PT", "DX", "CR", "MG", "RF"]
_SEXES = ["M", "F", "O"]


def _random_date(rng: random.Random, start_year: int = 1940, end_year: int = 2000) -> str:
    """Return YYYYMMDD string for a random date in the range."""
    year = rng.randint(start_year, end_year)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}{month:02d}{day:02d}"


def _random_study_date(rng: random.Random) -> str:
    year = rng.randint(2010, 2024)
    month = rng.randint(1, 12)
    day = rng.randint(1, 28)
    return f"{year:04d}{month:02d}{day:02d}"


def _seeded_uid(rng: random.Random) -> str:
    """Generate a deterministic DICOM UID using seeded RNG (2.25 root prefix)."""
    # 2.25 prefix + 39-digit integer derived from seeded random
    num = rng.randint(10**18, 10**19 - 1)
    return f"2.25.{num}"


def _random_accession(rng: random.Random) -> str:
    return "ACC" + "".join(str(rng.randint(0, 9)) for _ in range(9))


def _random_mrn(rng: random.Random) -> str:
    return "MRN" + "".join(str(rng.randint(0, 9)) for _ in range(7))


def _random_age(rng: random.Random) -> str:
    """DICOM age string e.g. 045Y."""
    return f"{rng.randint(1, 89):03d}Y"


def _build_dicom_dataset(
    rng: random.Random,
    record_index: int,
) -> tuple[Dataset, Dict[str, Any]]:
    """Build a synthetic pydicom Dataset and a JSON-serialisable header dict.

    Returns (ds, header_dict). header_dict maps DICOM keyword -> value string
    for easy JSON serialisation and offset searching.
    """
    if not PYDICOM_AVAILABLE:
        raise RuntimeError("pydicom is required: pip install pydicom")

    first = rng.choice(_FIRST_NAMES)
    last = rng.choice(_LAST_NAMES)
    patient_name_str = f"{last}^{first}"
    patient_id = _random_mrn(rng)
    dob = _random_date(rng)
    sex = rng.choice(_SEXES)
    age = _random_age(rng)
    street_num = rng.randint(100, 9999)
    street = f"{street_num} Oak Street, Springfield, IL 62701"
    referring_physician = f"{rng.choice(_LAST_NAMES)}^{rng.choice(_FIRST_NAMES)}"
    physician_of_record = f"{rng.choice(_LAST_NAMES)}^{rng.choice(_FIRST_NAMES)}"
    institution = rng.choice(_INSTITUTIONS)
    study_date = _random_study_date(rng)
    accession = _random_accession(rng)
    modality = rng.choice(_MODALITIES)

    ds = Dataset()
    # Patient module (PS3.3 C.7.1.1)
    ds.PatientName = patient_name_str
    ds.PatientID = patient_id
    ds.PatientBirthDate = dob
    ds.PatientSex = sex
    ds.PatientAge = age
    ds.PatientAddress = street

    # General Study module (PS3.3 C.7.2.1)
    ds.StudyDate = study_date
    ds.AccessionNumber = accession
    ds.ReferringPhysicianName = referring_physician
    ds.StudyInstanceUID = _seeded_uid(rng)
    ds.StudyID = f"STD{record_index:05d}"

    # General Equipment module
    ds.InstitutionName = institution

    # General Series module
    ds.Modality = modality
    ds.SeriesInstanceUID = _seeded_uid(rng)
    ds.SeriesNumber = str(rng.randint(1, 99))

    # Physician of Record (0008,1048) -- tag by number to avoid pydicom keyword warning
    ds[0x00081048] = pydicom.DataElement(0x00081048, "PN", physician_of_record)

    # SOP Common
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage
    ds.SOPInstanceUID = _seeded_uid(rng)

    header_dict: Dict[str, Any] = {
        "PatientName": patient_name_str,
        "PatientID": patient_id,
        "PatientBirthDate": dob,
        "PatientSex": sex,
        "PatientAge": age,
        "PatientAddress": street,
        "StudyDate": study_date,
        "AccessionNumber": accession,
        "ReferringPhysicianName": referring_physician,
        "InstitutionName": institution,
        "Modality": modality,
        "PhysicianOfRecord": physician_of_record,
        "SOPClassUID": str(ds.SOPClassUID),
        "SOPInstanceUID": str(ds.SOPInstanceUID),
    }
    return ds, header_dict


class DICOMHeaderGenerator(DeterministicGenerator):
    """Generate synthetic DICOM header records.

    Authority: DICOM PS3.15 Annex E Basic Confidentiality Profile
    Each tag covered maps to one or more HIPAA 164.514(b)(2)(i) categories.
    """

    def generate_batch(self, count: int = 20) -> List[Record]:
        records: List[Record] = []
        for i in range(count):
            rng = self.fresh(f"dicom:{i}")
            ds, header_dict = _build_dicom_dataset(rng, i)
            text = json.dumps(header_dict, indent=2)

            patient_name_str = header_dict["PatientName"]
            patient_id = header_dict["PatientID"]
            dob = header_dict["PatientBirthDate"]
            accession = header_dict["AccessionNumber"]
            institution = header_dict["InstitutionName"]
            referring = header_dict["ReferringPhysicianName"]

            spans_spec = [
                (patient_name_str, "NAME", "A", "us", AUTH_DICOM_BACP, DETECTION_REGIME_NER),
                (patient_id, "MRN", "H", "us", AUTH_DICOM_BACP, DETECTION_REGIME_RULE),
                (dob, "DATE", "C", "us", AUTH_DICOM_BACP, DETECTION_REGIME_RULE),
                (accession, "ACCESSION_NUMBER", "R", "us", AUTH_DICOM_BACP, DETECTION_REGIME_RULE),
                (institution, "INSTITUTION_NAME", "R", "us", AUTH_DICOM_BACP, DETECTION_REGIME_NER),
                (referring, "NAME", "A", "us", AUTH_DICOM_BACP, DETECTION_REGIME_NER),
            ]

            gold_spans = self.annotate(text, spans_spec)

            record = Record(
                record_id=self.record_id("dicom_header", i),
                text=text,
                gold_spans=gold_spans,
                layer=LAYER_HIPAA,
                jurisdiction="us",
                detection_regime=DETECTION_REGIME_NER,
                de_id_tier="identifiable",
                risk_tier="minimal",
                context="treatment",
                format="dicom_header",
                authority_citations=[AUTH_DICOM_BACP, AUTH_HIPAA_SAFE_HARBOR],
                metadata={
                    "modality": header_dict["Modality"],
                    "sop_class_uid": header_dict["SOPClassUID"],
                    "pydicom_available": PYDICOM_AVAILABLE,
                },
            )

            errors = record.verify_spans()
            if errors:
                raise ValueError(f"Record {i} span errors: {errors}")

            records.append(record)
        return records

    def get_raw_datasets(self, count: int = 20) -> List[Dataset]:
        """Return actual pydicom Dataset objects (for pydicom-load tests)."""
        datasets = []
        for i in range(count):
            rng = self.fresh(f"dicom:{i}")
            ds, _ = _build_dicom_dataset(rng, i)
            datasets.append(ds)
        return datasets


def generate_corpus(seed: int = 42, count: int = 20) -> List[Record]:
    """Write DICOM header corpus to corpus/file_formats/dicom_headers.jsonl.

    Authority: DICOM PS3.15 Annex E Basic Confidentiality Profile
    """
    gen = DICOMHeaderGenerator(seed=seed)
    records = gen.generate_batch(count=count)
    out_path = Path(__file__).parent.parent.parent / "corpus" / "file_formats" / "dicom_headers.jsonl"
    written = write_jsonl(records, out_path)
    return records
