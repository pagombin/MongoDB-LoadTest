from __future__ import annotations

import random

from mongo_load_tester.config import NamespaceConfig, OperationMixConfig
from mongo_load_tester.models import OperationType
from mongo_load_tester.workload.selection import NamespaceSelector, OperationSelector


def test_operation_selector_single_weight() -> None:
    selector = OperationSelector(
        operation_mix=OperationMixConfig(read=0, insert=1, update=0, delete=0, find_and_modify=0),
        rng=random.Random(123),
    )
    picks = [selector.choose() for _ in range(200)]
    assert set(picks) == {OperationType.INSERT}


def test_namespace_selector_builds_full_cartesian_set() -> None:
    selector = NamespaceSelector(
        namespace=NamespaceConfig(
            database_prefix="db",
            collection_prefix="orders",
            databases=3,
            collections_per_database=2,
        ),
        rng=random.Random(7),
    )
    targets = selector.targets
    assert len(targets) == 6
    assert targets[0].database == "db_1"
    assert targets[0].collection == "orders_1"
    assert targets[-1].database == "db_3"
    assert targets[-1].collection == "orders_2"


def test_operation_selector_uses_live_mix_provider() -> None:
    mix = OperationMixConfig(read=1, insert=0, update=0, delete=0, find_and_modify=0)

    def provider() -> OperationMixConfig:
        return mix

    selector = OperationSelector(
        operation_mix=OperationMixConfig(read=0, insert=1, update=0, delete=0, find_and_modify=0),
        rng=random.Random(99),
        mix_provider=provider,
    )
    picks_before = [selector.choose() for _ in range(100)]
    assert set(picks_before) == {OperationType.READ}

    mix = OperationMixConfig(read=0, insert=0, update=1, delete=0, find_and_modify=0)
    picks_after = [selector.choose() for _ in range(100)]
    assert set(picks_after) == {OperationType.UPDATE}
