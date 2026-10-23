"""Document generation for the simple shape and the legacy schema shape."""

from __future__ import annotations

import random
import string
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ballast.engine.profile import DocumentSpec

if TYPE_CHECKING:
    from mongo_load_tester.workload.schema import DocumentGenerator

UTC = timezone.utc
SCHEMA_SHAPE_NEEDS = ("faker", "yaml")


def _schema_generator(fields: list, rng: random.Random) -> DocumentGenerator:
    """The legacy field-type schema shape reuses the legacy generator, which needs Faker and PyYAML."""
    try:
        from mongo_load_tester.config import SchemaConfig
        from mongo_load_tester.workload.schema import DocumentGenerator
    except ImportError as exc:
        raise RuntimeError(
            f"the 'schema' document shape needs the {exc.name} package; install it (pip install faker pyyaml) or use the 'simple' shape"
        ) from None
    return DocumentGenerator(SchemaConfig(fields=fields), rng)


_PAD_ALPHABET = string.ascii_letters + string.digits
_FIXED_OVERHEAD = 60
_PER_FIELD_OVERHEAD = 16


class DocumentFactory:
    def __init__(self, spec: DocumentSpec, rng: random.Random):
        self.spec = spec
        self.rng = rng
        self._schema_gen: DocumentGenerator | None = None
        if spec.mode == "schema":
            self._schema_gen = _schema_generator(spec.schema_fields or [], rng)
        pad_len = max(0, spec.size_bytes - _FIXED_OVERHEAD - spec.fields * _PER_FIELD_OVERHEAD)
        self._pad_len = pad_len
        self._pad_source = "".join(rng.choice(_PAD_ALPHABET) for _ in range(max(pad_len * 2, 64)))

    def _pad(self) -> str:
        if self._pad_len == 0:
            return ""
        start = self.rng.randrange(len(self._pad_source) - self._pad_len + 1)
        return self._pad_source[start : start + self._pad_len]

    def new(self, key: int) -> dict[str, Any]:
        if self._schema_gen is not None:
            generated = self._schema_gen.generate_document()
            generated["_id"] = key
            return generated
        doc: dict[str, Any] = {"_id": key, "v": self.rng.randrange(1_000_000), "n": 0, "ts": datetime.now(UTC)}
        for i in range(self.spec.fields - 3):
            doc[f"f{i}"] = self.rng.randrange(1_000_000)
        doc["pad"] = self._pad()
        return doc

    def update(self) -> dict[str, Any]:
        if self._schema_gen is not None:
            fields = self._schema_gen.generate_update_fields()
            return {"$set": fields or {"ts": datetime.now(UTC)}, "$inc": {"n": 1}}
        return {"$set": {"v": self.rng.randrange(1_000_000), "ts": datetime.now(UTC)}, "$inc": {"n": 1}}

    def approx_size(self) -> int:
        if self._schema_gen is not None:
            import bson

            return len(bson.encode(self.new(0)))
        return self.spec.size_bytes
