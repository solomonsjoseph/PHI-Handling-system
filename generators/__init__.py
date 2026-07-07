"""
PHI corpus generators package.

Generator inventory by jurisdiction:
  USA (HIPAA):
    hipaa_safe_harbor  -- All 18 Safe Harbor categories (164.514(b)(2)(i)(A-R)) + quasi-identifiers
    hipaa_lds          -- Limited Data Set tier (164.514(e))
    hipaa_reid_codes   -- Re-identification code compliance (164.514(c))
    hipaa_fundraising  -- Fundraising context PHI (164.514(f))
    hipaa_verification -- Disclosure verification audit logs (164.514(h))
    hipaa_biometric    -- Biometric identifiers dedicated (164.514(b)(2)(i)(P))
    hipaa_device       -- Device identifiers dedicated (164.514(b)(2)(i)(M)); GS1/HIBCC/ICCBBA UDI
    hipaa_fax          -- Fax numbers dedicated (164.514(b)(2)(i)(E)); disambiguation vs phone
    hipaa_vehicle      -- Vehicle identifiers dedicated (164.514(b)(2)(i)(L)); ISO 3779 VIN + state plates
"""
from .hipaa_safe_harbor import HIPAASafeHarborGenerator, HIPAAQuasiIdentifierGenerator
from .hipaa_lds import HIPAALDSGenerator
from .hipaa_reid_codes import HIPAAReIDCodesGenerator
from .hipaa_fundraising import HIPAAFundraisingGenerator
from .hipaa_verification import HIPAAVerificationGenerator
from .hipaa_biometric import HIPAABiometricGenerator
from .hipaa_device import HIPAADeviceGenerator
from .hipaa_fax import HIPAAFaxGenerator
from .hipaa_vehicle import HIPAAVehicleGenerator
from importlib import import_module

IndiaDPDPAGenerator = import_module("generators.in.in_dpdpa").IndiaDPDPAGenerator
IndiaIdentifierGenerator = import_module("generators.in.in_identifiers").IndiaIdentifierGenerator
from .eu.eu_gdpr import EUGDPRGenerator
from .br.br_lgpd import BrazilLGPDGenerator
from .au.au_privacy import AustraliaPrivacyGenerator
from .ug.ug_dppa import UgandaDPPAGenerator
from .file_formats.dicom_header_gen import DICOMHeaderGenerator
from .file_formats.fhir_gen import FHIRGenerator
from .file_formats.hl7v2_gen import HL7v2Generator
from .file_formats.eml_gen import EMLGenerator
from .file_formats.xlsx_gen import XlsxGenerator

__all__ = [
    "HIPAASafeHarborGenerator",
    "HIPAAQuasiIdentifierGenerator",
    "HIPAALDSGenerator",
    "HIPAAReIDCodesGenerator",
    "HIPAAFundraisingGenerator",
    "HIPAAVerificationGenerator",
    "HIPAABiometricGenerator",
    "HIPAADeviceGenerator",
    "HIPAAFaxGenerator",
    "HIPAAVehicleGenerator",
    "IndiaDPDPAGenerator",
    "IndiaIdentifierGenerator",
    "EUGDPRGenerator",
    "BrazilLGPDGenerator",
    "AustraliaPrivacyGenerator",
    "UgandaDPPAGenerator",
    "DICOMHeaderGenerator",
    "FHIRGenerator",
    "HL7v2Generator",
    "EMLGenerator",
    "XlsxGenerator",
]
