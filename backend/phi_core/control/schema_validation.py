"""D69 "structured output validation" against ``policy.OUTPUT_SCHEMAS``.

Implemented and tested, deliberately NOT called from
``ProviderGateway.complete``. A live model reply is routinely prose-wrapped
or fenced JSON even when it satisfies its manifest's ``output_schema``
downstream (each agent's own parser already recovers that shape); enforcing
this check at the gateway boundary would turn ordinary model phrasing
variance into a hard deny for a call that already spent its budget, which is
an agent-behavior change out of this phase's scope. Callers that want this
check today invoke ``validate_response_schema`` explicitly at the point that
already parses/consumes ``GatewayResult.text``.
"""
from __future__ import annotations

import json

from .policy import OUTPUT_SCHEMAS

_SCHEMA_PY_TYPES: dict[str, type] = {"object": dict, "array": list}


class ResponseSchemaError(ValueError):
    """Raised when a completion's parsed JSON does not match its declared schema."""


def validate_response_schema(schema_name: str, text: str) -> object:
    """Parse ``text`` as JSON and check its top-level shape against
    ``OUTPUT_SCHEMAS[schema_name]``. Returns the parsed value on success."""
    schema = OUTPUT_SCHEMAS.get(schema_name)
    if schema is None:
        raise ResponseSchemaError(f"unknown response schema {schema_name!r}")
    expected_type = str(schema.get("type", ""))
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ResponseSchemaError(f"response is not valid JSON: {exc}") from exc
    py_type = _SCHEMA_PY_TYPES.get(expected_type)
    if py_type is not None and not isinstance(parsed, py_type):
        raise ResponseSchemaError(f"response top-level shape is {type(parsed).__name__}, expected {expected_type!r}")
    return parsed
