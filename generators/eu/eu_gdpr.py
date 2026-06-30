"""
EU GDPR multi-country identifier generator.

Covers national identifiers from eight EU member states plus principle-based
GDPR personal data categories. Includes conflict cases where HIPAA and GDPR
diverge on PHI status (ZIP codes, specific dates).

Primary authority: GDPR Article 4(1) (personal data definition)
Special category authority: GDPR Article 9(1)
Research safeguard authority: GDPR Article 89
Health data authority: GDPR Recital 35, Article 4(15)
EHDS authority: EU EHDS Regulation 2024/3175 Article 3

Country-specific co-authorities:
  DE: Bundesdatenschutzgesetz (BDSG) 2018 section 22 (health/biometric)
  FR: Loi Informatique et Libertes, CNIL deliberation on NIR/INSEE number
  NL: Wet bescherming persoonsgegevens / AVG; BSN restricted under Wabb
  IT: Codice in materia di protezione dei dati personali (D.Lgs. 196/2003)
  ES: Ley Organica de Proteccion de Datos y Garantia de Derechos Digitales
      (LOPDGDD) 3/2018
  SE: Dataskyddslag (2018:218)
  PL: Ustawa o ochronie danych osobowych (UODO) 2018
  DK: Databeskyttelsesloven 502/2018

Conflict case documentation:
  ZIP codes: HIPAA Safe Harbor removes ZIP3-restricted codes; GDPR Article 4(1)
    treats any postal code capable of singling out an individual as personal
    data (Recital 30). A German PLZ "10115" can identify a single household.
    Detection regime: conflict_case; conflict_jurisdictions: ["us", "eu"].
  Dates: HIPAA removes all date elements except year (45 CFR 164.514(b)(2)(i)(C));
    GDPR treats specific dates (e.g., exact birth date) as personal data
    under Article 4(1). Detection regime: conflict_case.
"""
from __future__ import annotations

import string
from pathlib import Path
from typing import List, Optional

from generators.common import (
    AUTH_EHDS_2024,
    AUTH_GDPR_ARTICLE_4,
    AUTH_GDPR_ARTICLE_4_14,
    AUTH_GDPR_ARTICLE_4_15,
    AUTH_GDPR_ARTICLE_9,
    AUTH_GDPR_ARTICLE_89,
    AUTH_GDPR_RECITAL_26,
    AUTH_GDPR_RECITAL_35,
    DETECTION_REGIME_CONFLICT,
    DETECTION_REGIME_NER,
    DETECTION_REGIME_RULE,
    LAYER_CONFLICT,
    LAYER_GDPR,
    DeterministicGenerator,
    Record,
    luhn_make,
    write_jsonl,
)

# ---------------------------------------------------------------------------
# Name pools: synthetic European names (common, not targeting real individuals)
# ---------------------------------------------------------------------------

