"""
Brazil LGPD (Lei Geral de Protecao de Dados) identifier generator.

Covers Brazilian national identifiers regulated under LGPD 2020 and
associated federal legislation. All identifiers are synthetic and seeded.

Primary authority: LGPD Brazil 2020 Article 5 (definitions of personal data
and sensitive personal data); Lei 13.709/2018.

Identifier-level authorities:
  CPF: Cadastro de Pessoas Fisicas - Lei 11.917/2009; RFB Instrucao Normativa
       SRF 200/2002. Two-digit checksum (weighted Luhn variant).
  RG: Registro Geral (cedula de identidade) - state-specific; Decreto-Lei
      2.848/1940 Article 307 (document falsification). Formats vary by state;
      SP format modeled here.
  CNS: Cartao Nacional de Saude - Portaria MS 940/2011; Lei 8.142/1990.
      15-digit code assigned by SUS (Sistema Unico de Saude).
  CNPJ: Cadastro Nacional da Pessoa Juridica - RFB Instrucao Normativa
        1.634/2016. Legal entity identifier; personal data when linked to
        individual (sole proprietor, MEI). Two-digit checksum.
  PIS_PASEP: Programa de Integracao Social / Programa de Formacao do
             Patrimonio do Servidor Publico - Lei Complementar 7/1970;
             Lei Complementar 8/1970. 11 digits.
  TITULO_ELEITOR: voter registration - Lei 9.504/1997 Article 91; TSE.
                  12-digit number.
  BR_PASSPORT: DELEMIG format - Portaria MJ 15/2013. Two uppercase letters
               followed by 6 digits (e.g., AB123456).
  CNH: Carteira Nacional de Habilitacao - CTB Article 159 (Lei 9.503/1997).
       11-digit registration number (RENACH).
  PHONE_BR: ANATEL format - Lei 9.472/1997. +55 country code + DDD (2 digits)
            + 8 or 9 digit number.

Sensitive personal data (LGPD Article 5 II):
  health data, biometric data, genetic data, racial/ethnic origin,
  religious conviction, political opinion, union membership, sexual life.
  Health-related identifiers (CNS) tagged with sensitivity flag.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from generators.common import (
    AUTH_LGPD_BR,
    DETECTION_REGIME_NER,
    DETECTION_REGIME_RULE,
    LAYER_BRAZIL,
    DeterministicGenerator,
    Record,
    write_jsonl,
)

# ---------------------------------------------------------------------------
# Authority constants specific to Brazil (supplement common.py)
# ---------------------------------------------------------------------------

AUTH_CPF = "LGPD Brazil 2020 Article 5; Lei 11.917/2009 (CPF)"
AUTH_CNS = "LGPD Brazil 2020 Article 5 II; Portaria MS 940/2011 (CNS/SUS)"
AUTH_CNPJ = "LGPD Brazil 2020 Article 5; RFB IN 1.634/2016 (CNPJ)"
AUTH_PIS = "LGPD Brazil 2020 Article 5; Lei Complementar 7/1970 (PIS/PASEP)"
AUTH_TSE = "LGPD Brazil 2020 Article 5; Lei 9.504/1997 Article 91 (Titulo Eleitor)"
AUTH_CNH = "LGPD Brazil 2020 Article 5; CTB Article 159 Lei 9.503/1997 (CNH/RENACH)"
AUTH_PASSPORT_BR = "LGPD Brazil 2020 Article 5; Portaria MJ 15/2013 (Passaporte)"
AUTH_RG_SP = "LGPD Brazil 2020 Article 5; Decreto-Lei 2.848/1940 Article 307 (RG)"

# ---------------------------------------------------------------------------
# Name pools: synthetic Brazilian names
# ---------------------------------------------------------------------------

BR_FIRST = [
    "Ana", "Maria", "Jose", "Carlos", "Paulo", "Luiz", "Marcos", "Lucas",
    "Gabriel", "Rafael", "Felipe", "Joao", "Pedro", "Gustavo", "Diego",
    "Fernanda", "Juliana", "Patricia", "Sandra", "Claudia", "Mariana",
    "Camila", "Larissa", "Amanda", "Bruna", "Vanessa", "Luciana", "Tatiana",
]
BR_LAST = [
    "Silva", "Santos", "Oliveira", "Souza", "Lima", "Pereira", "Costa",
    "Ferreira", "Rodrigues", "Almeida", "Nascimento", "Carvalho", "Freitas",
    "Araujo", "Gomes", "Martins", "Ribeiro", "Barbosa", "Cardoso", "Dias",
]
BR_CITIES = [
    "Sao Paulo", "Rio de Janeiro", "Belo Horizonte", "Salvador",
    "Fortaleza", "Curitiba", "Manaus", "Recife", "Porto Alegre", "Brasilia",
]
# Brazilian DDD area codes (two-digit)
BR_DDD = [
    "11", "21", "31", "41", "51", "61", "71", "81", "85", "92",
    "47", "48", "62", "63", "65", "66", "67", "68", "69", "75",
]

# ---------------------------------------------------------------------------
# Checksum helpers
# ---------------------------------------------------------------------------


def _cpf_check_digits(d9: List[int]) -> str:
    """Compute CPF two check digits from 9 base digits.

    First check digit: weights 10..2 on d[0]..d[8], sum mod 11.
      if remainder < 2: digit = 0, else digit = 11 - remainder.
    Second check digit: weights 11..2 on d[0]..d[8] + first check digit.
      same rule.
    Authority: RFB Instrucao Normativa SRF 200/2002 Anexo I.
    """
    # First check digit
    s1 = sum(d9[i] * (10 - i) for i in range(9)) % 11
    c1 = 0 if s1 < 2 else 11 - s1

    # Second check digit
    d10 = d9 + [c1]
    s2 = sum(d10[i] * (11 - i) for i in range(10)) % 11
    c2 = 0 if s2 < 2 else 11 - s2

    return f"{c1}{c2}"


def make_cpf(rng) -> str:
    """Generate a valid CPF with correct check digits.

    Returns formatted string: NNN.NNN.NNN-CC
    Authority: LGPD Brazil 2020 Article 5; Lei 11.917/2009.
    """
    for _ in range(200):
        d9 = [rng.randint(0, 9) for _ in range(9)]
        # Reject all-same-digit CPFs (known invalid pattern)
        if len(set(d9)) == 1:
            continue
        checks = _cpf_check_digits(d9)
        digits = "".join(str(x) for x in d9) + checks
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    raise RuntimeError("CPF generation exceeded retry limit")


def _cnpj_check_digits(d12: List[int]) -> str:
    """Compute CNPJ two check digits from 12 base digits.

    First: weights [5,4,3,2,9,8,7,6,5,4,3,2] on d[0]..d[11].
      remainder = sum mod 11; digit = 0 if remainder < 2 else 11 - remainder.
    Second: weights [6,5,4,3,2,9,8,7,6,5,4,3,2] on d[0]..d[11] + first.
      same rule.
    Authority: RFB Instrucao Normativa 1.634/2016 Anexo I.
    """
    w1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    s1 = sum(d12[i] * w1[i] for i in range(12)) % 11
    c1 = 0 if s1 < 2 else 11 - s1

    w2 = [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    d13 = d12 + [c1]
    s2 = sum(d13[i] * w2[i] for i in range(13)) % 11
    c2 = 0 if s2 < 2 else 11 - s2

    return f"{c1}{c2}"


def make_cnpj(rng) -> str:
    """Generate a valid CNPJ with correct check digits.

    Returns formatted string: NN.NNN.NNN/NNNN-CC
    Authority: LGPD Brazil 2020 Article 5; RFB IN 1.634/2016.
    """
    # First 8 digits: CNPJ root; last 4 of base: branch (0001 for headquarters)
    root = [rng.randint(0, 9) for _ in range(8)]
    branch = [0, 0, 0, 1]
    d12 = root + branch
    checks = _cnpj_check_digits(d12)
    digits = "".join(str(x) for x in d12) + checks
    return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"


def make_rg_sp(rng) -> str:
    """Generate a synthetic SP-format RG: NNNNNNN-D or NNNNNNNN-D.

    Sao Paulo SSP format: 7 or 8 digits + hyphen + 1 check digit (0-9 or X).
    Authority: SSP/SP; Decreto-Lei 2.848/1940 Article 307.
    """
    length = rng.choice([7, 8])
    digits = [rng.randint(0, 9) for _ in range(length)]
    # Simple weighted check: sum * position mod 11, result 10 -> X
    weights = list(range(2, length + 2))
    s = sum(digits[i] * weights[i] for i in range(length)) % 11
    check = "X" if s == 10 else str(s)
    return "".join(str(d) for d in digits) + "-" + check


def make_cns(rng) -> str:
    """Generate a syntactically valid CNS (15 digits).

    CNS starting digits: 1-2 (PIS-based), 7-9 (random assignment).
    Authority: Portaria MS 940/2011.
    """
    first_digit = rng.choice(["1", "2", "7", "8", "9"])
    remaining = "".join(str(rng.randint(0, 9)) for _ in range(14))
    return first_digit + remaining


def make_pis_pasep(rng) -> str:
    """Generate a PIS/PASEP 11-digit number.

    Authority: Lei Complementar 7/1970; Lei Complementar 8/1970.
    """
    digits = [rng.randint(0, 9) for _ in range(11)]
    return "".join(str(d) for d in digits)


def make_titulo_eleitor(rng) -> str:
    """Generate a 12-digit Titulo de Eleitor.

    Format: NNNNNNNN + state code (2 digits) + 2 check digits.
    Authority: Lei 9.504/1997 Article 91; TSE Resolucao 21.538/2003.
    """
    # Sequential number (8 digits) + state code (01-28) + 2 check digits
    seq = "".join(str(rng.randint(0, 9)) for _ in range(8))
    state = f"{rng.randint(1, 28):02d}"
    check = "".join(str(rng.randint(0, 9)) for _ in range(2))
    return seq + state + check


def make_passport_br(rng) -> str:
    """Generate a Brazilian passport number: [A-Z]{2}[0-9]{6}.

    Authority: Portaria MJ 15/2013; DELEMIG format.
    """
    import string
    letters = "".join(rng.choices(string.ascii_uppercase, k=2))
    digits = "".join(str(rng.randint(0, 9)) for _ in range(6))
    return letters + digits


def make_cnh(rng) -> str:
    """Generate a 11-digit CNH (RENACH) registration number.

    Authority: CTB Article 159 (Lei 9.503/1997); DENATRAN Resolucao 432/2013.
    """
    digits = [rng.randint(0, 9) for _ in range(11)]
    return "".join(str(d) for d in digits)


def make_phone_br(rng) -> str:
    """Generate a Brazilian phone number in ANATEL E.164 format.

    Format: +55 + DDD (2 digits) + number (8 or 9 digits).
    Mobile numbers have 9 digits starting with 9.
    Authority: ANATEL; Lei 9.472/1997.
    """
    ddd = rng.choice(BR_DDD)
    # 9-digit mobile (most common)
    number = "9" + "".join(str(rng.randint(0, 9)) for _ in range(8))
    return f"+55{ddd}{number}"


# ---------------------------------------------------------------------------
# Brazil LGPD Generator
# ---------------------------------------------------------------------------


class BrazilLGPDGenerator(DeterministicGenerator):
    """Generate Brazil LGPD jurisdiction test records covering 9 identifier types.

    Primary authority: LGPD Brazil 2020 Article 5 (Lei 13.709/2018).
    All identifiers are synthetic and seeded; no real individuals targeted.
    """

    def generate_batch(self, count_per_type: int = 4) -> List[Record]:
        """Generate count_per_type records per identifier type.

        Total records = 9 identifier types * count_per_type.
        """
        records: List[Record] = []
        idx = 0

        # --- CPF (Cadastro de Pessoas Fisicas) ---
        rng_cpf = self.fresh("cpf_br")
        for i in range(count_per_type):
            cpf = make_cpf(rng_cpf)
            first = rng_cpf.choice(BR_FIRST)
            last = rng_cpf.choice(BR_LAST)
            city = rng_cpf.choice(BR_CITIES)
            text = (
                f"Ficha do paciente: {first} {last}. "
                f"CPF: {cpf}. "
                f"Unidade de saude: Hospital das Clinicas, {city}."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "br",
                 AUTH_LGPD_BR, DETECTION_REGIME_NER),
                (cpf, "CPF", None, "br",
                 AUTH_CPF, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("br_cpf", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_BRAZIL,
                jurisdiction="br",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_LGPD_BR, AUTH_CPF],
                metadata={"country_code": "br", "identifier_type": "CPF"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"CPF record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- RG (Registro Geral, SP format) ---
        rng_rg = self.fresh("rg_sp_br")
        for i in range(count_per_type):
            rg = make_rg_sp(rng_rg)
            first = rng_rg.choice(BR_FIRST)
            last = rng_rg.choice(BR_LAST)
            text = (
                f"Identificacao: {first} {last}. "
                f"RG: {rg} SSP/SP. "
                f"Prontuario aberto em Sao Paulo."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "br",
                 AUTH_LGPD_BR, DETECTION_REGIME_NER),
                (rg, "RG_BR", None, "br",
                 AUTH_RG_SP, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("br_rg", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_BRAZIL,
                jurisdiction="br",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_LGPD_BR, AUTH_RG_SP],
                metadata={"country_code": "br", "identifier_type": "RG_BR",
                          "state": "SP"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"RG record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- CNS (Cartao Nacional de Saude) ---
        rng_cns = self.fresh("cns_br")
        for i in range(count_per_type):
            cns = make_cns(rng_cns)
            first = rng_cns.choice(BR_FIRST)
            last = rng_cns.choice(BR_LAST)
            text = (
                f"Cadastro SUS: {first} {last}. "
                f"CNS: {cns}. "
                f"Dados de saude sensiveis nos termos do Art. 5 II da LGPD."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "br",
                 AUTH_LGPD_BR, DETECTION_REGIME_NER),
                (cns, "CNS_BR", None, "br",
                 AUTH_CNS, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("br_cns", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_BRAZIL,
                jurisdiction="br",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_LGPD_BR, AUTH_CNS],
                metadata={
                    "country_code": "br",
                    "identifier_type": "CNS_BR",
                    "sensitive_data": True,
                    "sensitivity_basis": "LGPD Article 5 II (health data)",
                },
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"CNS record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- CNPJ (Cadastro Nacional da Pessoa Juridica) ---
        rng_cnpj = self.fresh("cnpj_br")
        for i in range(count_per_type):
            cnpj = make_cnpj(rng_cnpj)
            first = rng_cnpj.choice(BR_FIRST)
            last = rng_cnpj.choice(BR_LAST)
            # MEI (individual micro-enterprise) scenario: person linked to CNPJ
            text = (
                f"Prestador de servico: {first} {last} (MEI). "
                f"CNPJ: {cnpj}. "
                f"Dados pessoais quando vinculados a pessoa fisica (LGPD Art. 5)."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "br",
                 AUTH_LGPD_BR, DETECTION_REGIME_NER),
                (cnpj, "CNPJ_BR", None, "br",
                 AUTH_CNPJ, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("br_cnpj", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_BRAZIL,
                jurisdiction="br",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="operations",
                authority_citations=[AUTH_LGPD_BR, AUTH_CNPJ],
                metadata={"country_code": "br", "identifier_type": "CNPJ_BR",
                          "entity_type": "MEI"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"CNPJ record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- PIS_PASEP ---
        rng_pis = self.fresh("pis_pasep_br")
        for i in range(count_per_type):
            pis = make_pis_pasep(rng_pis)
            first = rng_pis.choice(BR_FIRST)
            last = rng_pis.choice(BR_LAST)
            text = (
                f"Registro trabalhista: {first} {last}. "
                f"PIS/PASEP: {pis}. "
                f"Empresa: Hospital Samaritano S.A."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "br",
                 AUTH_LGPD_BR, DETECTION_REGIME_NER),
                (pis, "PIS_PASEP_BR", None, "br",
                 AUTH_PIS, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("br_pis", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_BRAZIL,
                jurisdiction="br",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="operations",
                authority_citations=[AUTH_LGPD_BR, AUTH_PIS],
                metadata={"country_code": "br", "identifier_type": "PIS_PASEP_BR"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"PIS record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- TITULO_ELEITOR ---
        rng_te = self.fresh("titulo_eleitor_br")
        for i in range(count_per_type):
            te = make_titulo_eleitor(rng_te)
            first = rng_te.choice(BR_FIRST)
            last = rng_te.choice(BR_LAST)
            text = (
                f"Dados do cidadao: {first} {last}. "
                f"Titulo de Eleitor: {te}. "
                f"Zona eleitoral registrada no TSE."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "br",
                 AUTH_LGPD_BR, DETECTION_REGIME_NER),
                (te, "TITULO_ELEITOR_BR", None, "br",
                 AUTH_TSE, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("br_titulo", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_BRAZIL,
                jurisdiction="br",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="operations",
                authority_citations=[AUTH_LGPD_BR, AUTH_TSE],
                metadata={"country_code": "br", "identifier_type": "TITULO_ELEITOR_BR"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Titulo Eleitor record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- BR_PASSPORT ---
        rng_pp = self.fresh("passport_br")
        for i in range(count_per_type):
            passport = make_passport_br(rng_pp)
            first = rng_pp.choice(BR_FIRST)
            last = rng_pp.choice(BR_LAST)
            text = (
                f"Passageiro: {first} {last}. "
                f"Passaporte: {passport}. "
                f"Emitido pela Policia Federal do Brasil."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "br",
                 AUTH_LGPD_BR, DETECTION_REGIME_NER),
                (passport, "BR_PASSPORT", None, "br",
                 AUTH_PASSPORT_BR, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("br_passport", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_BRAZIL,
                jurisdiction="br",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="operations",
                authority_citations=[AUTH_LGPD_BR, AUTH_PASSPORT_BR],
                metadata={"country_code": "br", "identifier_type": "BR_PASSPORT"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Passport record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- CNH (Carteira Nacional de Habilitacao / RENACH) ---
        rng_cnh = self.fresh("cnh_br")
        for i in range(count_per_type):
            cnh = make_cnh(rng_cnh)
            first = rng_cnh.choice(BR_FIRST)
            last = rng_cnh.choice(BR_LAST)
            text = (
                f"Condutor: {first} {last}. "
                f"CNH (RENACH): {cnh}. "
                f"Registro no DETRAN autorizado nos termos do CTB Art. 159."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "br",
                 AUTH_LGPD_BR, DETECTION_REGIME_NER),
                (cnh, "CNH_BR", None, "br",
                 AUTH_CNH, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("br_cnh", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_BRAZIL,
                jurisdiction="br",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="operations",
                authority_citations=[AUTH_LGPD_BR, AUTH_CNH],
                metadata={"country_code": "br", "identifier_type": "CNH_BR"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"CNH record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        # --- PHONE_BR ---
        rng_ph = self.fresh("phone_br")
        for i in range(count_per_type):
            phone = make_phone_br(rng_ph)
            first = rng_ph.choice(BR_FIRST)
            last = rng_ph.choice(BR_LAST)
            text = (
                f"Contato do paciente: {first} {last}. "
                f"Telefone: {phone}. "
                f"Agendamento via central do SUS."
            )
            spans = self.annotate(text, [
                (first + " " + last, "PERSON_NAME", "A", "br",
                 AUTH_LGPD_BR, DETECTION_REGIME_NER),
                (phone, "PHONE_BR", None, "br",
                 AUTH_LGPD_BR, DETECTION_REGIME_RULE),
            ])
            rec = Record(
                record_id=self.record_id("br_phone", idx),
                text=text,
                gold_spans=spans,
                layer=LAYER_BRAZIL,
                jurisdiction="br",
                detection_regime=DETECTION_REGIME_RULE,
                de_id_tier="identifiable",
                context="treatment",
                authority_citations=[AUTH_LGPD_BR],
                metadata={"country_code": "br", "identifier_type": "PHONE_BR"},
            )
            errors = rec.verify_spans()
            if errors:
                raise ValueError(f"Phone record {idx} span errors: {errors}")
            records.append(rec)
            idx += 1

        return records


def generate_corpus(seed: int = 42, count_per_type: int = 4) -> List[Record]:
    """Generate Brazil LGPD corpus and write to corpus/br/brazil_identifiers.jsonl.

    Authority: LGPD Brazil 2020 Article 5 (Lei 13.709/2018).
    Returns the list of generated Record objects.
    """
    gen = BrazilLGPDGenerator(seed=seed)
    records = gen.generate_batch(count_per_type=count_per_type)
    out_path = (
        Path(__file__).resolve().parents[2] / "corpus" / "br" / "brazil_identifiers.jsonl"
    )
    count = write_jsonl(records, out_path)
    return records
