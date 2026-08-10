# PHI-handled study bundle

**Session**: `d36e4593276a41e799be6738e496c1e3`
**Jurisdiction**: US
**Regulation**: HIPAA Privacy Rule 45 CFR 164.514(b)(2)(i) — Safe Harbor.

## Contents

```
safe_to_share/
├── datasets/             # PHI-handled datasets (CSV/XLSX)
├── forms/                # PHI-handled forms (redacted text)
├── dictionary/           # PHI-handled data dictionary / mapping
├── attestation.json      # machine-readable attestation
├── attestation.txt       # human-readable attestation
└── README.md             # this file
```

If the bundle contains a `publication/` folder, it holds paper-ready tables,
figures and drafts that describe how these outputs were produced and how
this system compares to established de-identification tools.

## How this bundle was produced

1. **Intake v3** validates the study package structure (datasets / forms /
   dictionary components).
2. Twelve agents classify each dataset column using **only the column
   header** plus the data dictionary and any accompanying forms — never a
   row value.
3. The **Executor** applies the chosen action per column (drop /
   pseudonymize / cap_age_90 / year_only / zip3_truncate / scrub_text).
4. The **Publish Guard** runs a deterministic PHI scan (SSN, phone,
   email, full DOB, restricted ZIP3, age > 89) on every output; downloads
   are refused unless the guard clears.
5. Every changed decision carries a reviewer id + comment + timestamp.

## Verification

Re-hash any file with SHA-256 and match against `attestation.json`.

```
$ sha256sum datasets/*.csv
```

The value in `attestation.files["<relative_path>"]` must match.
