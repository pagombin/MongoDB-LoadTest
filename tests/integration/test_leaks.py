"""Repeated runs must not leak generator processes, threads, file descriptors or asyncio tasks."""

import asyncio
import gc
import multiprocessing
import os
import threading
import time
import warnings
from pathlib import Path

import pytest
from pymongo import MongoClient

from ballast.engine.profile import Profile
from ballast.engine.runs import Hub, RunManager
from ballast.store import Store

pytestmark = pytest.mark.integration


def open_fds() -> int:
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


async def one_run(manager: RunManager, target: dict, db: str) -> None:
    profile = Profile.model_validate({
        "name": "leak", "load": {"model": "open", "rate": 300, "concurrency": 32, "processes": 2},
        "schedule": {"warmup_seconds": 1, "steady_seconds": 6}, "dataset": {"database": db, "load": {"documents": 300}},
        "sampling": {"enabled": True, "interval_seconds": 1},
    })
    await manager.start(name="leak", target=target, profile=profile, acknowledge_foreign=True)
    while manager.active is not None:
        await asyncio.sleep(0.2)


async def test_repeated_runs_do_not_leak(tmp_path: Path, uri):
    store = Store(tmp_path / "ballast.db")
    target = store.save_target({"name": "rs", "uri": uri, "tls": "off"})
    manager = RunManager(store, Hub(), tmp_path)
    db = f"ballast_leak_{int(time.time())}"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        await one_run(manager, target, db)
        await asyncio.sleep(2)
        gc.collect()
        base_threads, base_fds, base_tasks = threading.active_count(), open_fds(), len(asyncio.all_tasks())
        base_named = len([t for t in threading.enumerate() if not t.name.startswith("asyncio_")])
        for _ in range(3):
            await one_run(manager, target, db)
        await asyncio.sleep(2)
        gc.collect()
    with MongoClient(uri) as client:
        client.drop_database(db)
    await asyncio.sleep(1)

    def own_threads():
        return [t.name for t in threading.enumerate() if not t.name.startswith("asyncio_")]

    print("threads after:", [t.name for t in threading.enumerate()])
    assert multiprocessing.active_children() == []
    assert len(own_threads()) <= base_named, own_threads()
    assert threading.active_count() <= base_threads + os.cpu_count() + 4, "default executor stays bounded"
    assert open_fds() <= base_fds + 4, (base_fds, open_fds())
    assert len(asyncio.all_tasks()) <= base_tasks
    leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not leaks, [str(w.message) for w in leaks]
    runs = store.list_runs()
    assert len(runs) == 4 and all(r["status"] == "completed" for r in runs)
