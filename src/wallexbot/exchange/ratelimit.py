"""Token bucket rate limiter (default: 10 order creates per 10 s) with an injectable clock."""
from __future__ import annotations

import time
from typing import Callable


class TokenBucket:
    def __init__(self, capacity: int = 10, period_s: float = 10.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if capacity <= 0 or period_s <= 0:
            raise ValueError("capacity and period_s must be positive")
        self.capacity = capacity
        self.rate = capacity / period_s  # tokens per second
        self._clock = clock
        self._tokens = float(capacity)
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)  # a clock going backwards never adds tokens
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self.rate)
        self._last = now

    def try_acquire(self, n: int = 1) -> bool:
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    def available(self) -> float:
        self._refill()
        return self._tokens
