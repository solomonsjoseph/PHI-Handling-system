"""4.20: response security headers and per-route rate limits.

No TestClient/Mongo double here, matching the rest of the suite (see
test_chatgpt_auth.py docstring) -- the rate limiter and the header
middleware are exercised directly, and route wiring is checked via
``app.router.routes`` the same way test_production_readiness.py checks
``resolve_principal`` wiring.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def _closure_map(fn):
    """Map a closure function's free variable names to their bound values."""
    names = fn.__code__.co_freevars
    cells = fn.__closure__ or ()
    return dict(zip(names, (c.cell_contents for c in cells)))


def _fake_request(*, token: str | None = None, cookie: str | None = None, host: str = "203.0.113.1"):
    headers = {}
    if token is not None:
        headers["x-api-token"] = token
    cookies = {}
    if cookie is not None:
        cookies["phi_session"] = cookie
    return SimpleNamespace(
        headers=SimpleNamespace(get=lambda k, default=None: headers.get(k, default)),
        cookies=cookies,
        client=SimpleNamespace(host=host),
    )


@pytest.fixture(autouse=True)
def _clear_buckets():
    import server as srv
    srv._RATE_BUCKETS.clear()
    yield
    srv._RATE_BUCKETS.clear()


def test_every_named_route_carries_its_rate_limit_bucket_and_window():
    """4.20: the six credential/expensive routes are each rate limited with
    the exact bucket/limit/window from the plan."""
    import server as srv

    expected = {
        ("POST", "/api/auth/session"): ("auth_session", 10, 900),
        ("POST", "/api/sessions/{sid}/intake"): ("session_intake", 20, 3600),
        ("POST", "/api/corpus/study/research"): ("corpus_research", 5, 3600),
        ("POST", "/api/corpus/study/generate"): ("corpus_generate", 20, 3600),
        ("POST", "/api/corpus/study/run"): ("corpus_run", 20, 3600),
        ("POST", "/api/settings/warmup"): ("settings_warmup", 5, 3600),
    }
    routes_by_path_method = {}
    for r in srv.app.router.routes:
        methods = getattr(r, "methods", None) or set()
        for m in methods:
            routes_by_path_method[(m, r.path)] = r

    for (method, path), (bucket, limit, window) in expected.items():
        route = routes_by_path_method.get((method, path))
        assert route is not None, f"{method} {path} not registered"
        limiter_deps = [
            d.call for d in route.dependant.dependencies
            if getattr(d.call, "__name__", "") == "_dep" and "bucket" in d.call.__code__.co_freevars
        ]
        assert limiter_deps, f"{method} {path} has no rate_limited dependency"
        found = [_closure_map(fn) for fn in limiter_deps]
        assert any(
            m.get("bucket") == bucket and m.get("limit") == limit and m.get("window_seconds") == window
            for m in found
        ), f"{method} {path} rate limit mismatch: {found}"


def test_rate_limiter_blocks_after_limit_with_retry_after_header():
    import server as srv
    from fastapi import HTTPException

    dep = srv.rate_limited("unit_test_bucket", 3, 60)
    req = _fake_request(host="198.51.100.7")

    async def _run():
        for _ in range(3):
            await dep(req)
        with pytest.raises(HTTPException) as exc_info:
            await dep(req)
        return exc_info.value

    exc = asyncio.run(_run())
    assert exc.status_code == 429
    assert "Retry-After" in exc.headers
    assert int(exc.headers["Retry-After"]) >= 1


def test_rate_limiter_keys_by_principal_not_shared_address_when_resolved(monkeypatch):
    """Two different principals behind the same client address get
    independent buckets; the same principal from two addresses shares one."""
    import server as srv

    monkeypatch.setenv("API_TOKENS", "alice:tok-a,bob:tok-b")
    dep = srv.rate_limited("unit_test_bucket_2", 1, 60)

    alice_req = _fake_request(token="tok-a", host="10.0.0.1")
    bob_req = _fake_request(token="tok-b", host="10.0.0.1")  # same address as alice

    asyncio.run(dep(alice_req))
    # bob is a different principal behind the same address -- must not be blocked
    asyncio.run(dep(bob_req))

    alice_req_2 = _fake_request(token="tok-a", host="10.0.0.99")  # alice, different address
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(dep(alice_req_2))


def test_security_headers_present_on_every_response():
    import server as srv

    async def _call_next(request):
        from starlette.responses import Response
        return Response("ok")

    req = _fake_request()
    response = asyncio.run(srv._security_headers(req, _call_next))
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_hsts_only_set_outside_dev(monkeypatch):
    import importlib
    import server as srv

    async def _call_next(request):
        from starlette.responses import Response
        return Response("ok")

    req = _fake_request()

    monkeypatch.setattr(srv, "_HSTS", True)
    response = asyncio.run(srv._security_headers(req, _call_next))
    assert "Strict-Transport-Security" in response.headers

    monkeypatch.setattr(srv, "_HSTS", False)
    response = asyncio.run(srv._security_headers(req, _call_next))
    assert "Strict-Transport-Security" not in response.headers
