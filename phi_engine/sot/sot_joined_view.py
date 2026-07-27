"""Build the SoT joined query view consumed by PHI header review.

The archived SoT producer referenced ``scripts.ai_assistant.sot_joined_view``,
but that module is absent from this repository. This standalone replacement is
intentionally tiny: it joins the PDF-derived policy metadata with the row-1
schema metadata and writes exactly the YAML shape that
``phi_engine.security.phi_review.load_sot_variable_signals`` reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "resolve_sot_joined_view_path",
    "build_joined_query_view",
    "write_joined_query_view_yaml",
]


def resolve_sot_joined_view_path(sot_root: Path, stem: str) -> Path:
    """Return ``{sot_root}/{stem}/joined/{stem}_joined_query_view.yaml``."""

    return Path(sot_root) / stem / "joined" / f"{stem}_joined_query_view.yaml"


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _load_json_mapping(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def build_joined_query_view(policy_path: Path, schema_path: Path) -> dict[str, Any]:
    """Join policy variables with matching schema column PHI actions.

    Policy entries are copied through as-is under ``pdf``. Schema columns without
    a policy entry are ignored; policy variables without a matching schema column
    remain present with an empty ``dataset`` block.
    """

    policy = _load_yaml_mapping(policy_path)
    schema = _load_json_mapping(schema_path)

    raw_variables = policy.get("variables")
    variables: dict[str, Any] = raw_variables if isinstance(raw_variables, dict) else {}

    schema_columns = schema.get("columns")
    column_by_name: dict[str, dict[str, Any]] = {}
    if isinstance(schema_columns, list):
        for column in schema_columns:
            if not isinstance(column, dict):
                continue
            name = column.get("name")
            if name is not None:
                column_by_name[str(name)] = column

    joined: dict[str, Any] = {"variables": {}}
    for name, policy_entry in variables.items():
        pdf_block = policy_entry if isinstance(policy_entry, dict) else {}
        dataset_block: dict[str, Any] = {}
        column = column_by_name.get(str(name))
        if isinstance(column, dict) and "phi_action" in column:
            dataset_block["phi_action"] = column["phi_action"]
        joined["variables"][str(name)] = {"pdf": pdf_block, "dataset": dataset_block}
    return joined


def write_joined_query_view_yaml(path: Path, view: dict[str, Any]) -> None:
    """Write a joined query view YAML file, creating parent directories."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(view, sort_keys=False, allow_unicode=False, width=120),
        encoding="utf-8",
    )
