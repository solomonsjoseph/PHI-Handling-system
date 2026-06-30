"""
phi_rag_bridge.py — Convert RePORTal PHI-scrubbed JSONL output → xlsx + schema JSON
so db_rag/data_loader.py can index it without any changes.

Usage:
    python phi_rag_bridge.py --study IndoVAP

What it does:
  1. Reads JSONL files from output/{STUDY}/llm_source/dataset_schema/files/
  2. Writes xlsx files to local_data/db_rag_source/filtered_excel_files/
  3. Writes schema JSON files to local_data/db_rag_source/reviewed_annotated_json_files/
     (column names, dtypes, null rates — no row values)

After running this, call: python -m db_rag.build_index
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
import config  # noqa: E402 — needs PROJECT_ROOT on sys.path first


def jsonl_to_df(path: Path) -> pd.DataFrame:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return pd.DataFrame(rows)


def build_schema_entry(col: str, series: pd.Series) -> dict:
    total = len(series)
    nulls = int(series.isna().sum())
    return {
        "column": col,
        "description": col,
        "dataType": str(series.dtype),
        "notes": f"null_rate={nulls}/{total}; distinct={int(series.nunique())}",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--study", required=True, help="Study name, e.g. IndoVAP")
    args = parser.parse_args()

    llm_source_files = (
        PROJECT_ROOT / "output" / args.study / "llm_source" / "dataset_schema" / "files"
    )
    if not llm_source_files.exists():
        sys.exit(f"No pipeline output found at {llm_source_files}\nRun the PHI pipeline first.")

    excel_out = PROJECT_ROOT / "local_data" / "db_rag_source" / "filtered_excel_files"
    schema_out = PROJECT_ROOT / "local_data" / "db_rag_source" / "reviewed_annotated_json_files"
    excel_out.mkdir(parents=True, exist_ok=True)
    schema_out.mkdir(parents=True, exist_ok=True)

    for jsonl_path in sorted(llm_source_files.glob("*.jsonl")):
        form_name = jsonl_path.stem          # e.g. "1A_Screening"
        prefix = form_name.split("_")[0]     # e.g. "1A"

        df = jsonl_to_df(jsonl_path)
        if df.empty:
            print(f"  skip {form_name} (empty)")
            continue

        # Write xlsx
        xlsx_path = excel_out / f"{prefix}_{form_name}.xlsx"
        df.to_excel(xlsx_path, index=False)

        # Write schema JSON — column metadata only, no row values
        schema = {
            "form_name": form_name,
            "columns": {col: build_schema_entry(col, df[col]) for col in df.columns},
        }
        schema_path = schema_out / f"{prefix} {form_name}.json"
        schema_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")

        print(f"  {form_name}: {len(df)} rows, {len(df.columns)} cols → {xlsx_path.name}")

    print(f"\nDone. Run: python -m db_rag.build_index")


if __name__ == "__main__":
    main()
