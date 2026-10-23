"""Log-linear latency histogram with the HdrHistogram bucket layout.

Values are integers in microseconds. With 3 significant digits every recorded
value lands in a bucket whose width is at most 1/1024 of the value, so any
reported percentile is within 0.1% of the true sample value (values below
2,048 us are exact). Counts are stored sparsely so per-second histograms stay
small, and histograms merge by adding counts, which keeps percentiles over
long periods exact to the same precision. Percentiles are never averaged.
"""

from __future__ import annotations

import json
import math
import zlib
from array import array
from collections.abc import Iterable, Iterator
from itertools import accumulate

SUB_BUCKET_BITS = 11
SUB_BUCKET_COUNT = 1 << SUB_BUCKET_BITS
SUB_BUCKET_HALF = SUB_BUCKET_COUNT >> 1
HIGHEST_TRACKABLE_US = 3_600_000_000
RELATIVE_PRECISION = 1.0 / SUB_BUCKET_HALF


def bucket_index(value: int) -> int:
    if value < SUB_BUCKET_COUNT:
        return value if value > 0 else 0
    shift = value.bit_length() - SUB_BUCKET_BITS
    return (shift + 1) * SUB_BUCKET_HALF + ((value >> shift) - SUB_BUCKET_HALF)


def bucket_bounds(index: int) -> tuple[int, int]:
    """Lowest and highest value that map to ``index``."""
    if index < SUB_BUCKET_COUNT:
        return index, index
    shift = index // SUB_BUCKET_HALF - 1
    sub = index % SUB_BUCKET_HALF + SUB_BUCKET_HALF
    return sub << shift, ((sub + 1) << shift) - 1


class Histogram:
    """Sparse bucket counts. Histograms read from storage stay packed in two arrays (about 12 bytes per
    bucket instead of about 100 for a dict entry) until something writes to them."""

    __slots__ = ("_counts", "_idx", "_cnt", "total", "sum", "min", "max")

    def __init__(self) -> None:
        self._counts: dict[int, int] | None = {}
        self._idx: array | None = None
        self._cnt: array | None = None
        self.total = 0
        self.sum = 0
        self.min = 0
        self.max = 0

    @property
    def counts(self) -> dict[int, int]:
        if self._counts is None:
            assert self._idx is not None and self._cnt is not None
            self._counts = dict(zip(self._idx, self._cnt, strict=True))
            self._idx = self._cnt = None
        return self._counts

    @counts.setter
    def counts(self, value: dict[int, int]) -> None:
        self._counts, self._idx, self._cnt = value, None, None

    def items(self) -> Iterator[tuple[int, int]]:
        """(bucket index, count) in ascending index order."""
        if self._counts is None:
            assert self._idx is not None and self._cnt is not None
            return zip(self._idx, self._cnt, strict=True)
        return ((i, self._counts[i]) for i in sorted(self._counts))

    def pack(self) -> Histogram:
        """Switch to the compact read-only form; the next write unpacks it again."""
        if self._counts is not None:
            indexes = sorted(self._counts)
            self._idx = array("I", indexes)
            self._cnt = array("Q", (self._counts[i] for i in indexes))
            self._counts = None
        return self

    @property
    def buckets(self) -> int:
        return len(self._counts) if self._counts is not None else len(self._idx or ())

    def record(self, value_us: int | float, count: int = 1) -> None:
        value = int(value_us)
        if value < 0:
            value = 0
        elif value > HIGHEST_TRACKABLE_US:
            value = HIGHEST_TRACKABLE_US
        index = value if value < SUB_BUCKET_COUNT else bucket_index(value)
        counts = self.counts
        counts[index] = counts.get(index, 0) + count
        if self.total == 0 or value < self.min:
            self.min = value
        if value > self.max:
            self.max = value
        self.total += count
        self.sum += value * count

    def merge(self, other: Histogram) -> Histogram:
        if other.total == 0:
            return self
        if self.total == 0 and other._idx is not None and other._cnt is not None:
            self._idx, self._cnt, self._counts = array("I", other._idx), array("Q", other._cnt), None
        else:
            counts = self.counts
            for index, count in other.items():
                counts[index] = counts.get(index, 0) + count
        if self.total == 0 or other.min < self.min:
            self.min = other.min
        if other.max > self.max:
            self.max = other.max
        self.total += other.total
        self.sum += other.sum
        return self

    def copy(self) -> Histogram:
        clone = Histogram()
        if self._idx is not None and self._cnt is not None:
            clone._idx, clone._cnt, clone._counts = array("I", self._idx), array("Q", self._cnt), None
        else:
            clone._counts = dict(self.counts)
        clone.total, clone.sum, clone.min, clone.max = self.total, self.sum, self.min, self.max
        return clone

    @property
    def mean(self) -> float:
        return self.sum / self.total if self.total else 0.0

    def percentile(self, p: float) -> int:
        """Value at percentile ``p`` (0-100), reported as the bucket's highest equivalent value."""
        if self.total == 0:
            return 0
        if p <= 0:
            return self.min
        if p >= 100:
            return self.max
        rank = max(1, math.ceil(p / 100.0 * self.total))
        seen = 0
        for index, count in self.items():
            seen += count
            if seen >= rank:
                return min(bucket_bounds(index)[1], self.max)
        return self.max

    def percentiles(self, ps: Iterable[float]) -> dict[float, int]:
        wanted = sorted(set(ps))
        result: dict[float, int] = {}
        if self.total == 0:
            return {p: 0 for p in wanted}
        ranks = [(p, self.min if p <= 0 else self.max if p >= 100 else max(1, math.ceil(p / 100.0 * self.total))) for p in wanted]
        pending = [(p, r) for p, r in ranks if 0 < p < 100]
        for p, r in ranks:
            if not 0 < p < 100:
                result[p] = r
        seen = 0
        i = 0
        for index, count in self.items():
            seen += count
            while i < len(pending) and seen >= pending[i][1]:
                result[pending[i][0]] = min(bucket_bounds(index)[1], self.max)
                i += 1
            if i >= len(pending):
                break
        for p, _ in pending[i:]:
            result[p] = self.max
        return result

    def to_compact(self) -> list:
        deltas: list[int] = []
        counts: list[int] = []
        previous = 0
        for index, count in self.items():
            deltas.append(index - previous)
            counts.append(count)
            previous = index
        return [self.total, self.sum, self.min, self.max, deltas, counts]

    @classmethod
    def from_compact(cls, data: list | None) -> Histogram:
        hist = cls()
        if not data:
            return hist
        hist.total, hist.sum, hist.min, hist.max = int(data[0]), int(data[1]), int(data[2]), int(data[3])
        if len(data[4]) != len(data[5]):
            raise ValueError("histogram index and count lists differ in length")
        hist._idx, hist._cnt, hist._counts = array("I", accumulate(data[4])), array("Q", data[5]), None
        return hist

    def encode(self) -> bytes:
        return zlib.compress(json.dumps(self.to_compact(), separators=(",", ":")).encode())

    @classmethod
    def decode(cls, blob: bytes) -> Histogram:
        return cls.from_compact(json.loads(zlib.decompress(blob)))

    def summary(self) -> dict[str, float]:
        values = self.percentiles((50, 90, 99, 99.9))
        return {
            "count": self.total,
            "mean_us": round(self.mean, 1),
            "min_us": self.min,
            "p50_us": values[50],
            "p90_us": values[90],
            "p99_us": values[99],
            "p999_us": values[99.9],
            "max_us": self.max,
        }


def merged(histograms: Iterable[Histogram]) -> Histogram:
    result = Histogram()
    for hist in histograms:
        result.merge(hist)
    return result
