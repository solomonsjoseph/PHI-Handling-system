from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

CapabilityKind = Literal[
    "jurisdiction",
    "file_format",
    "benchmark",
    "validator",
    "security_control",
    "review_control",
    "privacy_attack",
]
CapabilityStatus = Literal[
    "planned",
    "implemented",
    "tested",
    "manifested",
    "externally_reviewed",
]

REGISTRY_PATH = Path(__file__).resolve().parent / "capability_registry.json"
STATUS_ORDER: dict[str, int] = {
    "planned": 0,
    "implemented": 1,
    "tested": 2,
    "manifested": 3,
    "externally_reviewed": 4,
}

_VALID_KINDS = set(get_args(CapabilityKind))
_VALID_STATUSES = set(get_args(CapabilityStatus))
_TUPLE_FIELDS = {"authority", "validators", "tests", "limitations"}
_REQUIRED_FIELDS = {"id", "kind", "status", "public_claim"}


@dataclass(frozen=True)
class Capability:
    id: str
    kind: CapabilityKind
    status: CapabilityStatus
    public_claim: str
    authority: tuple[str, ...] = ()
    jurisdiction: str = ""
    generator: str = ""
    output: str = ""
    validators: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


def _tuple_of_strings(value: Any, field_name: str, capability_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} for {capability_id} must be a list")
    if not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field_name} for {capability_id} must contain only strings")
    return tuple(value)


def load_capabilities(path: Path | None = None) -> list[Capability]:
    registry_path = path or REGISTRY_PATH
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("capability registry must be a list")

    capabilities: list[Capability] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"capability entry {index} must be an object")
        missing = sorted(_REQUIRED_FIELDS - item.keys())
        if missing:
            raise ValueError(f"capability entry {index} missing required keys: {', '.join(missing)}")

        capability_id = item["id"]
        if not isinstance(capability_id, str):
            raise ValueError(f"capability entry {index} id must be a string")
        if capability_id in seen:
            raise ValueError(f"duplicate capability id: {capability_id}")
        seen.add(capability_id)

        kind = item["kind"]
        if kind not in _VALID_KINDS:
            raise ValueError(f"unknown capability kind: {kind}")
        status = item["status"]
        if status not in _VALID_STATUSES:
            raise ValueError(f"unknown capability status: {status}")

        values: dict[str, Any] = dict(item)
        for field_name in _TUPLE_FIELDS:
            values[field_name] = _tuple_of_strings(values.get(field_name, []), field_name, capability_id)

        capabilities.append(Capability(**values))
    return capabilities


def registry_summary(capabilities: list[Capability]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for capability in capabilities:
        status_key = f"status:{capability.status}"
        kind_key = f"kind:{capability.kind}"
        summary[status_key] = summary.get(status_key, 0) + 1
        summary[kind_key] = summary.get(kind_key, 0) + 1
    return summary


def capability_rows(capabilities: list[Capability]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for capability in sorted(capabilities, key=lambda c: (c.kind, c.jurisdiction, c.id)):
        rows.append(
            {
                "id": capability.id,
                "kind": capability.kind,
                "status": capability.status,
                "jurisdiction": capability.jurisdiction,
                "public_claim": capability.public_claim,
                "output": capability.output,
                "limitations": "; ".join(capability.limitations),
            }
        )
    return rows


def require_status(capabilities: list[Capability], capability_id: str, minimum: CapabilityStatus) -> None:
    for capability in capabilities:
        if capability.id == capability_id:
            if STATUS_ORDER[capability.status] < STATUS_ORDER[minimum]:
                raise RuntimeError(
                    f"capability {capability_id} is {capability.status}, below required {minimum}"
                )
            return
    raise ValueError(f"unknown capability id: {capability_id}")


def _markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the PHI capability registry as Markdown.")
    parser.add_argument("--registry", type=Path, default=REGISTRY_PATH)
    args = parser.parse_args(argv)

    rows = capability_rows(load_capabilities(args.registry))
    columns = ["ID", "Kind", "Status", "Jurisdiction", "Claim", "Output", "Limitations"]
    keys = ["id", "kind", "status", "jurisdiction", "public_claim", "output", "limitations"]
    print("| " + " | ".join(columns) + " |")
    print("| " + " | ".join("---" for _ in columns) + " |")
    for row in rows:
        print("| " + " | ".join(_markdown_cell(row[key]) for key in keys) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
