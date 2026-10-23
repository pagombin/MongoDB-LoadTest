"""Document generation based on declarative schema config."""

from __future__ import annotations

import math
import random
import string
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from bson import Decimal128, ObjectId
from faker import Faker

from mongo_load_tester.config import SchemaConfig, SchemaFieldConfig

UTC = timezone.utc


class DocumentGenerator:
    """Generates synthetic documents for insert/update operations."""

    def __init__(self, schema: SchemaConfig, rng: random.Random):
        self._schema = schema
        self._rng = rng
        self._faker = Faker()
        self._faker.seed_instance(rng.randint(0, 2**31))
        self._increment_counter = 0

    def generate_document(self) -> dict[str, Any]:
        self._increment_counter += 1
        return self._generate_object(self._schema.fields)

    def generate_update_fields(self) -> dict[str, Any]:
        if not self._schema.fields:
            return {}
        flat_fields = [f for f in self._schema.fields if f.field_type not in ("object", "array")]
        if not flat_fields:
            flat_fields = self._schema.fields
        field = self._rng.choice(flat_fields)
        return {field.name: self._generate_value(field)}

    def _generate_object(self, fields: list[SchemaFieldConfig]) -> dict[str, Any]:
        return {field.name: self._generate_value(field) for field in fields}

    def _generate_value(self, field: SchemaFieldConfig) -> Any:
        ft = field.field_type
        if ft == "choice":
            return self._rng.choice(field.choices) if field.choices else None
        if ft == "string":
            if field.choices:
                return self._rng.choice(field.choices)
            return self._generate_string(field)
        if ft == "int":
            low = math.floor(field.min_value)
            high = math.floor(field.max_value)
            if low > high:
                low, high = high, low
            return self._rng.randint(low, high)
        if ft == "float":
            return round(self._rng.uniform(field.min_value, field.max_value), 4)
        if ft == "decimal":
            val = round(self._rng.uniform(field.min_value, field.max_value), 4)
            return Decimal128(Decimal(str(val)))
        if ft == "bool":
            return self._rng.random() >= 0.5
        if ft in ("datetime", "date_range"):
            return self._generate_datetime(field)
        if ft == "objectid":
            return ObjectId()
        if ft == "uuid":
            return str(uuid.uuid4())
        if ft == "faker":
            return self._generate_faker_value(field)
        if ft == "null":
            return None
        if ft == "object":
            return self._generate_object(field.fields or [])
        if ft == "array":
            return self._generate_array(field)
        raise ValueError(f"Unsupported schema field type: {ft}")

    def _generate_string(self, field: SchemaFieldConfig) -> str:
        chars = string.ascii_letters + string.digits
        rand_part = "".join(self._rng.choice(chars) for _ in range(field.length))
        uid = str(uuid.uuid4())
        comp = field.composite
        if comp == "prefix_uuid":
            return f"{field.prefix}{uid}"
        if comp == "uuid_suffix":
            return f"{uid}{field.suffix}"
        if comp == "prefix_uuid_suffix":
            return f"{field.prefix}{uid}{field.suffix}"
        if comp == "prefix_random":
            return f"{field.prefix}{rand_part}"
        if comp == "random_suffix":
            return f"{rand_part}{field.suffix}"
        if field.prefix or field.suffix:
            return f"{field.prefix}{rand_part}{field.suffix}"
        return rand_part

    def _generate_array(self, field: SchemaFieldConfig) -> list[Any]:
        count = self._rng.randint(field.min_items, field.max_items)
        if field.items is None:
            return []
        return [self._generate_value(field.items) for _ in range(count)]

    def _generate_datetime(self, field: SchemaFieldConfig) -> datetime:
        now = datetime.now(UTC)
        if field.date_mode == "now":
            return now
        if field.date_mode == "random_past":
            seconds_back = self._rng.randint(0, field.date_range_days * 86400)
            return now - timedelta(seconds=seconds_back)
        if field.date_mode == "random_future":
            seconds_forward = self._rng.randint(0, field.date_range_days * 86400)
            return now + timedelta(seconds=seconds_forward)
        if field.date_mode == "increment":
            return now + timedelta(seconds=self._increment_counter * field.date_increment_seconds)
        return now

    def _generate_faker_value(self, field: SchemaFieldConfig) -> Any:
        provider = field.faker_provider or "name"
        fn = getattr(self._faker, provider, None)
        if fn is None or not callable(fn):
            return self._faker.text(max_nb_chars=field.length)
        return fn()
