import math
import random

import pytest

from ballast.engine.histogram import Histogram, RELATIVE_PRECISION, bucket_bounds, bucket_index, merged


def exact_percentile(values, p):
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


DISTRIBUTIONS = {
    "uniform": lambda r: r.randint(100, 50_000),
    "exponential": lambda r: int(r.expovariate(1 / 2_000)) + 1,
    "lognormal": lambda r: int(r.lognormvariate(7.5, 1.2)),
    "bimodal": lambda r: r.randint(500, 900) if r.random() < 0.97 else r.randint(200_000, 2_000_000),
}


@pytest.mark.parametrize("name", sorted(DISTRIBUTIONS))
def test_percentiles_within_stated_precision(name):
    rng = random.Random(42)
    values = [DISTRIBUTIONS[name](rng) for _ in range(200_000)]
    hist = Histogram()
    for v in values:
        hist.record(v)
    for p in (1, 25, 50, 90, 99, 99.9, 99.99):
        expected = exact_percentile(values, p)
        got = hist.percentile(p)
        assert got >= expected
        assert got - expected <= max(1, expected * RELATIVE_PRECISION), (p, expected, got)
    assert hist.max == max(values)
    assert hist.min == min(values)
    assert hist.total == len(values)
    assert hist.mean == pytest.approx(sum(values) / len(values))


def test_bucket_layout_is_contiguous_and_monotonic():
    previous_high = -1
    for index in range(0, bucket_index(3_600_000_000) + 1):
        low, high = bucket_bounds(index)
        assert low == previous_high + 1
        assert bucket_index(low) == index
        assert bucket_index(high) == index
        assert high - low <= max(0, low * RELATIVE_PRECISION)
        previous_high = high


def test_merge_equals_recording_everything_in_one():
    rng = random.Random(7)
    parts = []
    combined = Histogram()
    for _ in range(10):
        part = Histogram()
        for _ in range(5_000):
            v = int(rng.lognormvariate(8, 1))
            part.record(v)
            combined.record(v)
        parts.append(part)
    total = merged(parts)
    assert total.counts == combined.counts
    for p in (50, 99, 99.9):
        assert total.percentile(p) == combined.percentile(p)


def test_merging_is_not_averaging_percentiles():
    fast = Histogram()
    slow = Histogram()
    for _ in range(990):
        fast.record(1_000)
    for _ in range(10):
        slow.record(1_000_000)
    avg_of_p99 = (fast.percentile(99) + slow.percentile(99)) / 2
    combined = merged([fast, slow])
    assert combined.percentile(99) <= 1_001
    assert combined.percentile(99.5) >= 999_000
    assert avg_of_p99 != combined.percentile(99)


def test_compact_and_encoded_round_trip():
    hist = Histogram()
    for v in (0, 1, 5, 2047, 2048, 123_456, 10**9, 10**12):
        hist.record(v)
    for clone in (Histogram.from_compact(hist.to_compact()), Histogram.decode(hist.encode())):
        assert clone.counts == hist.counts
        assert (clone.total, clone.sum, clone.min, clone.max) == (hist.total, hist.sum, hist.min, hist.max)


def test_empty_histogram_reports_zero():
    hist = Histogram()
    assert hist.percentile(99) == 0
    assert hist.summary()["count"] == 0
    assert Histogram.from_compact(None).total == 0


def test_percentiles_batch_matches_single():
    rng = random.Random(3)
    hist = Histogram()
    for _ in range(10_000):
        hist.record(rng.randint(1, 10**7))
    batch = hist.percentiles((0, 50, 99, 99.9, 100))
    for p, v in batch.items():
        assert v == hist.percentile(p)
