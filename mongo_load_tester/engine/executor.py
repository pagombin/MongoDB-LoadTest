"""Low-level MongoDB operation executor."""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone
UTC = timezone.utc
from typing import Any, Callable

from bson import ObjectId
from pymongo import MongoClient, ReturnDocument

from mongo_load_tester.config import QueryTargetConfig
from mongo_load_tester.engine.retry import RetryController, RetryFailure, RetryPolicy
from mongo_load_tester.engine.id_registry import DocumentIdRegistry
from mongo_load_tester.models import OperationResult, OperationType
from mongo_load_tester.workload.registry import SchemaRegistry
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
        retry_policy: RetryPolicy | None,
        logger: logging.Logger,
        rng: random.Random,
        retry_policy_provider: Callable[[], RetryPolicy] | None = None,
        query_targets: QueryTargetConfig | None = None,
        schema_registry: SchemaRegistry | None = None,
    ):
        self._client = client
        self._namespace_selector = namespace_selector
        self._operation_selector = operation_selector
        self._document_generator = document_generator
        self._id_registry = id_registry
        self._logger = logger
        self._rng = rng
        self._query_targets = query_targets
        self._schema_registry = schema_registry
        if retry_policy_provider is not None:
            self._retry_policy_provider = retry_policy_provider
        elif retry_policy is not None:
            self._retry_policy_provider = lambda: retry_policy
        else:
            raise ValueError("Either retry_policy or retry_policy_provider must be provided.")
        self._cached_retry_controller: RetryController | None = None
        self._cached_retry_policy_id: int | None = None

    def _get_retry_controller(self) -> RetryController:
        policy = self._retry_policy_provider()
        policy_id = id(policy)
        if self._cached_retry_controller is not None and self._cached_retry_policy_id == policy_id:
            return self._cached_retry_controller
        self._cached_retry_controller = RetryController(
            policy=policy, rng=self._rng, log_warning=self._logger.warning,
        )
        self._cached_retry_policy_id = policy_id
        return self._cached_retry_controller

    @property
    def _use_query_targets(self) -> bool:
        return self._query_targets is not None and self._query_targets.enabled

    def _get_generator(self, database: str, coll: str) -> DocumentGenerator:
        if self._schema_registry is not None:
            return self._schema_registry.get_generator(database, coll, self._rng)
        return self._document_generator

    def execute_once(self) -> OperationResult:
        target = self._namespace_selector.choose()
        collection = self._client[target.database][target.collection]
        operation = self._operation_selector.choose()
        start = time.perf_counter()
        success = True
        error: str | None = None
        attempts = 1
        operation_name = f"{operation.value} {target.database}.{target.collection}"
        retry_controller = self._get_retry_controller()
        try:
            _, attempts = retry_controller.run(
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
                operation.value, target.database, target.collection,
            )
        latency_ms = (time.perf_counter() - start) * 1000.0
        return OperationResult(
            timestamp=datetime.now(UTC), operation=operation,
            database=target.database, collection=target.collection,
            latency_ms=latency_ms, success=success, error=error, attempts=attempts,
        )

    def _dispatch_operation(self, operation: OperationType, collection: Any, database: str, coll: str) -> None:
        if operation == OperationType.READ:
            self._run_read(collection, database, coll)
        elif operation == OperationType.INSERT:
            self._run_insert(collection, database, coll)
        elif operation == OperationType.UPDATE:
            self._run_update(collection, database, coll)
        elif operation == OperationType.DELETE:
            self._run_delete(collection, database, coll)
        elif operation == OperationType.FIND_AND_MODIFY:
            self._run_find_and_modify(collection, database, coll)
        else:
            raise ValueError(f"Unsupported operation type: {operation}")

    # ---- READ ----

    def _run_read(self, collection: Any, database: str, coll: str) -> None:
        if self._use_query_targets and self._query_targets.read.filter_fields:
            query_filter = self._build_field_filter(collection, self._query_targets.read.filter_fields)
            if query_filter:
                kwargs: dict[str, Any] = {}
                proj = self._query_targets.read.projection_fields
                if proj:
                    kwargs["projection"] = {f: 1 for f in proj}
                sort_field = self._query_targets.read.sort_field
                if sort_field:
                    direction = 1 if self._query_targets.read.sort_direction == "asc" else -1
                    kwargs["sort"] = [(sort_field, direction)]
                collection.find_one(query_filter, **kwargs)
                return
        document_id = self._choose_known_id(collection, database, coll)
        if document_id is not None:
            collection.find_one({"_id": document_id})
            return
        found = collection.find_one({}, {"_id": 1}, hint=[("_id", 1)])
        if found and "_id" in found:
            self._id_registry.add_id(database, coll, found["_id"])

    # ---- INSERT ----

    def _run_insert(self, collection: Any, database: str, coll: str) -> None:
        gen = self._get_generator(database, coll)
        document = gen.generate_document()
        inserted = collection.insert_one(document)
        self._id_registry.add_id(database, coll, inserted.inserted_id)

    # ---- UPDATE ----

    def _run_update(self, collection: Any, database: str, coll: str) -> None:
        gen = self._get_generator(database, coll)
        update_doc = self._build_update_document(gen, "update")

        if self._use_query_targets and self._query_targets.update.filter_fields:
            query_filter = self._build_field_filter(collection, self._query_targets.update.filter_fields)
            if query_filter:
                collection.update_one(query_filter, update_doc)
                return

        document_id = self._choose_known_id(collection, database, coll)
        if document_id is None:
            document_id = ObjectId()
        result = collection.update_one({"_id": document_id}, update_doc, upsert=False)
        if result.matched_count > 0:
            self._id_registry.add_id(database, coll, document_id)

    # ---- DELETE ----

    def _run_delete(self, collection: Any, database: str, coll: str) -> None:
        if self._use_query_targets and self._query_targets.delete.filter_fields:
            query_filter = self._build_field_filter(collection, self._query_targets.delete.filter_fields)
            if query_filter:
                collection.delete_one(query_filter)
                return

        document_id = self._choose_known_id(collection, database, coll)
        if document_id is None:
            found = collection.find_one({}, {"_id": 1}, hint=[("_id", 1)])
            if not found:
                return
            document_id = found["_id"]
        result = collection.delete_one({"_id": document_id})
        if result.deleted_count > 0:
            self._id_registry.remove_id(database, coll, document_id)

    # ---- FIND AND MODIFY ----

    def _run_find_and_modify(self, collection: Any, database: str, coll: str) -> None:
        gen = self._get_generator(database, coll)
        fam = self._query_targets.find_and_modify if self._use_query_targets else None

        if fam and fam.filter_fields:
            query_filter = self._build_field_filter(collection, fam.filter_fields)
        else:
            document_id = self._choose_known_id(collection, database, coll)
            query_filter = {"_id": document_id} if document_id else {"_id": ObjectId()}

        if not query_filter:
            return

        update_doc = self._build_update_document(gen, "find_and_modify")

        kwargs: dict[str, Any] = {
            "return_document": ReturnDocument.AFTER if (fam and fam.return_updated) else ReturnDocument.BEFORE,
        }
        if fam and fam.sort_field:
            direction = 1 if fam.sort_direction == "asc" else -1
            kwargs["sort"] = [(fam.sort_field, direction)]
        if fam and fam.upsert:
            kwargs["upsert"] = True

        collection.find_one_and_update(query_filter, update_doc, **kwargs)

    # ---- HELPERS ----

    def _build_update_document(self, gen: DocumentGenerator, op_kind: str) -> dict[str, Any]:
        """Build a MongoDB update document with configured operators."""
        update_doc: dict[str, Any] = {}
        operators = None
        if self._use_query_targets:
            if op_kind == "update" and self._query_targets.update.operators:
                operators = self._query_targets.update.operators
            elif op_kind == "find_and_modify" and self._query_targets.find_and_modify.operators:
                operators = self._query_targets.find_and_modify.operators

        if operators:
            if operators.set_fields:
                set_vals: dict[str, Any] = {}
                for f_name in operators.set_fields:
                    set_vals[f_name] = gen.generate_update_fields().get(f_name) or gen._generate_value(
                        next((f for f in gen._schema.fields if f.name == f_name), gen._schema.fields[0])
                    ) if gen._schema.fields else None
                if set_vals:
                    update_doc["$set"] = set_vals

            if operators.inc_fields:
                inc_vals: dict[str, Any] = {}
                for f_name in operators.inc_fields:
                    inc_vals[f_name] = self._rng.randint(1, 10)
                update_doc["$inc"] = inc_vals

            if operators.unset_fields:
                update_doc["$unset"] = {f: "" for f in operators.unset_fields}

            if operators.push_fields:
                push_vals: dict[str, Any] = {}
                for f_name in operators.push_fields:
                    field_cfg = next((f for f in gen._schema.fields if f.name == f_name), None)
                    if field_cfg:
                        push_vals[f_name] = gen._generate_value(field_cfg)
                    else:
                        push_vals[f_name] = gen._rng.randint(1, 100)
                if push_vals:
                    update_doc["$push"] = push_vals

            if operators.current_date_fields:
                update_doc["$currentDate"] = {f: True for f in operators.current_date_fields}

        if not update_doc:
            update_doc["$set"] = gen.generate_update_fields()

        return update_doc

    def _build_field_filter(self, collection: Any, fields: list[str]) -> dict[str, Any] | None:
        """Sample a random document and build a filter from the specified fields."""
        sample = list(collection.aggregate([{"$sample": {"size": 1}}]))
        if not sample:
            return None
        doc = sample[0]
        query: dict[str, Any] = {}
        for field_name in fields:
            value = _get_nested_value(doc, field_name)
            if value is not None:
                query[field_name] = value
        return query if query else None

    def _choose_known_id(self, collection: Any, database: str, coll: str) -> Any | None:
        document_id = self._id_registry.get_random_id(database, coll, self._rng)
        if document_id is not None:
            return document_id
        found = collection.find_one({}, {"_id": 1}, hint=[("_id", 1)])
        if not found:
            return None
        document_id = found["_id"]
        self._id_registry.add_id(database, coll, document_id)
        return document_id


def _get_nested_value(doc: dict, dotted_key: str) -> Any | None:
    """Traverse a document using dot-notation (e.g. 'address.city')."""
    parts = dotted_key.split(".")
    current: Any = doc
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current
