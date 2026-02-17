"""Low-level MongoDB operation executor."""

from __future__ import annotations

import logging
import random
import time
from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo import MongoClient

from mongo_load_tester.engine.retry import RetryController, RetryFailure, RetryPolicy
from mongo_load_tester.engine.id_registry import DocumentIdRegistry
from mongo_load_tester.models import OperationResult, OperationType
from mongo_load_tester.workload.schema import DocumentGenerator
from mongo_load_tester.workload.selection import NamespaceSelector, OperationSelector


class MongoOperationExecutor:
    """Executes a weighted operation against a random namespace target."""

    def __init__(
        self,
        client: MongoClient,
        namespace_selector: NamespaceSelector,
        operation_selector: OperationSelector,
        document_generator: DocumentGenerator,
        id_registry: DocumentIdRegistry,
        retry_policy: RetryPolicy,
        logger: logging.Logger,
        rng: random.Random,
    ):
        self._client = client
        self._namespace_selector = namespace_selector
        self._operation_selector = operation_selector
        self._document_generator = document_generator
        self._id_registry = id_registry
        self._logger = logger
        self._rng = rng
        self._retry_controller = RetryController(
            policy=retry_policy,
            rng=rng,
            log_warning=self._logger.warning,
        )

    def execute_once(self) -> OperationResult:
        target = self._namespace_selector.choose()
        collection = self._client[target.database][target.collection]
        operation = self._operation_selector.choose()
        start = time.perf_counter()
        success = True
        error: str | None = None
        attempts = 1
        operation_name = f"{operation.value} {target.database}.{target.collection}"

        try:
            _, attempts = self._retry_controller.run(
                operation_name=operation_name,
                func=lambda: self._dispatch_operation(operation, collection, target.database, target.collection),
            )
        except RetryFailure as exc:
            success = False
            attempts = exc.attempts
            error = str(exc.last_exception)
        except Exception as exc:  # pragma: no cover - runtime safety
            success = False
            attempts = 1
            error = str(exc)
            self._logger.exception(
                "Unexpected executor failure op=%s db=%s coll=%s",
                operation.value,
                target.database,
                target.collection,
            )

        latency_ms = (time.perf_counter() - start) * 1000.0
        return OperationResult(
            timestamp=datetime.now(UTC),
            operation=operation,
            database=target.database,
            collection=target.collection,
            latency_ms=latency_ms,
            success=success,
            error=error,
            attempts=attempts,
        )

    def _dispatch_operation(self, operation: OperationType, collection: Any, database: str, coll: str) -> None:
        if operation == OperationType.READ:
            self._run_read(collection, database, coll)
            return
        if operation == OperationType.INSERT:
            self._run_insert(collection, database, coll)
            return
        if operation == OperationType.UPDATE:
            self._run_update(collection, database, coll)
            return
        if operation == OperationType.DELETE:
            self._run_delete(collection, database, coll)
            return
        raise ValueError(f"Unsupported operation type: {operation}")

    def _run_read(self, collection: Any, database: str, coll: str) -> None:
        document_id = self._choose_known_id(collection, database, coll)
        if document_id is not None:
            collection.find_one({"_id": document_id})
            return
        found = collection.find_one({}, {"_id": 1})
        if found and "_id" in found:
            self._id_registry.add_id(database, coll, found["_id"])

    def _run_insert(self, collection: Any, database: str, coll: str) -> None:
        document = self._document_generator.generate_document()
        inserted = collection.insert_one(document)
        self._id_registry.add_id(database, coll, inserted.inserted_id)

    def _run_update(self, collection: Any, database: str, coll: str) -> None:
        update_fields = self._document_generator.generate_update_fields()
        document_id = self._choose_known_id(collection, database, coll)
        if document_id is None:
            document_id = ObjectId()
        collection.update_one({"_id": document_id}, {"$set": update_fields}, upsert=True)
        self._id_registry.add_id(database, coll, document_id)

    def _run_delete(self, collection: Any, database: str, coll: str) -> None:
        document_id = self._choose_known_id(collection, database, coll)
        if document_id is None:
            found = collection.find_one({}, {"_id": 1})
            if not found:
                return
            document_id = found["_id"]
        collection.delete_one({"_id": document_id})
        self._id_registry.remove_id(database, coll, document_id)

    def _choose_known_id(self, collection: Any, database: str, coll: str) -> Any | None:
        document_id = self._id_registry.get_random_id(database, coll, self._rng)
        if document_id is not None:
            return document_id
        found = collection.find_one({}, {"_id": 1})
        if not found:
            return None
        document_id = found["_id"]
        self._id_registry.add_id(database, coll, document_id)
        return document_id
