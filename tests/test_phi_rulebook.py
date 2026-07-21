"""Live rulebook resolution path (opt-in AI-extract) — hardened-stack routing.

The default pinned path is exercised through the pipeline integration suites;
this module covers the live path (:func:`resolve_live_rulebook`) in isolation
with an injected router and a stubbed official-source fetch, so it needs no
network and no local model. It asserts the continuity contract the cleanup
established: extraction routes through the official-source registry + router
(never a bare ``scripts.*`` import), verified candidates merge OVER the pinned
floor, unchanged sources reuse the cache without a model call, offline/optional
degrades to pinned, REQUIRE_LIVE fails closed, and a tampered cache is rejected.

NOTE: other hermetic tests in this suite evict ``phi_engine.*`` modules
from ``sys.modules`` between hermetic workspaces to avoid stale import-time
configuration and class identity leaking across studies. Capturing
modules/classes at COLLECTION time would leave stale objects that no longer
match the freshly re-imported modules ``resolve_live_rulebook`` uses
internally — so every module and class is imported FRESH inside the
``env`` fixture and monkeypatched there.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

_SHA = "a" * 64


@pytest.fixture
def env():
    import phi_engine.config.config as config
    from phi_engine.security import official_sources, phi_rulebook
    from phi_engine.security.model_routing import (
        ExtractedRuleCandidate,
        OfficialRuleExtraction,
    )
    from phi_engine.security.phi_review import Action, StudyPrivacyConfig

    return SimpleNamespace(
        config=config,
        official_sources=official_sources,
        rb=phi_rulebook,
        ExtractedRuleCandidate=ExtractedRuleCandidate,
        OfficialRuleExtraction=OfficialRuleExtraction,
        Action=Action,
        StudyPrivacyConfig=StudyPrivacyConfig,
    )


def _privacy(env, tmp_path):
    return env.StudyPrivacyConfig(
        study_dir=tmp_path,
        jurisdictions=("USA",),
        rule_refresh="online_preferred",
        conflict_policy="strictest_wins",
        max_synthetic_attempts=5,
        approval_mode="hybrid",
        parallelism_mode="auto",
    )


def _make_router(env, alias="participant_ref"):
    class _FakeRouter:
        def __init__(self):
            self.calls = []

        def extract_official_rules(self, registry_source_id, jurisdiction):
            self.calls.append((registry_source_id, jurisdiction))
            return env.OfficialRuleExtraction(
                registry_source_id=registry_source_id,
                jurisdiction=jurisdiction,
                source_sha256=_SHA,
                candidates=(
                    env.ExtractedRuleCandidate(
                        rule_id=f"live_{jurisdiction.lower()}_{registry_source_id}",
                        action=env.Action.DROP,
                        literal_aliases=(alias,),
                        citation="test citation",
                        jurisdiction=jurisdiction,
                    ),
                ),
            )

    return _FakeRouter()


class _ExplodingRouter:
    def extract_official_rules(self, *_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("router must not be called on a cache reuse")


def _stub_fetch(env, monkeypatch, *, sha=_SHA, fail=False):
    def fetch(registry_source_id, jurisdiction):
        if fail:
            raise env.official_sources.RegisteredSourceError("source_unavailable")
        return SimpleNamespace(source_sha256=sha)

    monkeypatch.setattr(env.official_sources, "fetch_registered_source", fetch)


def test_live_path_merges_verified_router_rules(env, tmp_path, monkeypatch):
    monkeypatch.setattr(env.config, "RULEBOOK_REQUIRE_LIVE", False)
    _stub_fetch(env, monkeypatch)
    router = _make_router(env)

    res = env.rb.resolve_live_rulebook(
        _privacy(env, tmp_path),
        allow_network=True,
        router=router,
        cache_dir=tmp_path / "cache",
        seed_dir=tmp_path / "seed",
    )

    assert res.bundle.source_mode == "latest_official_ai"
    assert {r.id for r in res.bundle.rules if r.id.startswith("live_usa_")}
    assert router.calls
    cache_file = (tmp_path / "cache") / env.rb.cache_filename(
        ("USA",), version=env.rb.RULEBOOK_LIVE_CACHE_VERSION
    )
    assert cache_file.is_file()


def test_live_reuse_if_unchanged_skips_model(env, tmp_path, monkeypatch):
    monkeypatch.setattr(env.config, "RULEBOOK_REQUIRE_LIVE", False)
    _stub_fetch(env, monkeypatch)
    cache_dir = tmp_path / "cache"
    seed_dir = tmp_path / "seed"

    first = env.rb.resolve_live_rulebook(
        _privacy(env, tmp_path), allow_network=True, router=_make_router(env),
        cache_dir=cache_dir, seed_dir=seed_dir,
    )
    assert first.cache_status == "live_fetch"

    second = env.rb.resolve_live_rulebook(
        _privacy(env, tmp_path), allow_network=True, router=_ExplodingRouter(),
        cache_dir=cache_dir, seed_dir=seed_dir,
    )
    assert second.cache_status == "cache_hit_live"
    assert second.bundle.rules_sha256 == first.bundle.rules_sha256


def test_live_offline_falls_back_to_pinned_when_optional(env, tmp_path, monkeypatch):
    monkeypatch.setattr(env.config, "RULEBOOK_REQUIRE_LIVE", False)
    _stub_fetch(env, monkeypatch, fail=True)

    res = env.rb.resolve_live_rulebook(
        _privacy(env, tmp_path), allow_network=True, router=_make_router(env),
        cache_dir=tmp_path / "cache", seed_dir=tmp_path / "seed",
    )

    assert res.bundle.source_mode == "pinned"
    assert res.offline_warning


def test_require_live_fails_closed_when_unavailable(env, tmp_path, monkeypatch):
    monkeypatch.setattr(env.config, "RULEBOOK_REQUIRE_LIVE", True)
    _stub_fetch(env, monkeypatch, fail=True)

    with pytest.raises(env.rb.RulebookUnavailableError):
        env.rb.resolve_live_rulebook(
            _privacy(env, tmp_path), allow_network=True, router=_make_router(env),
            cache_dir=tmp_path / "cache", seed_dir=tmp_path / "seed",
        )


def test_tampered_cache_is_rejected_and_reextracted(env, tmp_path, monkeypatch):
    monkeypatch.setattr(env.config, "RULEBOOK_REQUIRE_LIVE", False)
    _stub_fetch(env, monkeypatch)
    cache_dir = tmp_path / "cache"
    seed_dir = tmp_path / "seed"

    env.rb.resolve_live_rulebook(
        _privacy(env, tmp_path), allow_network=True, router=_make_router(env),
        cache_dir=cache_dir, seed_dir=seed_dir,
    )
    cache_file = cache_dir / env.rb.cache_filename(
        ("USA",), version=env.rb.RULEBOOK_LIVE_CACHE_VERSION
    )
    entry = json.loads(cache_file.read_text())
    entry["rules"][0]["patterns"] = [".*"]  # inject an over-broad catch-all
    cache_file.write_text(json.dumps(entry))

    res = env.rb.resolve_live_rulebook(
        _privacy(env, tmp_path), allow_network=True, router=_make_router(env),
        cache_dir=cache_dir, seed_dir=seed_dir,
    )
    assert all(
        pat.pattern != ".*" for rule in res.bundle.rules for pat in rule.patterns
    )
