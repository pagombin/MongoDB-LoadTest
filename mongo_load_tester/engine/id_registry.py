"""Thread-safe local cache of document IDs per namespace."""

from __future__ import annotations

import random
from collections import defaultdict
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


class DocumentIdRegistry:
    """Keeps a bounded rolling window of known _id values."""

    def __init__(self, max_ids_per_namespace: int = 20_000):
        self._max_ids_per_namespace = max_ids_per_namespace
        self._ids: dict[tuple[str, str], _IndexedIdSet] = defaultdict(
            lambda: _IndexedIdSet(self._max_ids_per_namespace)
        )
        self._lock = Lock()

    def add_id(self, database: str, collection: str, document_id: Any) -> None:
        key = (database, collection)
        with self._lock:
            self._ids[key].add(document_id)

    def get_random_id(self, database: str, collection: str, rng: random.Random) -> Any | None:
        key = (database, collection)
        with self._lock:
            bucket = self._ids.get(key)
            if not bucket:
                return None
            return bucket.random(rng)

    def remove_id(self, database: str, collection: str, document_id: Any) -> None:
        key = (database, collection)
        with self._lock:
            bucket = self._ids.get(key)
            if not bucket:
                return
            bucket.remove(document_id)
