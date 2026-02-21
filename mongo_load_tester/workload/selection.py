"""Operation and namespace selection strategies."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Callable

from mongo_load_tester.config import NamespaceConfig, OperationMixConfig
from mongo_load_tester.models import OperationType


class OperationSelector:
    """Weighted random operation chooser."""

    def __init__(
        self,
        operation_mix: OperationMixConfig,
        rng: random.Random,
        mix_provider: Callable[[], OperationMixConfig] | None = None,
    ):
        self._rng = rng
        self._mix_provider = mix_provider
        self._operations: list[OperationType] = [
            OperationType.READ,
            OperationType.INSERT,
            OperationType.UPDATE,
            OperationType.DELETE,
        ]
        self._weights: list[float] = self._build_weights(operation_mix.normalized())

    def choose(self) -> OperationType:
        if self._mix_provider is not None:
            weights = self._build_weights(self._mix_provider().normalized())
            return self._rng.choices(self._operations, weights=weights, k=1)[0]
        return self._rng.choices(self._operations, weights=self._weights, k=1)[0]

    @staticmethod
    def _build_weights(normalized: dict[str, float]) -> list[float]:
        return [
            normalized["read"],
            normalized["insert"],
            normalized["update"],
            normalized["delete"],
        ]


@dataclass(frozen=True, slots=True)
class NamespaceTarget:
    database: str
    collection: str


class NamespaceSelector:
    """Randomly pick target database/collection combinations."""

    def __init__(self, namespace: NamespaceConfig, rng: random.Random):
        self._rng = rng
        self._targets: list[NamespaceTarget] = []
        for db_idx in range(namespace.databases):
            database_name = f"{namespace.database_prefix}_{db_idx + 1}"
            for coll_idx in range(namespace.collections_per_database):
                collection_name = f"{namespace.collection_prefix}_{coll_idx + 1}"
                self._targets.append(NamespaceTarget(database=database_name, collection=collection_name))

    @property
    def targets(self) -> tuple[NamespaceTarget, ...]:
        return tuple(self._targets)

    def choose(self) -> NamespaceTarget:
        return self._rng.choice(self._targets)
