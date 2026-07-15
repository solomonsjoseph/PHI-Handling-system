from __future__ import annotations

import hashlib
import http.client
from dataclasses import dataclass
from urllib.parse import urlsplit

_MAX_SOURCE_BYTES = 4_000_000


class RegisteredSourceError(RuntimeError):
    """Controlled failure at the closed official-source boundary."""


@dataclass(frozen=True)
class FetchedRegisteredSource:
    registry_source_id: str
    jurisdiction: str
    source_sha256: str
    body: bytes
    citation: str


@dataclass(frozen=True)
class _RegisteredSource:
    registry_source_id: str
    jurisdiction: str
    url: str
    citation: str


_REGISTRY: dict[tuple[str, str], _RegisteredSource] = {
    ("usa_hipaa_164_514", "USA"): _RegisteredSource(
        registry_source_id="usa_hipaa_164_514",
        jurisdiction="USA",
        url="https://www.ecfr.gov/current/title-45/subtitle-A/subchapter-C/part-164/subpart-E/section-164.514",
        citation="45 CFR 164.514",
    ),
    ("usa_hhs_deidentification", "USA"): _RegisteredSource(
        registry_source_id="usa_hhs_deidentification",
        jurisdiction="USA",
        url="https://www.hhs.gov/hipaa/for-professionals/privacy/special-topics/de-identification/index.html",
        citation="HHS HIPAA De-identification Guidance",
    ),
    ("india_dpdp_2023", "INDIA"): _RegisteredSource(
        registry_source_id="india_dpdp_2023",
        jurisdiction="INDIA",
        url="https://www.indiacode.nic.in/indiacode/handle/123456789/22037",
        citation="Digital Personal Data Protection Act, 2023",
    ),
}


def is_registered_source(registry_source_id: str, jurisdiction: str) -> bool:
    return (registry_source_id, jurisdiction) in _REGISTRY


def fetch_registered_source(
    registry_source_id: str,
    jurisdiction: str,
) -> FetchedRegisteredSource:
    """Fetch one exact source selected solely by its closed registry identity."""
    source = _REGISTRY.get((registry_source_id, jurisdiction))
    if source is None:
        raise RegisteredSourceError("source_not_registered")
    body = _fetch_registered_url(source)
    return FetchedRegisteredSource(
        registry_source_id=source.registry_source_id,
        jurisdiction=source.jurisdiction,
        source_sha256=hashlib.sha256(body).hexdigest(),
        body=body,
        citation=source.citation,
    )


def _fetch_registered_url(source: _RegisteredSource) -> bytes:
    parsed = urlsplit(source.url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RegisteredSourceError("source_registry_invalid")
    port = parsed.port or 443
    path = parsed.path or "/"
    connection = http.client.HTTPSConnection(parsed.hostname, port, timeout=10)
    response = None
    try:
        connection.request(
            "GET",
            path,
            headers={"User-Agent": "RePORT-AI-Portal/official-rule-fetch"},
        )
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise RegisteredSourceError("source_redirect_rejected")
        if response.status != 200:
            raise RegisteredSourceError("source_http_error")
        body = response.read(_MAX_SOURCE_BYTES + 1)
        if len(body) > _MAX_SOURCE_BYTES:
            raise RegisteredSourceError("source_too_large")
        return body
    except RegisteredSourceError:
        raise
    except Exception:
        raise RegisteredSourceError("source_unavailable") from None
    finally:
        if response is not None:
            response.close()
        connection.close()
