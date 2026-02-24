"""Document generation based on declarative schema config."""

from __future__ import annotations

import math
import random
import string
from datetime import UTC, datetime
from typing import Any

from mongo_load_tester.config import SchemaConfig, SchemaFieldConfig


class DocumentGenerator:
    """Generates synthetic documents for insert/update operations."""

    def __init__(self, schema: SchemaConfig, rng: random.Random):
        self._schema = schema
        self._rng = rng

    def generate_document(self) -> dict[str, Any]:
        return {field.name: self._generate_value(field) for field in self._schema.fields}

    def generate_update_fields(self) -> dict[str, Any]:
        if not self._schema.fields:
            return {}
        field = self._rng.choice(self._schema.fields)
        return {field.name: self._generate_value(field)}

    def _generate_value(self, field: SchemaFieldConfig) -> Any:
        if field.choices:
            return self._rng.choice(field.choices)
        if field.field_type == "string":
            chars = string.ascii_letters + string.digits
            return "".join(self._rng.choice(chars) for _ in range(field.length))
        if field.field_type == "int":
            low = math.floor(field.min_value)
            high = math.floor(field.max_value)
            if low > high:
                low, high = high, low
            return self._rng.randint(low, high)
        if field.field_type == "float":
            return round(self._rng.uniform(field.min_value, field.max_value), 4)
        if field.field_type == "bool":
            return self._rng.random() >= 0.5
        if field.field_type == "datetime":
            return datetime.now(UTC)
        raise ValueError(f"Unsupported schema field type: {field.field_type}")
