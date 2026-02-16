"""Thread-safe local cache of document IDs per namespace."""

from __future__ import annotations

import random
from collections import defaultdict, deque
from threading import Lock
from typing import Any


class DocumentIdRegistry:
    """Keeps a bounded rolling window of known _id values."""

    def __init__(self, max_ids_per_namespace: int = 20_000):
        self._max_ids_per_namespace = max_ids_per_namespace
        self._ids: dict[tuple[str, str], deque[Any]] = defaultdict(deque)
        self._lock = Lock()

    def add_id(self, database: str, collection: str, document_id: Any) -> None:
        key = (database, collection)
        with self._lock:
            bucket = self._ids[key]
            bucket.append(document_id)
            while len(bucket) > self._max_ids_per_namespace:
                bucket.popleft()

    def get_random_id(self, database: str, collection: str, rng: random.Random) -> Any | None:
        key = (database, collection)
        with self._lock:
            bucket = self._ids.get(key)
            if not bucket:
                return None
            return bucket[rng.randrange(len(bucket))]

    def remove_id(self, database: str, collection: str, document_id: Any) -> None:
        key = (database, collection)
        with self._lock:
            bucket = self._ids.get(key)
            if not bucket:
                return
            try:
                bucket.remove(document_id)
            except ValueError:
                pass
