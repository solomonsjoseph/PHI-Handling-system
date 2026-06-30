"""Shared JSONL line-parsing helper for RePORT AI Portal.

This module provides the canonical line-level JSONL parser used across the
pipeline: trio bundle and downstream processing.  Centralizing this eliminates
duplicate copies and provides a single place to fix JSON-parsing edge cases.
"""

# SHARED UTILITY (Note 20 Gap C / Note 19): stays in scripts/, imported
# read-only by plugin skills; never moved into a skill, never imports from
# plugins/.

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

__all__ = ["JSONLParseError", "load_json_object_line"]


class JSONLParseError(ValueError):
    """Raised when a JSONL line is malformed or not a JSON object."""


def load_json_object_line(line: str, *, source_path: Path, line_number: int) -> dict[str, Any]:
    """Parse one JSONL line and require a top-level JSON object.

    Args:
        line: Raw line text (should be stripped by caller).
        source_path: File the line came from (for error context).
        line_number: 1-based line number (for error context).

    Returns:
        Parsed JSON object as a dict.

    Raises:
        JSONLParseError: If the line is not valid JSON or not a dict.
    """
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise JSONLParseError(
            f"Malformed JSON in {source_path} at line {line_number}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise JSONLParseError(f"Non-object JSON record in {source_path} at line {line_number}")
    return cast(dict[str, Any], payload)
