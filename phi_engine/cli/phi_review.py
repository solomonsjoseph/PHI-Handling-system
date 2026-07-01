"""Interactive CLI for reviewing uncertain PHI classification decisions.

Human-in-the-loop triage for cases where the LLM confidence fell below
PHI_CONFIDENCE_THRESHOLD. Each uncertain case is read from the review queue
JSONL, presented to the reviewer, and the decision is written to the audit ledger.

Usage:
    phi-review
    phi-review --queue audit/human_review/llm_uncertain.jsonl
    phi-review --jurisdiction HIPAA
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_QUEUE = PROJECT_ROOT / "audit" / "human_review" / "llm_uncertain.jsonl"
DECISIONS_FILE = PROJECT_ROOT / "audit" / "human_review" / "decisions.jsonl"


def _load_queue(queue_path: Path) -> list[dict]:
    if not queue_path.is_file():
        return []
    items = []
    for line in queue_path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return items


def _write_decision(item: dict, decision: str, override_entity: str = "") -> None:
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "header": item.get("header"),
        "original_entity_type": item.get("entity_type"),
        "original_confidence": item.get("confidence"),
        "jurisdiction": item.get("jurisdiction", ""),
        "decision": decision,
        "override_entity_type": override_entity or None,
        "authority_citation": item.get("authority_citation", ""),
    }
    with DECISIONS_FILE.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def _clear_processed(queue_path: Path, processed_indices: set[int], all_items: list[dict]) -> None:
    """Rewrite queue without processed items."""
    remaining = [item for i, item in enumerate(all_items) if i not in processed_indices]
    if remaining:
        queue_path.write_text("\n".join(json.dumps(r) for r in remaining) + "\n")
    else:
        queue_path.unlink(missing_ok=True)


def run_review(queue_path: Path, jurisdiction_filter: str = "") -> None:
    items = _load_queue(queue_path)
    if not items:
        print("No items in review queue. Nothing to do.")
        return

    if jurisdiction_filter:
        items = [i for i in items if i.get("jurisdiction", "").upper() == jurisdiction_filter.upper()]
        if not items:
            print(f"No items for jurisdiction {jurisdiction_filter!r}.")
            return

    print(f"\nPHI Review Queue -- {len(items)} uncertain case(s)")
    print("=" * 60)

    processed: set[int] = set()
    all_items = _load_queue(queue_path)  # keep original list for rewrite

    for idx, item in enumerate(items):
        print(f"\n[{idx + 1}/{len(items)}] UNCERTAIN CASE")
        print(f"  Header:         {item.get('header', 'N/A')}")
        print(f"  Jurisdiction:   {item.get('jurisdiction', 'N/A')}")
        print(f"  LLM decision:   {item.get('entity_type', 'N/A')} (confidence {item.get('confidence', 0):.2f})")
        print(f"  Is PHI:         {item.get('is_phi', '?')}")
        print(f"  Authority:      {item.get('authority_citation', 'N/A')}")
        print(f"  Action:         {item.get('recommended_action', 'N/A')}")
        print(f"  Reasoning:      {item.get('reasoning', 'N/A')}")
        print()
        print("  [a]ccept   [r]eject   [o]verride entity_type   [s]kip   [q]uit")

        while True:
            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nInterrupted.")
                _clear_processed(queue_path, processed, all_items)
                return

            if choice == "a":
                _write_decision(item, "accepted")
                processed.add(all_items.index(item) if item in all_items else idx)
                print("  Accepted.")
                break
            elif choice == "r":
                _write_decision(item, "rejected")
                processed.add(all_items.index(item) if item in all_items else idx)
                print("  Rejected (not PHI).")
                break
            elif choice == "o":
                new_entity = input("  Override entity_type: ").strip().upper()
                if new_entity:
                    _write_decision(item, "overridden", override_entity=new_entity)
                    processed.add(all_items.index(item) if item in all_items else idx)
                    print(f"  Overridden to {new_entity!r}.")
                    break
                else:
                    print("  No entity type provided, try again.")
            elif choice == "s":
                print("  Skipped (stays in queue).")
                break
            elif choice == "q":
                print("  Quitting review session.")
                _clear_processed(queue_path, processed, all_items)
                return
            else:
                print("  Invalid choice. Use a/r/o/s/q.")

    _clear_processed(queue_path, processed, all_items)
    print(f"\nReview complete. {len(processed)} case(s) decided.")
    print(f"Decisions written to: {DECISIONS_FILE}")


def _main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Interactive PHI review queue CLI")
    parser.add_argument(
        "--queue",
        type=Path,
        default=DEFAULT_QUEUE,
        help=f"Review queue JSONL file (default: {DEFAULT_QUEUE})",
    )
    parser.add_argument(
        "--jurisdiction",
        default="",
        help="Filter by jurisdiction (e.g. HIPAA, DPDPA)",
    )
    args = parser.parse_args()
    run_review(args.queue, jurisdiction_filter=args.jurisdiction)


if __name__ == "__main__":
    _main()
