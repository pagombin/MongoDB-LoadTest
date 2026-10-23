"""Key selection: uniform, zipfian (scrambled) and sequential over a growing key space."""

from __future__ import annotations

import math
import random


def fnv1a64(value: int) -> int:
    h = 0xCBF29CE484222325
    for _ in range(8):
        h ^= value & 0xFF
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        value >>= 8
    return h


def _helper1(x: float) -> float:
    return math.log1p(x) / x if abs(x) > 1e-8 else 1 - x * (0.5 - x * (1 / 3 - 0.25 * x))


def _helper2(x: float) -> float:
    return math.expm1(x) / x if abs(x) > 1e-8 else 1 + x * 0.5 * (1 + x / 3 * (1 + 0.25 * x))


class Zipf:
    """Rejection-inversion sampler (Hormann and Derflinger) for ranks 1..n with exponent s.

    Constant time per sample for any n, so the key space can grow without
    recomputing a zeta table.
    """

    def __init__(self, n: int, s: float):
        self.n = max(1, n)
        self.s = s
        self._x1 = self._h_integral(1.5) - 1.0
        self._hn = self._h_integral(self.n + 0.5)
        self._sv = 2.0 - self._h_integral_inverse(self._h_integral(2.5) - self._h(2.0))

    def _h(self, x: float) -> float:
        return math.exp(-self.s * math.log(x))

    def _h_integral(self, x: float) -> float:
        log_x = math.log(x)
        return _helper2((1.0 - self.s) * log_x) * log_x

    def _h_integral_inverse(self, x: float) -> float:
        t = x * (1.0 - self.s)
        if t < -1.0:
            t = -1.0
        return math.exp(_helper1(t) * x)

    def sample(self, rng: random.Random) -> int:
        while True:
            u = self._hn + rng.random() * (self._x1 - self._hn)
            x = self._h_integral_inverse(u)
            k = int(x + 0.5)
            if k < 1:
                k = 1
            elif k > self.n:
                k = self.n
            if k - x <= self._sv or u >= self._h_integral(k + 0.5) - self._h(k):
                return k


class KeyChooser:
    """Chooses existing keys in [0, size) and allocates new keys for inserts.

    Generator process ``index`` of ``count`` allocates insert keys
    ``base + index + count * i`` so processes never collide.
    """

    def __init__(
        self, distribution: str, size: int, rng: random.Random, theta: float = 0.99, process_index: int = 0, process_count: int = 1
    ):
        self.distribution = distribution
        self.rng = rng
        self.theta = theta
        self.size = max(0, size)
        self._zipf: Zipf | None = None
        self._zipf_n = 0
        self._sequence = 0
        self._stride = process_count
        self._next_insert = self.size + process_index

    def existing(self) -> int | None:
        n = self.size
        if n <= 0:
            return None
        if self.distribution == "uniform":
            return self.rng.randrange(n)
        if self.distribution == "sequential":
            key = self._sequence % n
            self._sequence += 1
            return key
        if self._zipf is None or abs(self._zipf_n - n) > max(16, self._zipf_n // 100):
            self._zipf = Zipf(n, self.theta)
            self._zipf_n = n
        rank = self._zipf.sample(self.rng)
        return fnv1a64(rank) % n

    def upsert_key(self) -> int:
        extra = self.size // 10 + 1
        return self.rng.randrange(-extra, max(1, self.size))

    def new_key(self) -> int:
        key = self._next_insert
        self._next_insert += self._stride
        if key >= self.size:
            self.size = key + 1
        return key
