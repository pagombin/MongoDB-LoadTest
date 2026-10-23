"""A known password must not appear in logs, API responses, or exports after a full run."""

import asyncio
import subprocess
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ballast import logs
from ballast.config import load
from ballast.server import App

pytestmark = pytest.mark.integration
PASSWORD = "Gr3p-Me-If-You-Can-77"
H = {"X-Ballast-Request": "1"}


def ensure_user(uri: str) -> str:
    from pymongo import MongoClient

    client = MongoClient(uri)
    try:
        client.admin.command("dropUser", "ballast_secret_test")
    except Exception:
        pass
    client.admin.command("createUser", "ballast_secret_test", pwd=PASSWORD, roles=[{"role": "readWrite", "db": "ballast_secret_db"}])
    return uri.replace("mongodb://", f"mongodb://ballast_secret_test:{PASSWORD}@", 1) + "&authSource=admin"


async def test_password_absent_everywhere(tmp_path, uri):
    auth_uri = ensure_user(uri)
    loaded = load(None, ["tls.mode=off"], environ={}, home=tmp_path)
    logs.setup(loaded.log_dir, "debug", "json")
    app = App(loaded)
    client = TestClient(TestServer(app.build()))
    await client.start_server()
    try:
        target = await (await client.post("/api/targets", json={"name": "auth-rs", "uri": auth_uri, "tls": "off"}, headers=H)).json()
        bad = auth_uri.replace(PASSWORD, PASSWORD + "x")
        r = await client.post("/api/targets/test", json={"target": {"name": "bad", "uri": bad, "tls": "off"}}, headers=H)
        assert PASSWORD not in await r.text()
        profile = {
            "name": "secret",
            "load": {"model": "open", "rate": 200, "concurrency": 16},
            "schedule": {"warmup_seconds": 1, "steady_seconds": 8},
            "dataset": {"database": "ballast_secret_db", "collections": 1, "load": {"documents": 200}},
            "sampling": {"enabled": True, "interval_seconds": 2},
        }
        run = await (
            await client.post(
                "/api/runs",
                json={"name": "secret run", "target_id": target["id"], "profile": profile, "acknowledge_foreign": True},
                headers=H,
            )
        ).json()
        await asyncio.sleep(2)
        await client.post(f"/api/runs/{run['id']}/markers", json={"label": "mid"}, headers=H)
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = (await (await client.get(f"/api/runs/{run['id']}")).json())["status"]
            if status not in ("starting", "loading", "running", "stopping"):
                break
            await asyncio.sleep(1)
        assert status == "completed"
        texts = []
        for path in (
            f"/api/runs/{run['id']}",
            "/api/runs",
            "/api/targets",
            "/api/system",
            f"/api/runs/{run['id']}/samples",
            f"/api/runs/{run['id']}/export.csv",
            f"/api/runs/{run['id']}/export.json",
            f"/api/runs/{run['id']}/export.html",
        ):
            texts.append(await (await client.get(path)).text())
        bundle = await (await client.get("/api/system/logs.zip")).read()
        for text in texts:
            assert PASSWORD not in text
        assert PASSWORD.encode() not in bundle
    finally:
        await client.close()
    for path in Path(loaded.log_dir).glob("*"):
        assert PASSWORD not in path.read_text(errors="replace"), path
    grep = subprocess.run(["grep", "-rl", PASSWORD, str(loaded.log_dir)], capture_output=True, text=True)
    assert grep.stdout == ""
