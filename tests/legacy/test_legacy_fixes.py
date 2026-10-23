from mongo_load_tester.config import SchemaConfig
from mongo_load_tester.engine.metrics import _categorize_error
from mongo_load_tester.workload.registry import SchemaRegistry
import random


def test_auth_substring_is_not_classified_as_auth():
    assert _categorize_error("bad option authSource=admin in uri") != "auth"
    assert _categorize_error("Authentication failed.") == "auth"
    assert _categorize_error("not authorized on db to execute command") == "auth"


def test_default_generator_is_cached_per_namespace():
    registry = SchemaRegistry(SchemaConfig())
    rng = random.Random(1)
    first = registry.get_generator("db", "c1", rng)
    assert registry.get_generator("db", "c1", rng) is first
    assert registry.get_generator("db", "c2", rng) is not first


def test_override_seed_is_stable_across_instances():
    docs = []
    for _ in range(2):
        registry = SchemaRegistry(SchemaConfig(), base_seed=7)
        registry.register("db", "c1", SchemaConfig())
        docs.append(registry.get_generator("db", "c1", random.Random(0)).generate_document()["tenant_id"])
    assert docs[0] == docs[1]
