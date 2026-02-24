"""Document generation based on declarative schema config."""

from __future__ import annotations

import math
import random
import string
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId
from faker import Faker

from mongo_load_tester.config import SchemaConfig, SchemaFieldConfig


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
        return {field.name: self._generate_value(field) for field in self._schema.fields}

    def generate_update_fields(self) -> dict[str, Any]:
        if not self._schema.fields:
            return {}
        field = self._rng.choice(self._schema.fields)
        return {field.name: self._generate_value(field)}

    def _generate_value(self, field: SchemaFieldConfig) -> Any:
        ft = field.field_type
        if ft == "choice":
            if not field.choices:
                return None
            return self._rng.choice(field.choices)
        if ft == "string":
            if field.choices:
                return self._rng.choice(field.choices)
            chars = string.ascii_letters + string.digits
            return "".join(self._rng.choice(chars) for _ in range(field.length))
        if ft == "int":
            low = math.floor(field.min_value)
            high = math.floor(field.max_value)
            if low > high:
                low, high = high, low
            return self._rng.randint(low, high)
        if ft == "float":
            return round(self._rng.uniform(field.min_value, field.max_value), 4)
        if ft == "bool":
            return self._rng.random() >= 0.5
        if ft == "datetime":
            return self._generate_datetime(field)
        if ft == "date_range":
            return self._generate_datetime(field)
        if ft == "objectid":
            return ObjectId()
        if ft == "uuid":
            return str(uuid.uuid4())
        if ft == "faker":
            return self._generate_faker_value(field)
        raise ValueError(f"Unsupported schema field type: {ft}")

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
