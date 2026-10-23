"""Failover under load against a real three-member replica set.

Run with: BALLAST_TEST_URI=... pytest -m integration tests/integration
"""

import asyncio
import time
from pathlib import Path

import pytest
from pymongo import MongoClient

from ballast.engine.profile import Profile
from ballast.engine.runs import Hub, RunManager
from ballast.store import Store

from .conftest import MEMBERS, stepdown

pytestmark = pytest.mark.integration


async def run_with_failover(tmp_path: Path, uri: str, processes: int):
    store = Store(tmp_path / "data" / "ballast.db")
    target = store.save_target({"name": "rs", "uri": uri, "tls": "off"})
    manager = RunManager(store, Hub(), tmp_path / "data")
    db = f"ballast_it_{processes}_{int(time.time())}"
    profile = Profile.model_validate(
        {
            "name": "failover",
            "seed": 7,
            "operations": {"find": 40, "update": 40, "insert": 20},
            "driver": {"retry_writes": False, "retry_reads": False, "server_selection_timeout_ms": 15000},
            "load": {"model": "open", "rate": 400, "concurrency": 64, "processes": processes},
            "schedule": {"warmup_seconds": 3, "steady_seconds": 45},
            "dataset": {"database": db, "collections": 2, "load": {"documents": 2000}},
        }
    )
    row = await manager.start(name="failover", target=target, profile=profile, acknowledge_foreign=True)
    run_id = row["id"]
    while manager.active is None or manager.active.status != "running":
        await asyncio.sleep(0.2)
    await asyncio.sleep(12)
    manager.add_marker(run_id, "stepdown started", source="api")
    stepped = stepdown()
    await asyncio.sleep(12)
    manager.add_marker(run_id, "stepdown finished", source="api")
    while manager.active is not None:
        await asyncio.sleep(0.5)
    with MongoClient(uri) as client:
        client.drop_database(db)
    run = store.get_run(run_id)
    events = store.events(run_id)
    store.close()
    return events, run, stepped


@pytest.mark.parametrize("processes", [1, 2])
async def test_failover_is_recorded_classified_and_measured(tmp_path, uri, processes):
    events, run, stepped = await run_with_failover(tmp_path, uri, processes)
    assert run["status"] == "completed", run["error"]
    kinds = {e["kind"] for e in events}
    assert "primary_lost" in kinds or "primary_changed" in kinds
    assert any(e["kind"] == "server_state" and e["data"]["address"].startswith(stepped) for e in events)
    analysis = run["analysis"]
    windows = [i for i in analysis["impacts"] if i["source"] == "markers"]
    assert len(windows) == 1
    w = windows[0]
    assert w["failed_ops"] > 0, "with retryable writes off, a stepdown must fail some operations"
    assert w["failed_by_class"], "failures are classified"
    assert set(w["failed_by_class"]) & {
        "NotWritablePrimary",
        "PrimarySteppedDown",
        "InterruptedDueToReplStateChange",
        "NetworkError",
        "ServerSelectionTimeout",
        "NotPrimaryNoSecondaryOk",
        "NotPrimaryOrSecondary",
    }
    assert w["first_failure_ms"] and w["last_failure_ms"] and w["first_failure_ms"] <= w["last_failure_ms"]
    if w["longest_write_gap"] is not None:
        assert w["longest_write_gap"]["ms"] >= 100
    assert any(p["kind"] in ("primary_changed", "primary_lost") for p in w["primary_changes"])
    assert w["recovery_seconds"] is not None, "throughput and p99 return to baseline before the run ends"
    assert analysis["summary"]["measured_seconds"] >= 40


def primary_container():
    import subprocess

    for name, port in MEMBERS:
        out = subprocess.run(
            ["docker", "exec", name, "mongosh", "--port", str(port), "--quiet", "--eval", "db.hello().isWritablePrimary"],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if out == "true":
            return name, port
    raise RuntimeError("no primary")


async def test_write_stall_measures_gap_latency_and_recovery(tmp_path, uri):
    """fsyncLock blocks writes on the primary for 3 s; reads keep working."""
    import subprocess

    store = Store(tmp_path / "data" / "ballast.db")
    target = store.save_target({"name": "rs", "uri": uri, "tls": "off"})
    manager = RunManager(store, Hub(), tmp_path / "data")
    db = f"ballast_stall_{int(time.time())}"
    profile = Profile.model_validate(
        {
            "name": "stall",
            "operations": {"find": 50, "update": 50},
            "load": {"model": "open", "rate": 300, "concurrency": 256},
            "schedule": {"warmup_seconds": 3, "steady_seconds": 40},
            "dataset": {"database": db, "collections": 1, "load": {"documents": 2000}},
        }
    )
    row = await manager.start(name="stall", target=target, profile=profile, acknowledge_foreign=True)
    while manager.active is None or manager.active.status != "running":
        await asyncio.sleep(0.2)
    await asyncio.sleep(15)
    name, port = primary_container()
    manager.add_marker(row["id"], "stall started")
    await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", name, "mongosh", "--port", str(port), "--quiet", "--eval", "db.fsyncLock(); sleep(3000); db.fsyncUnlock()"],
        capture_output=True,
        timeout=60,
    )
    manager.add_marker(row["id"], "stall finished")
    while manager.active is not None:
        await asyncio.sleep(0.5)
    with MongoClient(uri) as client:
        client.drop_database(db)
    run = store.get_run(row["id"])
    assert run["status"] == "completed", run["error"]
    w = [i for i in run["analysis"]["impacts"] if i["source"] == "markers"][0]
    assert w["longest_write_gap"]["ms"] >= 2500
    stall_seconds = [r for r in store.seconds(row["id"]) if w["start_ms"] // 1000 <= r.t <= w["end_ms"] // 1000]
    assert sum(r.pool_wait.total for r in stall_seconds) > 0, "connection checkout waits are measured"
    store.close()
    assert w["during"]["max_us"] >= 2_500_000, "open model reports the stall as latency"
    assert w["before"]["p99_us"] < 500_000
    assert w["recovered"] is True and w["recovery_seconds"] is not None
