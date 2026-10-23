"""Tests for MongoOperationExecutor."""

from __future__ import annotations

import logging
import random
from unittest.mock import MagicMock

import pytest
from bson import ObjectId
from pymongo.errors import AutoReconnect

from mongo_load_tester.config import (
    NamespaceConfig,
    OperationMixConfig,
    RetryConfig,
    SchemaConfig,
)
from mongo_load_tester.engine.executor import MongoOperationExecutor
from mongo_load_tester.engine.id_registry import DocumentIdRegistry
from mongo_load_tester.engine.retry import RetryController, RetryFailure, RetryPolicy
from mongo_load_tester.models import OperationResult, OperationType
from mongo_load_tester.workload.schema import DocumentGenerator
from mongo_load_tester.workload.selection import NamespaceSelector, OperationSelector


def _make_executor(
    *,
    client: MagicMock | None = None,
    namespace_selector: NamespaceSelector | None = None,
    operation_selector: OperationSelector | None = None,
    document_generator: DocumentGenerator | None = None,
    id_registry: DocumentIdRegistry | None = None,
    retry_policy: RetryPolicy | None = None,
    rng: random.Random | None = None,
) -> MongoOperationExecutor:
    """Build a minimal executor with mocked dependencies."""
    if client is None:
        client = MagicMock()
        mock_db = MagicMock()
        mock_coll = MagicMock()
        client.__getitem__.return_value.__getitem__.return_value = mock_coll
        client.__getitem__.return_value = mock_db
        mock_db.__getitem__.return_value = mock_coll

    if namespace_selector is None:
        ns_config = NamespaceConfig(
            database_prefix="db",
            collection_prefix="coll",
            databases=1,
            collections_per_database=1,
        )
        namespace_selector = NamespaceSelector(namespace=ns_config, rng=rng or random.Random(42))

    if operation_selector is None:
        operation_selector = OperationSelector(
            operation_mix=OperationMixConfig(read=1, insert=0, update=0, delete=0),
            rng=rng or random.Random(42),
        )

    if document_generator is None:
        schema = SchemaConfig(fields=[])
        document_generator = DocumentGenerator(schema=schema, rng=rng or random.Random(42))

    if id_registry is None:
        id_registry = DocumentIdRegistry(max_ids_per_namespace=100)

    if retry_policy is None:
        retry_policy = RetryPolicy.from_config(
            RetryConfig(max_attempts=3, initial_backoff_ms=1, max_backoff_ms=10, jitter_ratio=0.0)
        )

    logger = logging.getLogger("test_executor")
    rng = rng or random.Random(42)

    return MongoOperationExecutor(
        client=client,
        namespace_selector=namespace_selector,
        operation_selector=operation_selector,
        document_generator=document_generator,
        id_registry=id_registry,
        retry_policy=retry_policy,
        logger=logger,
        rng=rng,
    )


def test_execute_once_returns_operation_result_with_correct_fields() -> None:
    """execute_once returns an OperationResult with correct fields."""
    mock_coll = MagicMock()
    mock_coll.find_one.return_value = {"_id": ObjectId()}
    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = mock_coll

    op_selector = OperationSelector(
        operation_mix=OperationMixConfig(read=1, insert=0, update=0, delete=0),
        rng=random.Random(7),
    )
    executor = _make_executor(client=client, operation_selector=op_selector)

    result = executor.execute_once()

    assert isinstance(result, OperationResult)
    assert result.operation == OperationType.READ
    assert result.database == "db_1"
    assert result.collection == "coll_1"
    assert result.success is True
    assert result.error is None
    assert result.attempts == 1
    assert result.latency_ms >= 0
    assert result.timestamp is not None


def test_read_operation_uses_find_one_with_known_id_from_registry() -> None:
    """Read operation uses find_one with known ID from registry."""
    known_id = ObjectId()
    mock_coll = MagicMock()
    mock_coll.find_one.return_value = {"_id": known_id}

    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = mock_coll

    id_registry = DocumentIdRegistry()
    id_registry.add_id("db_1", "coll_1", known_id)

    op_selector = OperationSelector(
        operation_mix=OperationMixConfig(read=1, insert=0, update=0, delete=0),
        rng=random.Random(99),
    )
    executor = _make_executor(
        client=client,
        operation_selector=op_selector,
        id_registry=id_registry,
    )

    executor.execute_once()

    mock_coll.find_one.assert_called_once_with({"_id": known_id})


def test_read_fallback_uses_find_one_empty_with_hint_when_registry_empty() -> None:
    """Read fallback uses find_one({}) with hint when registry is empty."""
    fallback_doc = {"_id": ObjectId()}
    mock_coll = MagicMock()
    mock_coll.find_one.side_effect = [None, fallback_doc]

    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = mock_coll

    id_registry = DocumentIdRegistry()

    op_selector = OperationSelector(
        operation_mix=OperationMixConfig(read=1, insert=0, update=0, delete=0),
        rng=random.Random(11),
    )
    executor = _make_executor(
        client=client,
        operation_selector=op_selector,
        id_registry=id_registry,
    )

    executor.execute_once()

    calls = mock_coll.find_one.call_args_list
    assert len(calls) >= 1
    fallback_call = calls[0]
    args, kwargs = fallback_call
    assert args[0] == {}
    assert args[1] == {"_id": 1}
    hint_value = args[2] if len(args) > 2 else kwargs.get("hint")
    assert hint_value == [("_id", 1)]


