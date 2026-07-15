"""PHI Rulebook engine — versioned offline cache over the jurisdiction rules (C2).

The jurisdiction classification rules live in
:mod:`scripts.security.phi_review` (``refresh_jurisdiction_rules`` resolves a
:class:`~scripts.security.phi_review.RuleBundle` from the pinned, official-source
rule pack, optionally probing the live official sources for a freshness hash).
This module wraps that primitive with three operational guarantees the raw
function does not provide:

* **Versioned offline cache.** Each resolved bundle's provenance (rules SHA-256,
  source mode, official sources, rule summaries) is persisted to a versioned JSON
  cache keyed by the sorted jurisdiction set, so repeated runs/processes share a
  durable record without re-probing the network.
* **Committed seed for airgapped first run.** A v1 seed cache is committed under
  ``config/_defaults/phi_rulebook/`` so the very first run in an environment with
  no network and no prior cache still has a known-good baseline to compare
  against. (The classification *rules themselves* are pinned in code, so the
  engine always functions offline — the seed/cache add provenance + drift
  detection, not the rules.)
* **Drift detection.** The freshly-built bundle's ``rules_sha256`` is compared to
  the cached/seed baseline; a mismatch is surfaced (``drift_detected``) so an
  operator/IRB reviewer is alerted when the effective rule set changed since the
  last recorded run — whether from a code change to the pinned rules or a live
  source update.

Value-free: the cache holds rule *metadata* (ids, jurisdictions, actions,
reasons, source URLs, SHA-256s) — never any study data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

import phi_engine.config.config as config
from phi_engine.security.phi_review import (
    _ACTION_RANK,
    Action,
    HeaderRule,
    RuleBundle,
    StudyPrivacyConfig,
    _canonical_bundle_payload,
    _sha256_json,
    classify_headers,
    refresh_jurisdiction_rules,
    validate_official_source_url,
)
from phi_engine.utils.logging_system import get_logger

__all__ = [
    "RULEBOOK_CACHE_VERSION",
    "RULEBOOK_LIVE_CACHE_VERSION",
    "RulebookResolution",
    "RulebookUnavailableError",
    "cache_filename",
    "default_cache_dir",
    "default_seed_dir",
    "read_cache_entry",
    "resolve_live_rulebook",
    "resolve_rulebook",
    "verify_extracted_rules",
    "write_cache_entry",
]

_logger = get_logger(__name__)

#: Bump when the cache JSON schema changes (invalidates older cache files).
RULEBOOK_CACHE_VERSION = 1

#: Live (AI-extracted) cache schema — persists compiled patterns + per-source
#: freshness hashes + extraction provenance, so a cache-hit can rebuild a
#: live-extracted rule's pattern (the v1 metadata cache cannot). Kept SEPARATE
#: from the pinned v1 cache/seed so the default (pinned) path is unchanged.
RULEBOOK_LIVE_CACHE_VERSION = 2


class RulebookUnavailableError(RuntimeError):
    """Raised when live rules are REQUIRED but none can be resolved (fail-closed)."""


# Cache-status values for RulebookResolution.cache_status.
_STATUS_LIVE = "live_fetch"  # bundle came from a successful live source probe
_STATUS_CACHE_HIT = "cache_hit"  # a matching prior cache entry existed
_STATUS_SEED = "seed"  # only the committed seed baseline existed
_STATUS_REBUILT = "rebuilt_no_cache"  # no cache and no seed — pinned rebuild only


@dataclass(frozen=True)
class RulebookResolution:
    """Result of resolving the active rulebook with cache/seed/drift accounting."""

    bundle: RuleBundle  # live bundle (with compiled patterns) for classification
    jurisdictions: tuple[str, ...]
    cache_status: str
    drift_detected: bool
    baseline_sha256: str | None  # what the current bundle was compared against
    cache_version: int = RULEBOOK_CACHE_VERSION
    # N7 live-path fields (always present; meaningful only on the AI-extract path).
    protection_weakened: bool = False  # an extracted rule would lower a pinned floor (flagged)
    offline_warning: str | None = None  # set when a live refresh was requested but unavailable


def _juris_key(jurisdictions: tuple[str, ...]) -> str:
    """Stable filename key from a jurisdiction set (sorted, upper-cased)."""
    return "_".join(sorted({j.upper() for j in jurisdictions}))


def cache_filename(jurisdictions: tuple[str, ...], *, version: int = RULEBOOK_CACHE_VERSION) -> str:
    """Return the versioned cache filename for a jurisdiction set."""
    return f"rulebook_v{version}_{_juris_key(jurisdictions)}.json"


def default_cache_dir() -> Path:
    """Per-study live cache location (audit zone, metadata only, no LLM access)."""
    return Path(config.STUDY_AUDIT_DIR) / "phi_rulebook"


def default_seed_dir() -> Path:
    """Committed v1 seed location (airgapped first-run baseline)."""
    return Path(config.CONFIG_DEFAULTS_DIR) / "phi_rulebook"


def _cache_payload(bundle: RuleBundle, jurisdictions: tuple[str, ...]) -> dict:
    """Build the value-free cache JSON for a resolved bundle."""
    payload: dict = bundle.to_json()
    payload["cache_version"] = RULEBOOK_CACHE_VERSION
    payload["jurisdictions"] = sorted({j.upper() for j in jurisdictions})
    return payload


def read_cache_entry(
    path: Path, *, jurisdictions: tuple[str, ...], version: int = RULEBOOK_CACHE_VERSION
) -> dict | None:
    """Read + validate a cache/seed entry; return None if absent/invalid/mismatched.

    Validates the cache version (``version`` — the pinned v1 by default, or the
    live v2 for the AI-extract path) and that the recorded jurisdictions match the
    requested set, so a stale-schema or wrong-jurisdiction file is ignored rather
    than trusted. Fail-soft: any I/O or parse error returns None.
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _logger.warning("rulebook cache unreadable: %s (ignored)", path)
        return None
    if not isinstance(data, dict):
        return None
    if data.get("cache_version") != version:
        _logger.warning(
            "rulebook cache version mismatch at %s (have %s, want %s); ignored",
            path,
            data.get("cache_version"),
            version,
        )
        return None
    want = sorted({j.upper() for j in jurisdictions})
    if data.get("jurisdictions") != want:
        return None
    if not isinstance(data.get("rules_sha256"), str):
        return None
    return data


