"""Australia jurisdiction PHI generators.

Primary authorities: Privacy Act 1988 (Cth) Australian Privacy Principles;
Healthcare Identifiers Act 2010 (Cth); My Health Records Act 2012 (Cth).
"""
from .au_privacy import AustraliaPrivacyGenerator, generate_corpus

__all__ = ["AustraliaPrivacyGenerator", "generate_corpus"]
