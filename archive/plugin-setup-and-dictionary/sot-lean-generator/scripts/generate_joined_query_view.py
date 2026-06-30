#!/usr/bin/env python3
"""Generate a derived LLM-readable joined query view.

This script does not modify the policy Source Truth YAML or dataset schema JSON.
It combines them into a query-only view keyed once by variable id.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts.ai_assistant.sot_joined_view import (
    build_joined_query_view,
    write_joined_query_view_yaml,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        "--lean",
        dest="policy",
        required=True,
        type=Path,
        help="Policy Source Truth YAML.",
    )
    parser.add_argument("--schema", required=True, type=Path, help="Per-form dataset schema JSON.")
    parser.add_argument(
        "--out", required=True, type=Path, help="Output YAML path for the joined query view."
    )
    args = parser.parse_args()

    view = build_joined_query_view(args.policy, args.schema)
    write_joined_query_view_yaml(args.out, view)
    print(f"joined query view written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