def write_cache_entry(cache_dir: Path, bundle: RuleBundle, jurisdictions: tuple[str, ...]) -> Path:
    """Persist the bundle provenance to the versioned cache; return the path."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / cache_filename(jurisdictions)
    payload = _cache_payload(bundle, jurisdictions)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _resolve_pinned(
    privacy_config: StudyPrivacyConfig,
    *,
    allow_network: bool = False,
    cache_dir: Path | None = None,
    seed_dir: Path | None = None,
) -> RulebookResolution:
    """Resolve the PINNED rulebook, comparing to v1 cache/seed and updating it.

    Order of baseline preference for drift comparison: a matching live cache
    entry, else the committed seed. The classification rules always come from
    :func:`refresh_jurisdiction_rules` (pinned in code), so resolution never
    depends on the network or the cache being present. This is the default path
    and the fallback for the AI-extract path (:func:`resolve_live_rulebook`).
    """
    jurisdictions = tuple(privacy_config.jurisdictions)
    cache_dir = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    seed_dir = Path(seed_dir) if seed_dir is not None else default_seed_dir()

    bundle = refresh_jurisdiction_rules(privacy_config, allow_network=allow_network)

    cache_entry = read_cache_entry(
        cache_dir / cache_filename(jurisdictions), jurisdictions=jurisdictions
    )
    seed_entry = (
        None
        if cache_entry is not None
        else read_cache_entry(seed_dir / cache_filename(jurisdictions), jurisdictions=jurisdictions)
    )
    baseline = cache_entry or seed_entry
    baseline_sha = baseline.get("rules_sha256") if baseline else None
    drift = baseline_sha is not None and baseline_sha != bundle.rules_sha256

    if bundle.source_mode == "latest_official":
        status = _STATUS_LIVE
    elif cache_entry is not None:
        status = _STATUS_CACHE_HIT
    elif seed_entry is not None:
        status = _STATUS_SEED
    else:
        status = _STATUS_REBUILT

    if drift:
        _logger.warning(
            "PHI rulebook DRIFT for %s: effective rules_sha256 %s differs from "
            "recorded baseline %s. The classification rule set changed since the "
            "last recorded run — confirm the change is intended.",
            _juris_key(jurisdictions),
            bundle.rules_sha256[:12],
            (baseline_sha or "")[:12],
        )

    # Persist the current bundle provenance so the next run has a live baseline.
    try:
        write_cache_entry(cache_dir, bundle, jurisdictions)
    except OSError:  # pragma: no cover - cache write is best-effort
        _logger.warning("rulebook cache write failed at %s (non-fatal)", cache_dir)

    return RulebookResolution(
        bundle=bundle,
        jurisdictions=jurisdictions,
        cache_status=status,
        drift_detected=drift,
        baseline_sha256=baseline_sha,
    )


def resolve_rulebook(
    privacy_config: StudyPrivacyConfig,
    *,
    allow_network: bool = False,
    cache_dir: Path | None = None,
    seed_dir: Path | None = None,
) -> RulebookResolution:
    """Resolve the active rulebook (router).

    DEFAULT (and whenever AI extraction is off): the deterministic pinned path
    (:func:`_resolve_pinned`) — byte-identical to the prior behavior. When
    ``REPORTAL_RULEBOOK_AI_EXTRACT`` is set AND ``allow_network`` AND the study's
    ``rule_refresh`` is ``online_preferred``, route to the live AI-extract path
    (:func:`resolve_live_rulebook`), which fetches the latest official text,
    AI-extracts rules, verifies them, and merges them OVER the pinned floor.
    """
    if (
        config.RULEBOOK_AI_EXTRACT
        and allow_network
        and privacy_config.rule_refresh == "online_preferred"
    ):
        return resolve_live_rulebook(
            privacy_config, allow_network=allow_network, cache_dir=cache_dir, seed_dir=seed_dir
        )
    return _resolve_pinned(
        privacy_config, allow_network=allow_network, cache_dir=cache_dir, seed_dir=seed_dir
    )


# ── N7: live official-rule fetch + AI extraction (opt-in) ───────────────────
# The AI reads ONLY public regulation text (never PHI/row values — GR-1). Every
# extracted rule is deterministically verified, namespaced so it cannot shadow a
# pinned rule, and merged OVER the pinned floor (strictest-wins at classify time
# means an extracted rule can only ADD/strengthen protection, never weaken it).

# Benign clinical column names an extracted pattern must NOT match — forces the AI
# to emit WORD-ANCHORED patterns (an unanchored `date` would match `update_flag`;
# an unanchored `id` would match `covid_status`), so over-broad rules are rejected.
_BENIGN_RULE_PROBES: tuple[str, ...] = (
    "hemoglobin_result",
    "visit_number",
    "treatment_arm",
    "weight_kg",
    "culture_status",
    "update_flag",
    "enrollment_status",
    "covid_status",
    "valid_record",
)

# Header probes used to assert the merged bundle never LOWERS the pinned floor.
_PROTECTION_PROBES: tuple[str, ...] = (
    "participant_id",
    "visit_date",
    "email",
    "aadhaar",
    "full_name",
    "address",
    "dob",
)


def _pattern_is_safe(pattern: str) -> tuple[re.Pattern[str] | None, str | None]:
    """Compile + safety-check an extracted pattern (no catch-all, no benign match)."""
    try:
        compiled = re.compile(pattern, re.I)
    except re.error as exc:
        return None, f"regex does not compile: {exc}"
    core = pattern.strip().lstrip("^").rstrip("$").strip()
    if core in {"", ".*", ".+", "(.*)", "(.+)", ".*?", "(.*)?"}:
        return None, "regex is over-broad (catch-all)"
    if any(compiled.search(probe) for probe in _BENIGN_RULE_PROBES):
        return None, "regex over-matches a benign clinical header"
    return compiled, None


def verify_extracted_rules(
    raw_rules: object, *, jurisdiction: str, source_url: str
) -> tuple[HeaderRule, ...]:
    """Deterministically verify AI-extracted rules (no LLM, no I/O).

    Each rule must: be namespaced ``live_<juris>_*`` (cannot shadow a pinned id),
    use an action in the Action enum, cite an official source, and carry only
    word-anchored, compiling, non-over-broad patterns. A failing rule is dropped
    (logged value-free); the pinned floor still covers that case.
    """
    if not isinstance(raw_rules, list):
        return ()
    juris = jurisdiction.upper()
    allowed = {a.value for a in Action}
    verified: list[HeaderRule] = []
    for raw in raw_rules:
        if not isinstance(raw, dict):
            continue
        rid = str(raw.get("id", "")).strip()
        action = str(raw.get("action", "")).strip()
        reason = str(raw.get("reason", "")).strip()
        patterns = raw.get("patterns")
        if not rid.startswith(f"live_{juris.lower()}_"):
            _logger.warning("extracted rule rejected: id %r not namespaced", rid)
            continue
        if action not in allowed:
            _logger.warning("extracted rule %s rejected: action %r not allowed", rid, action)
            continue
        try:
            validate_official_source_url(source_url)
        except Exception:
            _logger.warning("extracted rule %s rejected: non-official source", rid)
            continue
        if not isinstance(patterns, list) or not patterns:
            continue
        compiled: list[re.Pattern[str]] = []
        ok = True
        for p in patterns:
            pat, err = _pattern_is_safe(str(p))
            if pat is None:
                _logger.warning("extracted rule %s pattern rejected: %s", rid, err)
                ok = False
                break
            compiled.append(pat)
        if not ok or not compiled:
            continue
        verified.append(
            HeaderRule(
                id=rid,
                jurisdiction=juris,
                action=Action(action),
                patterns=tuple(compiled),
                reason=reason or f"AI-extracted {action} rule",
            )
        )
    return tuple(verified)


def _merge_over_pinned(
    pinned: RuleBundle, extracted: tuple[HeaderRule, ...], *, sources: list[dict[str, str]]
) -> RuleBundle:
    """Union pinned (floor) + extracted rules; recompute the bundle hash.

    Additive only: classify uses strictest-wins, so an extracted rule can add a
    new covered header or strengthen an action, never lower a pinned decision.
    """
    merged_rules = pinned.rules + tuple(extracted)
    payload = _canonical_bundle_payload(tuple(sources), merged_rules)
    return RuleBundle(
        source_mode="latest_official_ai" if extracted else pinned.source_mode,
        rules_sha256=_sha256_json(payload),
        sources=tuple(sources),
        rules=merged_rules,
    )


def detect_protection_weakening(
    pinned: RuleBundle, merged: RuleBundle, privacy_config: StudyPrivacyConfig
) -> tuple[str, ...]:
    """Return probe headers the merged bundle protects LESS than pinned (should be ∅).

    An invariant check: with additive merge + strictest-wins this is always empty;
    a non-empty result signals a merge bug and is surfaced prominently.
    """
    pinned_cls = classify_headers(_PROTECTION_PROBES, privacy_config, pinned)
    merged_cls = classify_headers(_PROTECTION_PROBES, privacy_config, merged)
    weakened = [
        h
        for h in _PROTECTION_PROBES
        if _ACTION_RANK[merged_cls[h].action] < _ACTION_RANK[pinned_cls[h].action]
    ]
    return tuple(weakened)


def _live_cache_payload(
    bundle: RuleBundle, jurisdictions: tuple[str, ...], fetched_sha: dict[str, str]
) -> dict:
    """Value-free live (v2) cache JSON — persists patterns + source freshness."""
    return {
        "cache_version": RULEBOOK_LIVE_CACHE_VERSION,
        "jurisdictions": sorted({j.upper() for j in jurisdictions}),
        "rules_sha256": bundle.rules_sha256,
        "source_mode": bundle.source_mode,
        "sources": [dict(s) for s in bundle.sources],
        "rules": [
            {
                "id": r.id,
                "jurisdiction": r.jurisdiction,
                "action": r.action.value,
                "reason": r.reason,
                "patterns": [p.pattern for p in r.patterns],
            }
            for r in bundle.rules
        ],
        "fetched_source_hashes": dict(sorted(fetched_sha.items())),
    }


def _bundle_from_live_cache(entry: dict) -> RuleBundle:
    """Rebuild a RuleBundle (with compiled patterns) from a v2 live cache entry."""
    rules = tuple(
        HeaderRule(
            id=str(r["id"]),
            jurisdiction=str(r["jurisdiction"]),
            action=Action(str(r["action"])),
            patterns=tuple(re.compile(str(p), re.I) for p in r.get("patterns", [])),
            reason=str(r.get("reason", "")),
        )
        for r in entry.get("rules", [])
    )
    sources = tuple(dict(s) for s in entry.get("sources", []))
    return RuleBundle(
        source_mode=str(entry.get("source_mode", "latest_official_ai")),
        rules_sha256=str(entry["rules_sha256"]),
        sources=sources,
        rules=rules,
    )


def _write_live_cache(
    cache_dir: Path, bundle: RuleBundle, jurisdictions: tuple[str, ...], fetched_sha: dict[str, str]
) -> Path:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / cache_filename(jurisdictions, version=RULEBOOK_LIVE_CACHE_VERSION)
    payload = _live_cache_payload(bundle, jurisdictions, fetched_sha)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _live_rules_from_cache(entry: dict) -> tuple[HeaderRule, ...] | None:
    """Re-verify and rebuild ONLY the ``live_*`` rules held in a live cache entry.

    A live cache file lives in the audit zone. Its persisted copy of the pinned
    floor is never trusted (the floor is always re-derived from code); only the
    AI-extracted ``live_*`` rules are read back, and each is re-run through the
    same deterministic safety checks applied at extraction time (known action,
    at least one word-anchored non-catch-all, non-benign-matching pattern). Any
    malformed/over-broad/non-enum rule fails the whole entry (``None``); an entry
    with no live rules is also rejected (a live cache always carries at least one).
    No LLM and no network.
    """
    rules_raw = entry.get("rules")
    if not isinstance(rules_raw, list):
        return None
    allowed = {action.value for action in Action}
    live: list[HeaderRule] = []
    for rule in rules_raw:
        if not isinstance(rule, dict):
            return None
        rule_id = str(rule.get("id", ""))
        if not rule_id.startswith("live_"):
            continue  # pinned floor copy — re-derived fresh, cached copy ignored
        action = str(rule.get("action", ""))
        if action not in allowed:
            return None
        patterns = rule.get("patterns")
        if not isinstance(patterns, list) or not patterns:
            return None
        compiled: list[re.Pattern[str]] = []
        for pattern in patterns:
            safe, _err = _pattern_is_safe(str(pattern))
            if safe is None:
                return None
            compiled.append(safe)
        live.append(
            HeaderRule(
                id=rule_id,
                jurisdiction=str(rule.get("jurisdiction", "")),
                action=Action(action),
                patterns=tuple(compiled),
                reason=str(rule.get("reason", "")),
            )
        )
    return tuple(live) if live else None


def _verified_live_bundle(
    entry: dict, pinned: RuleBundle, privacy_config: StudyPrivacyConfig
) -> RuleBundle | None:
    """Return a trustworthy bundle from a live cache entry, else ``None``.

    Rebuilds by merging the re-verified ``live_*`` rules OVER a freshly re-derived
    pinned floor (so the floor never depends on cache bytes), then runs an explicit
    protection-weakening probe against that floor. Additive merge + strictest-wins
    already mean a live rule can only strengthen a decision; this probe is the
    keyless backstop that makes the no-weakening invariant enforced, not assumed.
    """
    live = _live_rules_from_cache(entry)
    if live is None:
        return None
    merged = _merge_over_pinned(
        pinned, live, sources=[dict(source) for source in pinned.sources]
    )
    if detect_protection_weakening(pinned, merged, privacy_config):
        return None
    return merged


def _reuse_live_cache(
    cache_dir: Path,
    jurisdictions: tuple[str, ...],
    privacy_config: StudyPrivacyConfig,
    *,
    warning: str | None,
) -> RulebookResolution | None:
    """Reuse the last verified v2 live extraction when a fresh fetch is impossible.

    Returns ``None`` when no v2 live cache exists OR the cached rules fail the
    integrity re-check (caller then falls back to the pinned floor or fail-closes
    under REQUIRE_LIVE). The cached rules were verified when first extracted; they
    are re-verified here before reuse and the ``offline_warning`` records that a
    freshness check could not be performed.
    """
    entry = read_cache_entry(
        cache_dir / cache_filename(jurisdictions, version=RULEBOOK_LIVE_CACHE_VERSION),
        jurisdictions=jurisdictions,
        version=RULEBOOK_LIVE_CACHE_VERSION,
    )
    if not entry:
        return None
    pinned = refresh_jurisdiction_rules(privacy_config, allow_network=False)
    bundle = _verified_live_bundle(entry, pinned, privacy_config)
    if bundle is None:
        return None
    return RulebookResolution(
        bundle=bundle,
        jurisdictions=jurisdictions,
        cache_status="cache_hit_live_offline",
        drift_detected=False,
        baseline_sha256=str(entry["rules_sha256"]),
        cache_version=RULEBOOK_LIVE_CACHE_VERSION,
        offline_warning=warning or "reused last saved live rules (no freshness check)",
    )


def resolve_live_rulebook(
    privacy_config: StudyPrivacyConfig,
    *,
    allow_network: bool = False,
    router: object = None,
    cache_dir: Path | None = None,
    seed_dir: Path | None = None,
) -> RulebookResolution:
    """Fetch latest official regs -> AI-extract -> verify -> merge over pinned floor.

    Extraction routes EXCLUSIVELY through the hardened official-source registry
    (:mod:`phi_engine.security.official_sources`) and the sealed
    :class:`~phi_engine.security.model_routing.ModelTaskRouter`: the model only
    ever sees registry-verified PUBLIC regulation text (never study data -- GR-1),
    and every extracted candidate is re-verified deterministically
    (:func:`verify_extracted_rules`) before it can merge OVER the pinned floor.

    Reuse-if-unchanged skips the model when every registered source hash matches
    the (re-verified) v2 cache. Offline / model-unavailable / unverifiable falls
    back to the pinned floor with an ``offline_warning``;
    ``REPORTAL_RULEBOOK_REQUIRE_LIVE`` makes that fail-closed
    (:class:`RulebookUnavailableError`). ``router`` is injectable so tests run
    with no network and no live model.
    """
    from phi_engine.security import official_sources
    from phi_engine.security.model_routing import ModelTaskRouter

    jurisdictions = tuple(privacy_config.jurisdictions)
    cache_dir = Path(cache_dir) if cache_dir is not None else default_cache_dir()
    seed_dir = Path(seed_dir) if seed_dir is not None else default_seed_dir()

    def _fallback(warning: str | None) -> RulebookResolution:
        reused = _reuse_live_cache(
            cache_dir, jurisdictions, privacy_config, warning=warning
        )
        if reused is not None:
            return reused
        if config.RULEBOOK_REQUIRE_LIVE:
            raise RulebookUnavailableError(
                f"live rulebook required but unavailable for {_juris_key(jurisdictions)}: {warning}"
            )
        pinned_res = _resolve_pinned(
            privacy_config, allow_network=False, cache_dir=cache_dir, seed_dir=seed_dir
        )
        return replace(pinned_res, offline_warning=warning) if warning else pinned_res

    if not allow_network:
        return _fallback("live rulebook refresh not permitted (allow_network=False)")

    pinned = refresh_jurisdiction_rules(privacy_config, allow_network=False)  # the floor
    sources = [dict(s) for s in pinned.sources]

    # Only pinned sources with an exact registered identity can be live-extracted
    # through the closed official-source + model stack; the registry is the single
    # source of truth. Unregistered pinned sources stay covered by the floor.
    registered: list[tuple[str, str, str]] = []
    for src in sources:
        registry_source_id = official_sources.registry_id_for_url(
            str(src["url"]), str(src["jurisdiction"])
        )
        if registry_source_id is not None:
            registered.append(
                (registry_source_id, str(src["jurisdiction"]), str(src["url"]))
            )
    if not registered:
        return _fallback("no official source maps to a registered live-extract id")

    # Freshness probe (no model): one source hash per registered id, fail-soft.
    fetched_sha: dict[str, str] = {}
    any_unreachable = False
    for registry_source_id, juris, _url in registered:
        try:
            fetched = official_sources.fetch_registered_source(registry_source_id, juris)
        except Exception:  # unreachable/invalid source -> pinned floor covers it
            any_unreachable = True
            continue
        fetched_sha[registry_source_id] = fetched.source_sha256

    # Reuse-if-unchanged: every fetched hash matches the recorded (re-verified) v2 cache.
    live_cache = read_cache_entry(
        cache_dir / cache_filename(jurisdictions, version=RULEBOOK_LIVE_CACHE_VERSION),
        jurisdictions=jurisdictions,
        version=RULEBOOK_LIVE_CACHE_VERSION,
    )
    if live_cache and fetched_sha:
        cached_hashes = live_cache.get("fetched_source_hashes", {})
        if cached_hashes == dict(sorted(fetched_sha.items())):
            bundle = _verified_live_bundle(live_cache, pinned, privacy_config)
            if bundle is not None:
                return RulebookResolution(
                    bundle=bundle,
                    jurisdictions=jurisdictions,
                    cache_status="cache_hit_live",
                    drift_detected=False,
                    baseline_sha256=str(live_cache["rules_sha256"]),
                    cache_version=RULEBOOK_LIVE_CACHE_VERSION,
                )
            # tampered/unverifiable cache -> treat as miss and re-extract below.

    # Use-latest-on-change: extract each reachable registered source via the router.
    task_router = router if router is not None else ModelTaskRouter()
    extracted: list[HeaderRule] = []
    for registry_source_id, juris, url in registered:
        if registry_source_id not in fetched_sha:
            continue  # unreachable this run; pinned floor covers it
        try:
            extraction = task_router.extract_official_rules(registry_source_id, juris)
        except Exception as exc:  # model/source failure -> pinned covers this source
            _logger.warning(
                "rulebook extraction failed for %s: %s",
                registry_source_id,
                type(exc).__name__,
            )
            continue
        raw_rules = [
            {
                "id": candidate.rule_id,
                "action": candidate.action.value,
                "patterns": [
                    rf"\b{re.escape(alias)}\b" for alias in candidate.literal_aliases
                ],
                "reason": candidate.citation,
            }
            for candidate in extraction.candidates
        ]
        extracted.extend(
            verify_extracted_rules(raw_rules, jurisdiction=juris, source_url=url)
        )

    if not extracted:
        return _fallback(
            "no verifiable rules extracted from official sources"
            if not any_unreachable
            else "official sources unreachable or unverifiable; using pinned rules"
        )

    merged = _merge_over_pinned(pinned, tuple(extracted), sources=sources)
    weakened = detect_protection_weakening(pinned, merged, privacy_config)
    if weakened:
        _logger.warning(
            "PHI rulebook PROTECTION-WEAKENING flagged for %s on probes %s "
            "(pinned floor still applies; review the extracted rules).",
            _juris_key(jurisdictions),
            list(weakened),
        )
    try:
        _write_live_cache(cache_dir, merged, jurisdictions, fetched_sha)
    except OSError:  # pragma: no cover - best-effort
        _logger.warning("live rulebook cache write failed at %s (non-fatal)", cache_dir)

    drift = merged.rules_sha256 != pinned.rules_sha256
    return RulebookResolution(
        bundle=merged,
        jurisdictions=jurisdictions,
        cache_status=_STATUS_LIVE,
        drift_detected=drift,
        baseline_sha256=pinned.rules_sha256,
        cache_version=RULEBOOK_LIVE_CACHE_VERSION,
        protection_weakened=bool(weakened),
        offline_warning=(
            "some official sources unreachable; pinned floor used for those"
            if any_unreachable
            else None
        ),
    )
