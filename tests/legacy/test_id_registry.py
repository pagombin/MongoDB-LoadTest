from __future__ import annotations

import random
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from mongo_load_tester.engine.id_registry import DocumentIdRegistry


def test_add_and_get_random_ids() -> None:
    registry = DocumentIdRegistry(max_ids_per_namespace=100)
    rng = random.Random(42)

    registry.add_id("db1", "coll1", "id1")
    registry.add_id("db1", "coll1", "id2")
    registry.add_id("db1", "coll1", "id3")

    seen: set[str] = set()
    for _ in range(100):
        got = registry.get_random_id("db1", "coll1", rng)
        assert got is not None
        assert got in ("id1", "id2", "id3")
        seen.add(got)

    assert seen == {"id1", "id2", "id3"}


def test_remove_id_successful() -> None:
    registry = DocumentIdRegistry(max_ids_per_namespace=100)
    rng = random.Random(7)

    registry.add_id("db1", "coll1", "id1")
    registry.add_id("db1", "coll1", "id2")
    registry.remove_id("db1", "coll1", "id1")

    seen: set[str] = set()
    for _ in range(50):
        got = registry.get_random_id("db1", "coll1", rng)
        assert got is not None
        seen.add(got)

    assert seen == {"id2"}


def test_remove_id_non_existent_is_noop() -> None:
    registry = DocumentIdRegistry(max_ids_per_namespace=100)
    rng = random.Random(11)

    registry.add_id("db1", "coll1", "id1")
    registry.remove_id("db1", "coll1", "id_nonexistent")
    registry.remove_id("db1", "coll1", "id1")

    assert registry.get_random_id("db1", "coll1", rng) is None


def test_remove_id_from_empty_namespace_is_noop() -> None:
    registry = DocumentIdRegistry(max_ids_per_namespace=100)
    rng = random.Random(13)

    registry.remove_id("db1", "coll1", "id1")

    assert registry.get_random_id("db1", "coll1", rng) is None


def test_max_ids_per_namespace_eviction() -> None:
    registry = DocumentIdRegistry(max_ids_per_namespace=5)
    rng = random.Random(17)

    for i in range(6):
        registry.add_id("db1", "coll1", f"id{i}")

    seen: set[str] = set()
    for _ in range(100):
        got = registry.get_random_id("db1", "coll1", rng)
        assert got is not None
        seen.add(got)

    assert "id0" not in seen
    assert seen == {"id1", "id2", "id3", "id4", "id5"}


def test_multiple_namespaces_isolation() -> None:
    registry = DocumentIdRegistry(max_ids_per_namespace=100)
    rng = random.Random(19)

    registry.add_id("db1", "coll1", "a1")
    registry.add_id("db1", "coll2", "b1")
    registry.add_id("db2", "coll1", "c1")

    assert registry.get_random_id("db1", "coll1", rng) == "a1"
    assert registry.get_random_id("db1", "coll2", rng) == "b1"
    assert registry.get_random_id("db2", "coll1", rng) == "c1"

    registry.remove_id("db1", "coll1", "a1")
    assert registry.get_random_id("db1", "coll1", rng) is None
    assert registry.get_random_id("db1", "coll2", rng) == "b1"
    assert registry.get_random_id("db2", "coll1", rng) == "c1"


def test_empty_registry_returns_none() -> None:
    registry = DocumentIdRegistry(max_ids_per_namespace=100)
    rng = random.Random(23)

    assert registry.get_random_id("db1", "coll1", rng) is None


def test_duplicate_add_is_idempotent() -> None:
    registry = DocumentIdRegistry(max_ids_per_namespace=100)
    rng = random.Random(29)

    registry.add_id("db1", "coll1", "id1")
    registry.add_id("db1", "coll1", "id1")
    registry.add_id("db1", "coll1", "id1")

    seen: set[str] = set()
    for _ in range(50):
        got = registry.get_random_id("db1", "coll1", rng)
        assert got is not None
        seen.add(got)

    assert seen == {"id1"}


def test_thread_safety_concurrent_add_remove_get() -> None:
    registry = DocumentIdRegistry(max_ids_per_namespace=1000)
    rng = random.Random(31)
    errors: list[Exception] = []
    lock = threading.Lock()

    def add_ids(ids: list[int]) -> None:
        try:
            for i in ids:
                registry.add_id("db1", "coll1", i)
        except Exception as e:
            with lock:
                errors.append(e)

    def remove_ids(ids: list[int]) -> None:
        try:
            for i in ids:
                registry.remove_id("db1", "coll1", i)
        except Exception as e:
            with lock:
                errors.append(e)

    def get_ids(count: int) -> None:
        try:
            local_rng = random.Random(31)
            for _ in range(count):
                registry.get_random_id("db1", "coll1", local_rng)
        except Exception as e:
            with lock:
                errors.append(e)

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(add_ids, list(range(0, 100))),
            executor.submit(add_ids, list(range(100, 200))),
            executor.submit(add_ids, list(range(200, 300))),
            executor.submit(get_ids, 200),
        ]
        for f in as_completed(futures):
            f.result()
        futures = [
            executor.submit(remove_ids, list(range(50, 100))),
            executor.submit(remove_ids, list(range(100, 150))),
            executor.submit(get_ids, 200),
            executor.submit(get_ids, 200),
        ]
        for f in as_completed(futures):
            f.result()

    assert not errors

    seen: set[int] = set()
    for _ in range(500):
        got = registry.get_random_id("db1", "coll1", rng)
        if got is not None:
            seen.add(got)

    expected = set(range(0, 50)) | set(range(150, 300))
    assert seen <= expected
    assert len(seen) >= min(50, len(expected))
