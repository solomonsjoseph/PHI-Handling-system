"""The D12 evidence-verification rule.

An ``EvidenceClaim`` reaches ``VERIFIED`` only when it has at least one
tool-backed ``EvidenceSource`` -- a source whose URL was actually returned
in a provider tool call's citations for the same response, never a
model-authored URL taken on faith -- and every one of the five
``VerificationDimension`` checks independently passes. Model confidence
never substitutes for this: it is telemetry, not evidence.

This module is deliberately independent of the gateway's live call shape:
callers correlate a source to its originating response themselves (by
``provider_request_id``) and hand this module the resulting yes/no
``tool_backed`` flag, so the same functions verify a fresh Statute/Praxis
reply and a replayed/migrated claim identically.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping, Sequence

from .records import EvidenceClaim, EvidenceSource, EvidenceState, EvidenceVerificationResult, VerificationDimension

VERIFICATION_DIMENSIONS: tuple[VerificationDimension, ...] = (
    "retrieval_authenticity",
    "source_authority",
    "claim_support",
    "freshness",
    "contradiction",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_tool_backed(source_url: str, cited_urls: Sequence[str] | set[str]) -> bool:
    """Whether ``source_url`` was actually returned by a provider tool call.

    ``cited_urls`` must already be scoped to the exact response the source
    claims (``EvidenceSource.provider_request_id``): callers build it as
    ``{url for event in gateway_result.tool_events for url in event.citations}``
    only when ``gateway_result.provider_request_id == source.provider_request_id``.
    A model-authored URL that merely happens to match a citation from a
    *different* response is not tool-derived.
    """
    return bool(source_url) and source_url in set(cited_urls)


def record_source(
    *,
    claim_id: str,
    url: str,
    tool_backed: bool,
    dimensions: Mapping[VerificationDimension, tuple[EvidenceState, str]] | None = None,
    **fields: object,
) -> EvidenceSource:
    """Build an ``EvidenceSource`` with its ``verifications`` seeded per D12.

    When ``tool_backed`` is ``False`` every dimension is forced
    ``UNVERIFIED`` regardless of ``dimensions``: a source with no retrieval
    evidence has nothing for ``source_authority``/``claim_support``/etc. to
    check in the first place. This is what keeps a model-authored citation
    absent from every tool event from ever being treated as tool-derived
    provenance (mirrors ``test_forged_citation_is_not_tool_derived``).
    """
    if not tool_backed:
        verifications = [
            EvidenceVerificationResult(dimension=dimension, state="UNVERIFIED", reason="not tool-derived")
            for dimension in VERIFICATION_DIMENSIONS
        ]
    else:
        supplied = dimensions or {}
        verifications = []
        for dimension in VERIFICATION_DIMENSIONS:
            state, reason = supplied.get(dimension, ("UNVERIFIED", "not evaluated"))
            verifications.append(EvidenceVerificationResult(dimension=dimension, state=state, reason=reason))
    return EvidenceSource(claim_id=claim_id, url=url, verifications=verifications, **fields)


def _fully_verified(source: EvidenceSource) -> bool:
    by_dimension = {result.dimension: result.state for result in source.verifications}
    return all(by_dimension.get(dimension) == "VERIFIED" for dimension in VERIFICATION_DIMENSIONS)


def _contradicted(source: EvidenceSource) -> bool:
    return any(
        result.dimension == "contradiction" and result.state == "CONTRADICTED" for result in source.verifications
    )


def evaluate_claim(claim: EvidenceClaim, sources: Sequence[EvidenceSource]) -> EvidenceClaim:
    """Apply the D12 rule and return the claim with its ``state`` updated.

    ``VERIFIED`` requires at least one of ``claim``'s sources to pass all
    five dimensions. Any source reporting a ``contradiction`` finding wins
    over a verified source (a contradicted claim must never read as safe).
    A claim with sources that are merely unverified stays ``UNVERIFIED``,
    never silently promoted by confidence, recency, or how many sources
    exist.
    """
    relevant = [source for source in sources if source.claim_id == claim.claim_id]
    if any(_contradicted(source) for source in relevant):
        state: EvidenceState = "CONTRADICTED"
    elif any(_fully_verified(source) for source in relevant):
        state = "VERIFIED"
    elif relevant:
        state = "UNVERIFIED"
    else:
        state = "UNKNOWN"
    return claim.model_copy(
        update={
            "state": state,
            "source_ids": [source.source_id for source in relevant],
            "contradicted_by": [source.source_id for source in relevant if _contradicted(source)],
            "updated_at": _now(),
        }
    )


def evaluate_evidence(
    *,
    claim: EvidenceClaim,
    candidate_urls: Sequence[str],
    cited_urls: Sequence[str] | set[str],
    dimensions: Mapping[VerificationDimension, tuple[EvidenceState, str]] | None = None,
    **source_fields: object,
) -> tuple[EvidenceClaim, list[EvidenceSource]]:
    """End-to-end D12 check: build sources for ``candidate_urls``, verify
    each against ``cited_urls``, then evaluate ``claim`` against them.

    ``dimensions`` supplies the non-retrieval verification outcomes (source
    authority, claim support, freshness, contradiction) for a tool-backed
    source; a source with no citation match never reaches that check at
    all -- it is forced ``UNVERIFIED`` on every dimension regardless.
    """
    sources = [
        record_source(
            claim_id=claim.claim_id,
            url=url,
            tool_backed=is_tool_backed(url, cited_urls),
            dimensions=dimensions,
            **source_fields,
        )
        for url in candidate_urls
    ]
    return evaluate_claim(claim, sources), sources
