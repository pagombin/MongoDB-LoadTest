"""Per-collection schema registry with default fallback."""

from __future__ import annotations

import random
import zlib
from threading import Lock

from mongo_load_tester.config import SchemaConfig
from mongo_load_tester.workload.schema import DocumentGenerator


class SchemaRegistry:
    """Maps (database, collection) to a DocumentGenerator with a default fallback.

    Thread-safe for reads after initialization. register() acquires a lock
    for concurrent writes during metadata loading.
    """

    def __init__(self, default_schema: SchemaConfig, base_seed: int = 42):
        self._default_schema = default_schema
        self._base_seed = base_seed
        self._generators: dict[tuple[str, str], DocumentGenerator] = {}
        self._schemas: dict[tuple[str, str], SchemaConfig] = {}
        self._default_generators: dict[tuple[str, str], DocumentGenerator] = {}
        self._lock = Lock()

    def register(self, database: str, collection: str, schema: SchemaConfig) -> None:
        """Register a per-collection schema override."""
        key = (database, collection)
        seed = self._base_seed + zlib.crc32(f"{database}.{collection}".encode()) % (2**31)
        with self._lock:
            self._schemas[key] = schema
            self._generators[key] = DocumentGenerator(schema, random.Random(seed))

    def get_generator(self, database: str, collection: str, rng: random.Random) -> DocumentGenerator:
        """Get the generator for a specific namespace, falling back to the default."""
        key = (database, collection)
        gen = self._generators.get(key)
        if gen is not None:
            return gen
        default_gen = self._default_generators.get(key)
        if default_gen is None:
            default_gen = DocumentGenerator(self._default_schema, rng)
            self._default_generators[key] = default_gen
        return default_gen

    def get_schema(self, database: str, collection: str) -> SchemaConfig:
        """Get the schema for a specific namespace, falling back to the default."""
        return self._schemas.get((database, collection), self._default_schema)

    def has_override(self, database: str, collection: str) -> bool:
        return (database, collection) in self._schemas

    def all_overrides(self) -> dict[tuple[str, str], SchemaConfig]:
        with self._lock:
            return dict(self._schemas)

    @property
    def default_schema(self) -> SchemaConfig:
        return self._default_schema
