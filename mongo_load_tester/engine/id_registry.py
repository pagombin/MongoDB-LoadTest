"""Thread-safe local cache of document IDs per namespace."""

from __future__ import annotations

import random
from threading import Lock
from typing import Any


class _IndexedIdSet:
    """O(1) random access and O(1) amortized removal via swap-and-pop."""

    __slots__ = ("_ids", "_index", "_max_size")

    def __init__(self, max_size: int):
        self._ids: list[Any] = []
        self._index: dict[Any, int] = {}
        self._max_size = max_size

    def __len__(self) -> int:
        return len(self._ids)

    def __bool__(self) -> bool:
        return len(self._ids) > 0

    def add(self, doc_id: Any) -> None:
        if doc_id in self._index:
            return
        if len(self._ids) >= self._max_size:
            evicted = self._ids[0]
            self._swap_remove(0)
            self._index.pop(evicted, None)
        self._index[doc_id] = len(self._ids)
        self._ids.append(doc_id)

    def random(self, rng: random.Random) -> Any | None:
        if not self._ids:
            return None
        return self._ids[rng.randrange(len(self._ids))]

    def remove(self, doc_id: Any) -> None:
        idx = self._index.pop(doc_id, None)
        if idx is not None:
            self._swap_remove(idx)

    def _swap_remove(self, idx: int) -> None:
        last = len(self._ids) - 1
        if idx != last:
            self._ids[idx] = self._ids[last]
            self._index[self._ids[idx]] = idx
        self._ids.pop()


class _LockedBucket:
    """Per-namespace _IndexedIdSet with its own lock to reduce contention."""

    __slots__ = ("lock", "id_set")

    def __init__(self, max_size: int):
        self.lock = Lock()
        self.id_set = _IndexedIdSet(max_size)


class DocumentIdRegistry:
    """Keeps a bounded rolling window of known _id values."""

    def __init__(self, max_ids_per_namespace: int = 20_000):
        self._max_ids_per_namespace = max_ids_per_namespace
        self._buckets: dict[tuple[str, str], _LockedBucket] = {}
        self._registry_lock = Lock()

    def _get_bucket(self, key: tuple[str, str]) -> _LockedBucket:
        bucket = self._buckets.get(key)
        if bucket is not None:
            return bucket
        with self._registry_lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _LockedBucket(self._max_ids_per_namespace)
                self._buckets[key] = bucket
            return bucket

    def add_id(self, database: str, collection: str, document_id: Any) -> None:
        bucket = self._get_bucket((database, collection))
        with bucket.lock:
            bucket.id_set.add(document_id)

    def get_random_id(self, database: str, collection: str, rng: random.Random) -> Any | None:
        key = (database, collection)
        bucket = self._buckets.get(key)
        if bucket is None:
            return None
        with bucket.lock:
            if not bucket.id_set:
                return None
            return bucket.id_set.random(rng)

    def remove_id(self, database: str, collection: str, document_id: Any) -> None:
        key = (database, collection)
        bucket = self._buckets.get(key)
        if bucket is None:
            return
        with bucket.lock:
            bucket.id_set.remove(document_id)
