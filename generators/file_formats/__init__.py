"""
File-format PHI corpus generators.

Each module produces synthetic records where PHI is embedded inside a
specific file format (DICOM header, FHIR JSON, HL7 v2 message, RFC 5322
email). The format field on every Record reflects the container type.

Authority: See authorities/AUTHORITY_MATRIX.md Table A for the full
identifier-to-jurisdiction mapping that drives these generators.
"""
from generators.file_formats.dicom_header_gen import DICOMHeaderGenerator, generate_corpus as dicom_corpus
from generators.file_formats.fhir_gen import FHIRGenerator, generate_corpus as fhir_corpus
from generators.file_formats.hl7v2_gen import HL7v2Generator, generate_corpus as hl7v2_corpus
from generators.file_formats.eml_gen import EMLGenerator, generate_corpus as eml_corpus

__all__ = [
    "DICOMHeaderGenerator",
    "FHIRGenerator",
    "HL7v2Generator",
    "EMLGenerator",
    "dicom_corpus",
    "fhir_corpus",
    "hl7v2_corpus",
    "eml_corpus",
]