def test_insert_calls_insert_one_and_registers_new_id() -> None:
    """Insert calls insert_one and registers the new ID."""
    inserted_id = ObjectId()
    mock_coll = MagicMock()
    mock_coll.insert_one.return_value.inserted_id = inserted_id

    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = mock_coll

    doc_gen = MagicMock()
    doc_gen.generate_document.return_value = {"x": 1}

    op_selector = OperationSelector(
        operation_mix=OperationMixConfig(read=0, insert=1, update=0, delete=0),
        rng=random.Random(22),
    )
    id_registry = DocumentIdRegistry()
    executor = _make_executor(
        client=client,
        operation_selector=op_selector,
        document_generator=doc_gen,
        id_registry=id_registry,
    )

    executor.execute_once()

    mock_coll.insert_one.assert_called_once_with({"x": 1})
    assert id_registry.get_random_id("db_1", "coll_1", random.Random(22)) == inserted_id


def test_update_with_upsert_false_only_registers_id_when_matched_count_gt_zero() -> None:
    """Update with upsert=False - only registers ID when matched_count > 0."""
    known_id = ObjectId()
    mock_coll = MagicMock()
    mock_result_no_match = MagicMock()
    mock_result_no_match.matched_count = 0
    mock_result_match = MagicMock()
    mock_result_match.matched_count = 1

    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = mock_coll

    id_registry = DocumentIdRegistry()
    id_registry.add_id("db_1", "coll_1", known_id)

    doc_gen = MagicMock()
    doc_gen.generate_update_fields.return_value = {"status": "updated"}

    rng = random.Random(33)

    op_selector = OperationSelector(
        operation_mix=OperationMixConfig(read=0, insert=0, update=1, delete=0),
        rng=rng,
    )

    mock_coll.update_one.return_value = mock_result_no_match
    executor = _make_executor(
        client=client,
        operation_selector=op_selector,
        document_generator=doc_gen,
        id_registry=id_registry,
        rng=rng,
    )
    executor.execute_once()

    id_registry.remove_id("db_1", "coll_1", known_id)
    id_registry.add_id("db_1", "coll_1", known_id)

    mock_coll.update_one.return_value = mock_result_match
    executor.execute_once()

    assert id_registry.get_random_id("db_1", "coll_1", rng) == known_id


def test_delete_checks_deleted_count_before_removing_from_registry() -> None:
    """Delete checks deleted_count before removing from registry."""
    known_id = ObjectId()
    mock_coll = MagicMock()
    mock_result_no_delete = MagicMock()
    mock_result_no_delete.deleted_count = 0
    mock_result_deleted = MagicMock()
    mock_result_deleted.deleted_count = 1

    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = mock_coll

    id_registry = DocumentIdRegistry()
    id_registry.add_id("db_1", "coll_1", known_id)

    op_selector = OperationSelector(
        operation_mix=OperationMixConfig(read=0, insert=0, update=0, delete=1),
        rng=random.Random(44),
    )

    mock_coll.delete_one.return_value = mock_result_no_delete
    executor = _make_executor(
        client=client,
        operation_selector=op_selector,
        id_registry=id_registry,
        rng=random.Random(44),
    )
    executor.execute_once()

    check_rng = random.Random(99)
    assert id_registry.get_random_id("db_1", "coll_1", check_rng) == known_id

    mock_coll.delete_one.return_value = mock_result_deleted
    executor.execute_once()

    assert id_registry.get_random_id("db_1", "coll_1", check_rng) is None


def test_retry_controller_is_cached_and_reused_across_execute_once_calls() -> None:
    """RetryController is cached and reused across execute_once calls."""
    mock_coll = MagicMock()
    mock_coll.find_one.return_value = {"_id": ObjectId()}
    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = mock_coll

    policy = RetryPolicy.from_config(RetryConfig(max_attempts=3))
    policy_provider = MagicMock(return_value=policy)

    op_selector = OperationSelector(
        operation_mix=OperationMixConfig(read=1, insert=0, update=0, delete=0),
        rng=random.Random(55),
    )
    executor = MongoOperationExecutor(
        client=client,
        namespace_selector=NamespaceSelector(
            namespace=NamespaceConfig(
                database_prefix="db",
                collection_prefix="coll",
                databases=1,
                collections_per_database=1,
            ),
            rng=random.Random(55),
        ),
        operation_selector=op_selector,
        document_generator=DocumentGenerator(schema=SchemaConfig(fields=[]), rng=random.Random(55)),
        id_registry=DocumentIdRegistry(),
        retry_policy=None,
        retry_policy_provider=policy_provider,
        logger=logging.getLogger("test"),
        rng=random.Random(55),
    )

    executor.execute_once()
    executor.execute_once()

    assert policy_provider.call_count == 2
    assert executor._cached_retry_controller is not None
    first_controller = executor._cached_retry_controller
    second_controller = executor._get_retry_controller()
    assert first_controller is second_controller


def test_retry_failure_results_in_success_false_with_error_message(monkeypatch) -> None:
    """Retry failure results in success=False with error message."""
    mock_coll = MagicMock()
    mock_coll.find_one.side_effect = AutoReconnect("network error")
    client = MagicMock()
    client.__getitem__.return_value.__getitem__.return_value = mock_coll

    monkeypatch.setattr(
        "mongo_load_tester.engine.retry.time.sleep",
        lambda _: None,
    )

    policy = RetryPolicy.from_config(
        RetryConfig(max_attempts=2, initial_backoff_ms=1, max_backoff_ms=5, jitter_ratio=0.0)
    )
    op_selector = OperationSelector(
        operation_mix=OperationMixConfig(read=1, insert=0, update=0, delete=0),
        rng=random.Random(66),
    )
    executor = _make_executor(
        client=client,
        operation_selector=op_selector,
        retry_policy=policy,
    )

    result = executor.execute_once()

    assert result.success is False
    assert result.error is not None
    assert "network error" in result.error
    assert result.attempts == 2
