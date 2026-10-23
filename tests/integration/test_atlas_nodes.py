"""Atlas analytics and read-only nodes, on a local replica set tagged the way Atlas tags them.

Start it with scripts/dev-replset-atlas.sh (members ra1..ra5; ra4 is ANALYTICS, ra5 is READ_ONLY).
Override with BALLAST_TEST_ATLAS_URI and BALLAST_TEST_ATLAS_ANALYTICS / BALLAST_TEST_ATLAS_READ_ONLY
(container:port of the member to disrupt).
"""

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from ballast.engine.profile import Profile
from ballast.engine.runs import Hub, RunManager
from ballast.store import Store

pytestmark = pytest.mark.integration

URI = os.environ.get("BALLAST_TEST_ATLAS_URI", "mongodb://ra1:27041,ra2:27042,ra3:27043/?replicaSet=rs0")
ANALYTICS = os.environ.get("BALLAST_TEST_ATLAS_ANALYTICS", "ra4:27044")
READ_ONLY = os.environ.get("BALLAST_TEST_ATLAS_READ_ONLY", "ra5:27045")


@pytest.fixture(scope="module", autouse=True)
def atlas_like_cluster():
    try:
        with MongoClient(URI, serverSelectionTimeoutMS=3000) as client:
            tags = {(m.get("tags") or {}).get("nodeType") for m in client.admin.command("replSetGetConfig")["config"]["members"]}
    except PyMongoError as exc:
        pytest.skip(f"no Atlas-like replica set at {URI}: {exc}")
    if not {"ANALYTICS", "READ_ONLY"} <= tags:
        pytest.skip("the replica set has no ANALYTICS and READ_ONLY members; start scripts/dev-replset-atlas.sh")


def docker(container: str, *args: str) -> None:
    subprocess.run(["docker", *args, container], capture_output=True, timeout=120, check=True)


def mongosh(member: str, script: str) -> None:
    name, port = member.split(":")
    subprocess.run(["docker", "exec", name, "mongosh", "--port", port, "--quiet", "--eval", script], capture_output=True, timeout=120)


async def run_scenario(tmp_path: Path, profile: dict, disrupt, label: str):
    store = Store(tmp_path / "data" / "ballast.db")
    target = store.save_target({"name": "atlas-like", "uri": URI, "tls": "off"})
    manager = RunManager(store, Hub(), tmp_path / "data")
    db = f"ballast_atlas_{int(time.time())}"
    profile["dataset"] = {"database": db, "collections": 1, "load": {"documents": 3000}}
    row = await manager.start(name=label, target=target, profile=Profile.model_validate(profile), acknowledge_foreign=True)
    while manager.active is None or manager.active.status != "running":
        await asyncio.sleep(0.2)
    await asyncio.sleep(12)
    manager.add_marker(row["id"], f"{label} started")
    await asyncio.to_thread(disrupt)
    await asyncio.sleep(10)
    manager.add_marker(row["id"], f"{label} finished")
    while manager.active is not None:
        await asyncio.sleep(0.5)
    with MongoClient(URI) as client:
        client.drop_database(db)
    return store, store.get_run(row["id"])


