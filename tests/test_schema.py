from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from mongo_load_tester.config import SchemaConfig, SchemaFieldConfig
from mongo_load_tester.workload.schema import DocumentGenerator


def test_generate_document_produces_all_configured_fields() -> None:
    schema = SchemaConfig(
        fields=[
            SchemaFieldConfig(name="a", field_type="string", length=4),
            SchemaFieldConfig(name="b", field_type="int", min_value=1, max_value=10),
        ]
    )
    gen = DocumentGenerator(schema=schema, rng=random.Random(42))
    doc = gen.generate_document()
    assert set(doc.keys()) == {"a", "b"}
    assert isinstance(doc["a"], str)
    assert isinstance(doc["b"], int)


def test_generate_update_fields_returns_single_random_field() -> None:
    schema = SchemaConfig(
        fields=[
            SchemaFieldConfig(name="x", field_type="string", length=2),
            SchemaFieldConfig(name="y", field_type="int", min_value=0, max_value=1),
        ]
    )
    gen = DocumentGenerator(schema=schema, rng=random.Random(42))
    results = [gen.generate_update_fields() for _ in range(100)]
    for result in results:
        assert len(result) == 1
        assert list(result.keys())[0] in {"x", "y"}
    assert any("x" in r for r in results)
    assert any("y" in r for r in results)


def test_generate_update_fields_with_empty_fields_returns_empty_dict() -> None:
    schema = SchemaConfig(fields=[])
    gen = DocumentGenerator(schema=schema, rng=random.Random(42))
    assert gen.generate_update_fields() == {}


def test_string_generation_respects_length() -> None:
    schema = SchemaConfig(
        fields=[SchemaFieldConfig(name="s", field_type="string", length=7)]
    )
    gen = DocumentGenerator(schema=schema, rng=random.Random(42))
    for _ in range(20):
        doc = gen.generate_document()
        assert len(doc["s"]) == 7
        assert doc["s"].isalnum()


def test_int_generation_uses_floor_and_swaps_inverted_bounds() -> None:
    # math.floor: 5.3 -> 5, 7.8 -> 7, so valid range is [5, 7]
    schema = SchemaConfig(
        fields=[
            SchemaFieldConfig(name="n", field_type="int", min_value=5.3, max_value=7.8)
        ]
    )
    gen = DocumentGenerator(schema=schema, rng=random.Random(42))
    for _ in range(50):
        doc = gen.generate_document()
        assert 5 <= doc["n"] <= 7
        assert isinstance(doc["n"], int)

    # Inverted bounds: floor(7.9)=7, floor(5.1)=5; generator swaps to [5, 7]
    field = SchemaFieldConfig.model_construct(
        name="n",
        field_type="int",
        length=16,
        min_value=7.9,
        max_value=5.1,
        choices=None,
    )
    schema_inverted = SchemaConfig.model_construct(fields=[field])
    gen_inv = DocumentGenerator(schema=schema_inverted, rng=random.Random(42))
    for _ in range(50):
        doc = gen_inv.generate_document()
        assert 5 <= doc["n"] <= 7
        assert isinstance(doc["n"], int)


def test_float_generation_is_within_bounds() -> None:
    schema = SchemaConfig(
        fields=[
            SchemaFieldConfig(name="f", field_type="float", min_value=10.5, max_value=20.5)
        ]
    )
    gen = DocumentGenerator(schema=schema, rng=random.Random(42))
    for _ in range(50):
        doc = gen.generate_document()
        assert 10.5 <= doc["f"] <= 20.5
        assert isinstance(doc["f"], float)


def test_bool_generation_works() -> None:
    schema = SchemaConfig(
        fields=[SchemaFieldConfig(name="flag", field_type="bool")]
    )
    gen = DocumentGenerator(schema=schema, rng=random.Random(42))
    results = [gen.generate_document()["flag"] for _ in range(50)]
    assert all(isinstance(b, bool) for b in results)
    assert True in results
    assert False in results


def test_datetime_generation_returns_utc_datetime() -> None:
    schema = SchemaConfig(
        fields=[SchemaFieldConfig(name="ts", field_type="datetime")]
    )
    gen = DocumentGenerator(schema=schema, rng=random.Random(42))
    doc = gen.generate_document()
    assert isinstance(doc["ts"], datetime)
    assert doc["ts"].tzinfo is UTC


def test_choices_field_picks_from_choices_list() -> None:
    choices = ["alpha", "beta", "gamma"]
    schema = SchemaConfig(
        fields=[
            SchemaFieldConfig(name="c", field_type="string", choices=choices)
        ]
    )
    gen = DocumentGenerator(schema=schema, rng=random.Random(42))
    results = [gen.generate_document()["c"] for _ in range(50)]
    assert all(v in choices for v in results)
    assert len(set(results)) > 1


def test_unsupported_field_type_raises_value_error() -> None:
    field = SchemaFieldConfig.model_construct(
        name="bad",
        field_type="unknown",
        length=16,
        min_value=0,
        max_value=10,
        choices=None,
    )
    schema = SchemaConfig(fields=[field])
    gen = DocumentGenerator(schema=schema, rng=random.Random(42))
    with pytest.raises(ValueError, match="Unsupported schema field type: unknown"):
        gen.generate_document()