DE_FIRST = ["Hans", "Klaus", "Petra", "Monika", "Stefan", "Sabine", "Thomas", "Ursula"]
DE_LAST = ["Muller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner"]
FR_FIRST = ["Jean", "Marie", "Pierre", "Isabelle", "Michel", "Catherine", "Philippe"]
FR_LAST = ["Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit"]
NL_FIRST = ["Jan", "Anna", "Peter", "Maria", "Hendrik", "Elisabeth", "Willem"]
NL_LAST = ["de Jong", "Jansen", "de Vries", "van den Berg", "Bakker", "Visser"]
IT_FIRST = ["Marco", "Giulia", "Lorenzo", "Francesca", "Andrea", "Sofia", "Luca"]
IT_LAST = ["Rossi", "Ferrari", "Esposito", "Bianchi", "Romano", "Colombo"]
ES_FIRST = ["Antonio", "Carmen", "Jose", "Maria", "Francisco", "Ana", "David"]
ES_LAST = ["Garcia", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez"]
SE_FIRST = ["Erik", "Anna", "Lars", "Maria", "Karl", "Karin", "Per", "Eva"]
SE_LAST = ["Andersson", "Johansson", "Karlsson", "Nilsson", "Eriksson", "Larsson"]
PL_FIRST = ["Piotr", "Anna", "Krzysztof", "Maria", "Tomasz", "Agnieszka", "Marek"]
PL_LAST = ["Kowalski", "Nowak", "Wisniewski", "Wojciechowski", "Kowalczyk"]
DK_FIRST = ["Lars", "Anne", "Peter", "Mette", "Niels", "Kirsten", "Jens", "Lene"]
DK_LAST = ["Jensen", "Nielsen", "Hansen", "Pedersen", "Andersen", "Christensen"]

# ---------------------------------------------------------------------------
# Checksum helpers
# ---------------------------------------------------------------------------


def _bsn_check_digit(d: List[int]) -> Optional[int]:
    """Compute BSN 9th digit such that weighted sum mod 11 == 0.

    Weights: [9, 8, 7, 6, 5, 4, 3, 2, -1]
    Authority: Wet algemene bepalingen burgerservicenummer (Wabb) Article 2.
    d must have exactly 8 elements (d[0]..d[7]).
    Returns the 9th digit (0-9) or None if no valid digit exists.
    """
    # partial = sum(d[i] * w[i] for i in 0..7) + d[8] * (-1) == 0 mod 11
    # => d[8] * 1 == partial mod 11 => d[8] == partial mod 11
    partial = sum(d[i] * (9 - i) for i in range(8))
    check = partial % 11
    if check <= 9:
        return check
    return None  # 10 is invalid, caller must retry


def make_bsn(rng) -> str:
    """Generate a BSN-checksum-valid 9-digit string."""
    for _ in range(200):
        # First digit must be non-zero
        d = [rng.randint(1 if i == 0 else 0, 9) for i in range(8)]
        check = _bsn_check_digit(d)
        if check is not None:
            return "".join(str(x) for x in d) + str(check)
    raise RuntimeError("BSN generation exceeded retry limit")


def _pesel_check(d9: List[int]) -> int:
    """Compute PESEL 11th check digit.

    Weights: [1, 3, 7, 9, 1, 3, 7, 9, 1, 3] on first 10 digits.
    check = (10 - (weighted_sum mod 10)) mod 10
    Authority: UODO 2018 / PESEL Act 24 September 2010.
    d9 has 10 elements.
    """
    weights = [1, 3, 7, 9, 1, 3, 7, 9, 1, 3]
    s = sum(d9[i] * weights[i] for i in range(10)) % 10
    return (10 - s) % 10


def make_pesel(rng) -> str:
    """Generate a PESEL-checksum-valid 11-digit string."""
    # Format: YYMMDD (encoded) + XXXXX where month encodes century
    # For simplicity generate a valid-looking date in 1970-1999 range
    yy = rng.randint(50, 99)  # born 1950-1999
    mm = rng.randint(1, 12)
    dd = rng.randint(1, 28)
    order = rng.randint(100, 999)
    sex_digit = rng.randint(0, 9)
    base = f"{yy:02d}{mm:02d}{dd:02d}{order}{sex_digit}"
    d10 = [int(c) for c in base]
    check = _pesel_check(d10)
    return base + str(check)


def _codice_fiscale_char(rng) -> str:
    """Generate a syntactically valid Codice Fiscale (16 chars).

    Pattern: [A-Z]{6}[0-9]{2}[A-Z][0-9]{2}[A-Z][0-9]{3}[A-Z]
    Authority: D.Lgs. 196/2003 Allegato B; DPR 605/1973.
    """
    letters = string.ascii_uppercase
    # surname code (3 consonants or padded with vowels, simplified to 3 alpha)
    part1 = "".join(rng.choices(letters, k=6))
    # year, month code
    part2 = f"{rng.randint(0, 99):02d}"
    month_codes = "ABCDEHLMPRST"
    part3 = rng.choice(month_codes)
    # day + sex encoded (01-71 for females adds 40, simplified)
    part4 = f"{rng.randint(1, 31):02d}"
    # municipality code: letter + 3 digits
    part5 = rng.choice(letters) + f"{rng.randint(0, 999):03d}"
    # control character
    part6 = rng.choice(letters)
    return part1 + part2 + part3 + part4 + part5 + part6


def _dni_es_letter(number: int) -> str:
    """Return the DNI check letter for a given integer.

    Table from Ministerio del Interior: 23 letters excluding I, O, U, N, Y.
    Authority: RD 1553/2005 Article 11.
    """
    table = "TRWAGMYFPDXBNJZSQVHLCKE"
    return table[number % 23]


def make_dni(rng) -> str:
    """Generate a valid Spanish DNI (8 digits + letter)."""
    number = rng.randint(10000000, 99999999)
    return f"{number}{_dni_es_letter(number)}"


def make_personnummer_se(rng) -> str:
    """Generate a Swedish personnummer (10 digits, Luhn-valid).

    Format: YYMMDD (birth date) + 3 digits + 1 Luhn check digit.
    Authority: Folkbokforingslag (1991:481) section 18; Dataskyddslag (2018:218).
    """
    yy = rng.randint(40, 99)
    mm = rng.randint(1, 12)
    dd = rng.randint(1, 28)
    seq = rng.randint(0, 999)
    base9 = f"{yy:02d}{mm:02d}{dd:02d}{seq:03d}"
    return luhn_make(base9)  # returns 10-digit string


# ---------------------------------------------------------------------------
# EU GDPR Generator
# ---------------------------------------------------------------------------


class EUGDPRGenerator(DeterministicGenerator):
    """Generate EU GDPR jurisdiction test records for 8 member states.

    Primary authority: GDPR Article 4(1) (personal data definition),
    Article 9(1) (special categories), Article 89 (research safeguards).

    Sub-generator countries: DE (Germany), FR (France), NL (Netherlands),
    IT (Italy), ES (Spain), SE (Sweden), PL (Poland), DK (Denmark).

    Includes conflict_case records for ZIP codes and exact dates where
    HIPAA and GDPR diverge on de-identification status.
    """

    def generate_batch(self, count_per_type: int = 4) -> List[Record]:
        """Generate count_per_type records per identifier type.

        Total records = (8 identifier types + 2 conflict types) * count_per_type.
        """
        records: List[Record] = []
        idx = 0

        # --- BSN_NL (Dutch Burgerservicenummer) ---
        rng_bsn = self.fresh("bsn_nl")
        for i in range(count_per_type):
            bsn = make_bsn(rng_bsn)
            first = rng_bsn.choice(NL_FIRST)
            last = rng_bsn.choice(NL_LAST)
            text = (
                f"Patient {first} {last} is registered with BSN {bsn} "
                f"at the Amsterdam Medical Center."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_NER),
                (bsn, "BSN_NL", None, "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("eu_bsn_nl", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_GDPR,
                jurisdiction="eu",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_GDPR_ARTICLE_4, AUTH_GDPR_ARTICLE_9],
                metadata={"country_code": "nl", "identifier_type": "BSN_NL"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"BSN_NL record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- NIR_FR (French numero INSEE / social security) ---
        rng_nir = self.fresh("nir_fr")
        for i in range(count_per_type):
            sex = rng_nir.choice(["1", "2"])
            yy = f"{rng_nir.randint(40, 99):02d}"
            mm = f"{rng_nir.randint(1, 12):02d}"
            # department code: 01-95, 971-976 (overseas); use 2-char codes
            dept = f"{rng_nir.randint(1, 95):02d}"
            commune = f"{rng_nir.randint(1, 999):03d}"
            order_ = f"{rng_nir.randint(1, 999):03d}"
            # NIR is 15 digits + 2 check digits = 17 total
            # Simplified: generate 15 random body + 2 random check digits
            body15 = sex + yy + mm + dept + commune + order_
            # Check digits: 97 - (number mod 97), where number = int(body15)
            check = 97 - (int(body15) % 97)
            nir = body15 + f"{check:02d}"
            first = rng_nir.choice(FR_FIRST)
            last = rng_nir.choice(FR_LAST)
            text = (
                f"Dossier medical de {first} {last}. "
                f"Numero de securite sociale: {nir}. "
                f"Etablissement: Hopital Saint-Louis, Paris."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_NER),
                (nir, "NIR_FR", None, "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("eu_nir_fr", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_GDPR,
                jurisdiction="eu",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_GDPR_ARTICLE_4, AUTH_GDPR_ARTICLE_9],
                metadata={"country_code": "fr", "identifier_type": "NIR_FR"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"NIR_FR record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- CODICE_FISCALE_IT (Italian fiscal code) ---
        rng_cf = self.fresh("codice_fiscale_it")
        for i in range(count_per_type):
            cf = _codice_fiscale_char(rng_cf)
            first = rng_cf.choice(IT_FIRST)
            last = rng_cf.choice(IT_LAST)
            text = (
                f"Paziente: {first} {last}. "
                f"Codice Fiscale: {cf}. "
                f"Reparto: Cardiologia, Ospedale Niguarda, Milano."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_NER),
                (cf, "CODICE_FISCALE_IT", None, "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("eu_codice_it", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_GDPR,
                jurisdiction="eu",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_GDPR_ARTICLE_4, AUTH_GDPR_ARTICLE_9],
                metadata={"country_code": "it", "identifier_type": "CODICE_FISCALE_IT"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"CODICE_FISCALE_IT record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- DNI_ES (Spanish national identity document) ---
        rng_dni = self.fresh("dni_es")
        for i in range(count_per_type):
            dni = make_dni(rng_dni)
            first = rng_dni.choice(ES_FIRST)
            last = rng_dni.choice(ES_LAST)
            text = (
                f"Historia clinica de {first} {last}. "
                f"DNI: {dni}. "
                f"Centro: Hospital Universitario La Paz, Madrid."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_NER),
                (dni, "DNI_ES", None, "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("eu_dni_es", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_GDPR,
                jurisdiction="eu",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_GDPR_ARTICLE_4, AUTH_GDPR_ARTICLE_9],
                metadata={"country_code": "es", "identifier_type": "DNI_ES"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"DNI_ES record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- PERSONNUMMER_SE (Swedish personal identity number) ---
        rng_se = self.fresh("personnummer_se")
        for i in range(count_per_type):
            pnr10 = make_personnummer_se(rng_se)
            # Format with separator: YYMMDD-XXXX
            pnr_fmt = pnr10[:6] + "-" + pnr10[6:]
            first = rng_se.choice(SE_FIRST)
            last = rng_se.choice(SE_LAST)
            text = (
                f"Patient {first} {last} (personnummer {pnr_fmt}) "
                f"intagen vid Karolinska Universitetssjukhuset."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_NER),
                (pnr_fmt, "PERSONNUMMER_SE", None, "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("eu_personnummer_se", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_GDPR,
                jurisdiction="eu",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_GDPR_ARTICLE_4, AUTH_GDPR_ARTICLE_9],
                metadata={"country_code": "se", "identifier_type": "PERSONNUMMER_SE"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"PERSONNUMMER_SE record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- STEUER_ID_DE (German tax identification number) ---
        rng_de = self.fresh("steuer_id_de")
        for i in range(count_per_type):
            # 11 digits; first digit 1-9; no leading zero; no triple-repeat
            while True:
                digits = [rng_de.randint(1, 9)] + [rng_de.randint(0, 9) for _ in range(10)]
                # Basic validity: no three identical consecutive digits
                valid = all(
                    not (digits[j] == digits[j + 1] == digits[j + 2])
                    for j in range(9)
                )
                if valid:
                    break
            steuer_id = "".join(str(d) for d in digits)
            first = rng_de.choice(DE_FIRST)
            last = rng_de.choice(DE_LAST)
            text = (
                f"Patientenakte: {first} {last}. "
                f"Steueridentifikationsnummer: {steuer_id}. "
                f"Krankenhaus: Charite Universitatsmedizin Berlin."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_NER),
                (steuer_id, "STEUER_ID_DE", None, "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("eu_steuer_id_de", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_GDPR,
                jurisdiction="eu",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="operations",
                authority_citations=[AUTH_GDPR_ARTICLE_4],
                metadata={"country_code": "de", "identifier_type": "STEUER_ID_DE"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"STEUER_ID_DE record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- PESEL_PL (Polish national identification number) ---
        rng_pl = self.fresh("pesel_pl")
        for i in range(count_per_type):
            pesel = make_pesel(rng_pl)
            first = rng_pl.choice(PL_FIRST)
            last = rng_pl.choice(PL_LAST)
            text = (
                f"Dokumentacja medyczna: {first} {last}. "
                f"PESEL: {pesel}. "
                f"Szpital: Szpital Kliniczny nr 1, Wroclaw."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_NER),
                (pesel, "PESEL_PL", None, "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("eu_pesel_pl", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_GDPR,
                jurisdiction="eu",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_GDPR_ARTICLE_4, AUTH_GDPR_ARTICLE_9],
                metadata={"country_code": "pl", "identifier_type": "PESEL_PL"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"PESEL_PL record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- CPR_DK (Danish civil registration number) ---
        rng_dk = self.fresh("cpr_dk")
        for i in range(count_per_type):
            dd = rng_dk.randint(1, 28)
            mm = rng_dk.randint(1, 12)
            yy = rng_dk.randint(40, 99)
            seq = rng_dk.randint(1000, 9999)
            cpr = f"{dd:02d}{mm:02d}{yy:02d}-{seq:04d}"
            first = rng_dk.choice(DK_FIRST)
            last = rng_dk.choice(DK_LAST)
            text = (
                f"Patient {first} {last} (CPR-nummer: {cpr}) "
                f"indlagt pa Rigshospitalet, Kobenhavn."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_NER),
                (cpr, "CPR_DK", None, "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("eu_cpr_dk", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_GDPR,
                jurisdiction="eu",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_GDPR_ARTICLE_4, AUTH_GDPR_ARTICLE_9],
                metadata={"country_code": "dk", "identifier_type": "CPR_DK"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"CPR_DK record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- Conflict cases: ZIP codes ---
        # HIPAA: restricted ZIP3 codes only (17 codes); GDPR: any postal code
        # that can single out an individual is personal data (Recital 30).
        # A German PLZ "10115" (Berlin Mitte) is personal data under GDPR
        # but would be retained in a HIPAA Safe Harbor de-identification
        # (non-restricted ZIP3 "101").
        rng_zip = self.fresh("conflict_zip_eu")
        de_plz_examples = [
            "10115", "20095", "80331", "40210", "60311",
            "50667", "70173", "01067", "30159", "90402",
        ]
        for i in range(count_per_type):
            plz = rng_zip.choice(de_plz_examples)
            first = rng_zip.choice(DE_FIRST)
            last = rng_zip.choice(DE_LAST)
            dob_year = rng_zip.randint(1945, 1985)
            dob_month = rng_zip.randint(1, 12)
            dob_day = rng_zip.randint(1, 28)
            dob_str = f"{dob_day:02d}.{dob_month:02d}.{dob_year}"
            text = (
                f"Forschungsakte: {first} {last}, "
                f"geboren am {dob_str}, wohnhaft in PLZ {plz}. "
                f"GDPR: PLZ gilt als personenbezogenes Datum (Art. 4 Abs. 1). "
                f"HIPAA Safe Harbor: PLZ {plz[:3]} ist kein eingeschraenkter ZIP3-Code."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_NER),
                (dob_str, "DATE_OF_BIRTH", "C", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_CONFLICT),
                (plz, "POSTAL_CODE_EU", "B", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_CONFLICT),
            ])
            rec = Record(
                record_id=self.record_id("eu_conflict_zip", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_CONFLICT,
                jurisdiction="eu",
                detection_regime=DETECTION_REGIME_CONFLICT,
                de_id_tier="identifiable",
                context="research",
                authority_citations=[AUTH_GDPR_ARTICLE_4, AUTH_GDPR_RECITAL_26],
                metadata={
                    "country_code": "de",
                    "identifier_type": "POSTAL_CODE_EU",
                    "conflict_jurisdictions": ["us", "eu"],
                    "conflict_note": (
                        "HIPAA Safe Harbor permits non-restricted ZIP3 codes; "
                        "GDPR Article 4(1) and Recital 30 treat postal codes "
                        "capable of singling out individuals as personal data."
                    ),
                },
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Conflict ZIP record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- Conflict cases: specific dates ---
        # HIPAA: removes all date elements except year (164.514(b)(2)(i)(C)).
        # GDPR: exact birth date is personal data under Article 4(1); year alone
        # may be insufficient to identify but full date in context is personal.
        rng_date = self.fresh("conflict_date_eu")
        for i in range(count_per_type):
            yr = rng_date.randint(1950, 1990)
            mo = rng_date.randint(1, 12)
            dy = rng_date.randint(1, 28)
            dob_full = f"{dy:02d}/{mo:02d}/{yr}"
            dob_year_only = str(yr)
            first = rng_date.choice(FR_FIRST)
            last = rng_date.choice(FR_LAST)
            text = (
                f"Patient {first} {last} date de naissance: {dob_full}. "
                f"Note HIPAA: seule l'annee {dob_year_only} serait conservee apres "
                f"de-identification Safe Harbor (45 CFR 164.514(b)(2)(i)(C)). "
                f"Note RGPD: la date complete constitue une donnee personnelle "
                f"au sens de l'article 4 paragraphe 1."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_NER),
                (dob_full, "DATE_OF_BIRTH", "C", "eu",
                 AUTH_GDPR_ARTICLE_4, DETECTION_REGIME_CONFLICT),
            ])
            rec = Record(
                record_id=self.record_id("eu_conflict_date", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_CONFLICT,
                jurisdiction="eu",
                detection_regime=DETECTION_REGIME_CONFLICT,
                de_id_tier="identifiable",
                context="research",
                authority_citations=[AUTH_GDPR_ARTICLE_4, AUTH_GDPR_ARTICLE_89],
                metadata={
                    "country_code": "fr",
                    "identifier_type": "DATE_OF_BIRTH",
                    "conflict_jurisdictions": ["us", "eu"],
                    "conflict_note": (
                        "HIPAA Safe Harbor removes all date elements except year "
                        "(45 CFR 164.514(b)(2)(i)(C)); GDPR Article 4(1) treats "
                        "the full date of birth as personal data requiring protection."
                    ),
                },
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Conflict date record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        return records


def generate_corpus(seed: int = 42, count_per_type: int = 4) -> List[Record]:
    """Generate EU GDPR corpus and write to corpus/eu/eu_identifiers.jsonl.

    Authority: GDPR Article 4(1), Article 9(1), Article 89.
    Returns the list of generated Record objects.
    """
    gen = EUGDPRGenerator(seed=seed)
    records = gen.generate_batch(count_per_type=count_per_type)
    out_path = Path(__file__).resolve().parents[2] / "corpus" / "eu" / "eu_identifiers.jsonl"
    count = write_jsonl(records, out_path)
    return records
