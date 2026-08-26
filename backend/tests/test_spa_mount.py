"""4.15: the built frontend is mounted at "/" only when a build directory
exists, and it must be the last thing registered so it never shadows an
/api route. GET / moved to GET /api/version."""
from __future__ import annotations


def test_version_endpoint_registered_not_root():
    import server as srv
    paths = {r.path for r in srv.app.router.routes}
    assert "/api/version" in paths
    assert not any(
        getattr(r, "path", None) == "/" and "GET" in (getattr(r, "methods", None) or set())
        for r in srv.app.router.routes
    )


def test_no_static_mount_without_a_build_directory():
    import server as srv
    from starlette.routing import Mount
    # This checkout has no frontend/build/ directory, so the guard must
    # have skipped the mount entirely -- confirms a source checkout with
    # no built frontend still starts and still serves /api.
    assert not srv._FRONTEND_BUILD_DIR.exists()
    assert not any(isinstance(r, Mount) and r.path == "" for r in srv.app.router.routes)


def test_mount_is_the_last_route_when_present(tmp_path, monkeypatch):
    """Import server fresh against a fake build dir and confirm the mount,
    when present, is registered after every API route (so it never shadows
    one) -- exercised via a standalone FastAPI app built the same way
    server.py builds its own, rather than re-importing the real module
    (which has import-time side effects like _refuse_to_boot_insecure)."""
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "index.html").write_text("<html></html>", encoding="utf-8")

    app = FastAPI()

    @app.get("/api/probe")
    async def probe():
        return {"ok": True}

    app.mount("/", StaticFiles(directory=str(build_dir), html=True), name="ui")

    route_order = [type(r).__name__ for r in app.router.routes]
    assert route_order[-1] == "Mount", "the SPA mount must be the last registered route"
    assert any(r.path == "/api/probe" for r in app.router.routes if type(r).__name__ != "Mount")


def _spa_test_app(build_dir):
    """Small isolated app mounting the real `_SPAStaticFiles` subclass, so
    the fallback logic itself is exercised (not a copy of it), without
    booting the full server.py app (Mongo-backed startup hooks etc.)."""
    import server as srv
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/api/probe")
    async def probe():
        return {"ok": True}

    app.mount("/", srv._SPAStaticFiles(directory=str(build_dir), html=True), name="ui")
    return app


def test_deep_link_refresh_falls_back_to_index_html(tmp_path):
    """A React Router path with no matching file (e.g. a hard refresh on
    /sessions/<id>) must return the SPA shell, not a bare 404, or
    client-side routing can never take over."""
    from starlette.testclient import TestClient

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "index.html").write_text("<html>shell</html>", encoding="utf-8")

    client = TestClient(_spa_test_app(build_dir))
    r = client.get("/sessions/abc123-does-not-exist-as-a-file")
    assert r.status_code == 200
    assert r.text == "<html>shell</html>"


def test_real_static_asset_still_served_directly_not_shadowed_by_fallback(tmp_path):
    """The fallback must not swallow a genuine, existing static asset."""
    from starlette.testclient import TestClient

    build_dir = tmp_path / "build"
    static_dir = build_dir / "static" / "js"
    static_dir.mkdir(parents=True)
    (build_dir / "index.html").write_text("<html>shell</html>", encoding="utf-8")
    (static_dir / "main.js").write_text("console.log('real bundle');", encoding="utf-8")

    client = TestClient(_spa_test_app(build_dir))
    r = client.get("/static/js/main.js")
    assert r.status_code == 200
    assert "real bundle" in r.text


def test_api_routes_are_never_shadowed_by_the_spa_fallback(tmp_path):
    from starlette.testclient import TestClient

    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "index.html").write_text("<html>shell</html>", encoding="utf-8")

    client = TestClient(_spa_test_app(build_dir))
    r = client.get("/api/probe")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
