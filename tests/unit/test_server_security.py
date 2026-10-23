import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ballast.config import load
from ballast.server import App

PASSWORD = "Zx9-unique-Pa55word"
H = {"X-Ballast-Request": "1"}


async def make_client(tmp_path, flags=()):
    loaded = load(None, ["tls.mode=off", *flags], environ={}, home=tmp_path)
    app = App(loaded)
    client = TestClient(TestServer(app.build()))
    await client.start_server()
    return client


@pytest.fixture
async def client(tmp_path):
    c = await make_client(tmp_path)
    yield c
    await c.close()


async def test_security_headers_on_pages_and_api(client):
    for path in ("/", "/api/about"):
        r = await client.get(path)
        assert r.status == 200
        csp = r.headers["Content-Security-Policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "script-src 'self'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["Referrer-Policy"] == "same-origin"
        assert "aiohttp" not in r.headers.get("Server", "")
    assert "Strict-Transport-Security" not in r.headers


async def test_state_changing_requests_need_custom_header(client):
    r = await client.post("/api/profiles", json={"name": "x"})
    assert r.status == 403
    assert (await r.json())["error"]["title"] == "Missing request header"


async def test_cross_origin_requests_are_blocked(client):
    r = await client.post("/api/profiles", json={"name": "x"}, headers={**H, "Origin": "https://evil.example"})
    assert r.status == 403
    r = await client.post("/api/profiles", json={"name": "x"}, headers={**H, "Sec-Fetch-Site": "cross-site"})
    assert r.status == 403
    host = f"http://{client.host}:{client.port}"
    r = await client.post("/api/profiles", json={"name": "same origin ok"}, headers={**H, "Origin": host})
    assert r.status == 201


async def test_schema_validation_and_unknown_fields(client):
    r = await client.post("/api/profiles", json={"name": "x", "load": {"rate": -5}}, headers=H)
    assert r.status == 422
    body = await r.json()
    assert any(p["field"] == "load.rate" for p in body["error"]["problems"])
    r = await client.post("/api/targets", json={"name": "t", "uri": "mongodb://h/", "extra": 1}, headers=H)
    assert r.status == 422
    r = await client.post("/api/targets", data="not json", headers={**H, "Content-Type": "application/json"})
    assert r.status == 400
    r = await client.post("/api/targets", data="x", headers={**H, "Content-Type": "text/plain"})
    assert r.status == 415


async def test_body_size_limit(tmp_path):
    c = await make_client(tmp_path, ["server.max_body_kb=16"])
    try:
        r = await c.post(
            "/api/profiles", data=json.dumps({"name": "x", "description": "a" * 40000}), headers={**H, "Content-Type": "application/json"}
        )
        assert r.status == 413
    finally:
        await c.close()


async def test_password_never_returned_after_saving(client):
    uri = f"mongodb://bench:{PASSWORD}@h1:27017,h2:27017/?replicaSet=rs0"
    r = await client.post("/api/targets", json={"name": "prod", "uri": uri, "tls": "off"}, headers=H)
    assert r.status == 201
    created = await r.json()
    for path in ("/api/targets", f"/api/targets/{created['id']}", "/api/system"):
        text = await (await client.get(path)).text()
        assert PASSWORD not in text
    assert "****" in created["uri"]
    r = await client.put(f"/api/targets/{created['id']}", json={"name": "prod", "uri": created["uri"], "tls": "off"}, headers=H)
    assert PASSWORD not in await r.text()


async def test_access_gate(tmp_path):
    c = await make_client(tmp_path, ["server.access_token=gate-token-123"])
    try:
        r = await c.get("/api/runs")
        assert r.status == 401
        r = await c.get("/", allow_redirects=False)
        assert r.status == 303 and r.headers["Location"] == "/auth"
        assert (await c.get("/api/health")).status == 200
        r = await c.get("/api/runs", headers={"Authorization": "Bearer gate-token-123"})
        assert r.status == 200
        r = await c.post("/auth", data={"token": "wrong"}, allow_redirects=False)
        assert r.status == 401
        r = await c.post("/auth", data={"token": "gate-token-123"}, headers={"Origin": "https://evil.example"}, allow_redirects=False)
        assert r.status == 403
        r = await c.post(
            "/auth", data={"token": "gate-token-123"}, headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"}, allow_redirects=False
        )
        assert r.status == 403
        r = await c.post(
            "/auth", data={"token": "wrong"}, headers={"Origin": "null", "Sec-Fetch-Site": "same-origin"}, allow_redirects=False
        )
        assert r.status == 401
        r = await c.post("/auth", data={"token": "gate-token-123"}, allow_redirects=False)
        assert r.status == 303
        cookie = r.headers["Set-Cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
        assert "gate-token-123" not in cookie
        assert (await c.get("/api/runs")).status == 200
    finally:
        await c.close()


async def test_secure_cookie_and_hsts_behind_tls_proxy(tmp_path):
    c = await make_client(tmp_path, ["server.access_token=tok-abc-123", "server.trust_proxy_headers=true"])
    try:
        r = await c.post("/auth", data={"token": "tok-abc-123"}, headers={"X-Forwarded-Proto": "https"}, allow_redirects=False)
        assert "Secure" in r.headers["Set-Cookie"]
    finally:
        await c.close()


async def test_profiles_crud_duplicate_export_import(client):
    r = await client.post("/api/profiles", json={"name": "base", "load": {"model": "closed", "concurrency": 8}}, headers=H)
    p = await r.json()
    dup = await (await client.post(f"/api/profiles/{p['id']}/duplicate", headers=H)).json()
    assert dup["name"] == "base copy"
    exported = await (await client.get(f"/api/profiles/{p['id']}/export")).json()
    assert exported["format"] == "ballast-profile"
    imported = await (await client.post("/api/profiles/import", json=exported, headers=H)).json()
    assert imported["name"] == "base 2" and imported["body"]["load"]["concurrency"] == 8
    assert (await client.delete(f"/api/profiles/{p['id']}", headers=H)).status == 200


async def test_marker_needs_active_run(client):
    r = await client.post("/api/runs/active/markers", json={"label": "x"}, headers=H)
    assert r.status in (404, 409)
