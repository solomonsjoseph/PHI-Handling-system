"""Pydantic model for the artifact every ``generate_with_retry`` run of a
Schema-extraction source emits. Kept deliberately tiny: one JSON file per
dataset, with no prose, no free-text, and no nested columns beyond the
fixed fields below.

The schema is a hard contract between the LLM-generated extraction module
and the calling agent (Schema). The module must name every column of the
source dataset exactly once and report the integer cardinality and null
cardinality observed for it. Any deviation -- an extra key, a missing key,
or an unexpected type -- fails the run rather than attempting coercion,
since the immediate downstream consumer is the header safety gate which
must confirm every classifier-visible name exists as a real column.

Disclosive-statistics rule: ``distinct_count`` and ``row_count`` carry
information whose disclosure through an aggregate count must be bounded. A
cardinality of exactly one (constant) or of exactly the row count (unique)
is reported as the categorical flag (``CardKind.constant`` /
``CardKind.unique``) instead of as a raw integer. The identical-pair
threshold is the only threshold checked: anything in between stays a raw
count because it cannot be inferred from the public structure alone.

``known_safe_values`` is a closed set the caller may supply for values the
``known_safe_values`` mechanism in `generate_with_retry` already trusts:
values that appeared verbatim in approved read-ops inputs (legitimate
categorical recodes like ``{"M", "F"}``). Anything absent from this set
must not appear verbatim as a string literal in the generated module,
which is the whole point of the assertion the retry layer enforces.
"""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InferredType(StrEnum):
    string = "string"
    integer = "integer"
    float = "float"
    date = "date"
    boolean = "boolean"
    categorical = "categorical"
    unknown = "unknown"


class CardKind(StrEnum):
    constant = "constant"
    unique = "unique"
    normal = "normal"


class ExtractedColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    position: int = Field(ge=0)
    distinct_count: int = Field(ge=0)
    null_count: int = Field(ge=0)
    inferred_type: InferredType


class ExtractedSchema(BaseModel):
    """The complete, strict schema of one extraction artifact.

    ``extra="forbid"`` rejects any field not listed here; the position
    validator enforces strict 0..n-1 ordering with no gaps; and the
    names-distinct validator rejects duplicate column names, which the
    pre-existing duplicate-header intake check would otherwise silently
    collapse downstream (csv.DictReader's last-wins behavior)."""

    model_config = ConfigDict(extra="forbid")

    columns: list[ExtractedColumn]
    row_count: int = Field(ge=0)

    @model_validator(mode="before")
    @classmethod
    def _square_names_match_positions(cls, data: object) -> object:
        """Each column's declared position equals its index in the list.

        Enforced at the type level by sorting the list by position and
        then requiring positional equality: any permutation mismatch is a
        hard schema violation rather than a silent reorder.
        """
        if not isinstance(data, dict):
            return data
        cols = data.get("columns")
        if cols is None:
            return data
        if not isinstance(cols, list):
            return data
        for idx, col in enumerate(cols):
            if isinstance(col, dict):
                if col.get("position") != idx:
                    raise ValueError(
                        f"column at index {idx} declares position {col.get('position')}, "
                        f"positions must be 0..n-1 in order, with no gaps or duplicates"
                    )
        return data

    @model_validator(mode="after")
    def _names_are_distinct(self) -> "ExtractedSchema":
        """Every column name appears at most once. A duplicated name in
        the emitted artifact means the generated code faithfully reported
        a dataset whose header row itself contains the same string twice
        -- exactly the shape the duplicate-header fail-closed rule (server
        `_handle_pipeline_run`'s existing check) is meant to reject before
        any downstream code sees it, since pandas and csv.DictReader both
        collapse resolutions to one column amidst ambiguous writes."""
        names = [c.name for c in self.columns]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate column name(s) in extracted schema: {dupes!r}")
        return self


def card_kind(distinct_count: int, row_count: int) -> CardKind:
    """Bucket a (distinct, row) pair into the coarse reportable shape:
    exactly-1 is ``constant``, exactly-equal-to-rows is ``unique``,
    everything else is ``normal`` (only the bucket reaches a report)."""
    if distinct_count == 1:
        return CardKind.constant
    if distinct_count == row_count:
        return CardKind.unique
    return CardKind.normal