@pytest.mark.parametrize("selection_timeout_ms", [2000, None])
async def test_analytics_node_restart_affects_only_analytics_reads(tmp_path, selection_timeout_ms):
    """Aggregations go to the analytics node, point reads and writes to the primary. Restarting the
    analytics node (what a rolling resize of the analytics tier does to it) must show as a gap and failures
    on the analytics route only."""
    profile = {
        "name": "analytics",
        "operations": {"find": 40, "update": 30, "analytics": 30},
        "keys": {"analytics_docs": 500},
        "read_routing": {"analytics": {"mode": "secondary", "tags": [{"nodeType": "ANALYTICS"}]}},
        "load": {"model": "open", "rate": 200, "concurrency": 128},
        "schedule": {"warmup_seconds": 3, "steady_seconds": 45},
    }
    if selection_timeout_ms:
        profile["driver"] = {"server_selection_timeout_ms": selection_timeout_ms}
    store, run = await run_scenario(tmp_path, profile, lambda: docker(ANALYTICS.split(":")[0], "restart"), "analytics resize")
    try:
        assert run["status"] == "completed", run["error"]
        summary = run["analysis"]["summary"]
        members = summary["members"]
        assert members[ANALYTICS]["reads"] > 0, "analytics queries are answered by the analytics node"
        assert members.get(READ_ONLY, {}).get("reads", 0) == 0, "nothing is routed to the read-only node"
        primary = run["snapshot"]["server"]["primary"]
        assert members[primary]["writes"] > 0 and members[primary]["reads"] > 0
        w = next(i for i in run["analysis"]["impacts"] if i["source"] == "markers")
        assert w["route_gaps"]["ANALYTICS"] and w["route_gaps"]["ANALYTICS"]["ms"] >= 1500, w["route_gaps"]
        if selection_timeout_ms:
            assert w["operations"]["analytics"]["err"] > 0, (
                "with a short timeout, analytics reads fail while no analytics node is available"
            )
        else:
            analytics_max = summary["operations"]["analytics"]["response_time"]["max_us"]
            assert w["operations"]["analytics"]["err"] == 0, "with the default 30 s timeout, analytics reads wait for the node"
            assert analytics_max >= 2_000_000, f"waiting shows as analytics latency (max {analytics_max} us)"
        assert w["operations"]["find"]["err"] == 0 and w["operations"]["update"]["err"] == 0, w["operations"]
        assert w["longest_write_gap"] is None or w["longest_write_gap"]["ms"] < 1000
        assert not run["analysis"]["client_bound"]["client_bound"], "waiting on an unavailable node is not a client limit"
        find_p99 = summary["operations"]["find"]["response_time"]["p99_us"]
        assert find_p99 < 1_000_000, f"primary reads must not queue behind waiting analytics queries (p99 {find_p99} us)"
        assert any(e["kind"] == "server_state" and e.get("node_type") == "ANALYTICS" for e in w["member_events"]), w["member_events"]
        if selection_timeout_ms:
            assert w["members"][ANALYTICS]["during"]["reads_per_s"] < w["members"][ANALYTICS]["before"]["reads_per_s"]
        routes = run["snapshot"]["preflight"]["routes"]
        assert any(r["name"] == "ANALYTICS" and r["members"] == [ANALYTICS] for r in routes), routes
    finally:
        store.close()


async def test_read_only_node_lag_is_measured_without_privileges(tmp_path):
    """Reads go to the read-only node; blocking its replication shows as lag in the member snapshots."""
    profile = {
        "name": "read-only",
        "operations": {"find": 70, "update": 30},
        "driver": {"read_preference": "secondary", "read_preference_tags": [{"nodeType": "READ_ONLY"}]},
        "load": {"model": "open", "rate": 200, "concurrency": 64},
        "schedule": {"warmup_seconds": 3, "steady_seconds": 40},
        "sampling": {"enabled": False},
    }
    store, run = await run_scenario(
        tmp_path, profile, lambda: mongosh(READ_ONLY, "db.fsyncLock(); sleep(8000); db.fsyncUnlock()"), "read-only lag"
    )
    try:
        assert run["status"] == "completed", run["error"]
        members = run["analysis"]["summary"]["members"]
        assert members[READ_ONLY]["reads"] > 0
        lags = [
            m["lag_seconds"]
            for s in store.samples(run["id"], "members")
            for m in s["data"]["members"]
            if m["address"] == READ_ONLY and m["lag_seconds"] is not None
        ]
        assert lags and max(lags) >= 3, f"lag of the read-only node while its replication was blocked: {lags}"
        types = {m["address"]: m["node_type"] for s in store.samples(run["id"], "members")[:1] for m in s["data"]["members"]}
        assert types[READ_ONLY] == "READ_ONLY" and types[ANALYTICS] == "ANALYTICS"
    finally:
        store.close()
