"""In-memory sliding-window rate limits, for endpoints that take no account (telemetry).

Per process: with several uvicorn workers each keeps its own counts, so a client can get up to
`limit × workers` through. That is enough to stop floods and runaway apps; a shared store (Redis)
would be needed for exact global limits."""

import math
import threading
import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    """At most `limit` units per `window` seconds and per key. A hit may cost several units
    (e.g. a batch of 50 events)."""

    def __init__(
        self, limit: int, window: float, *, max_keys: int = 50_000, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._clock = clock
        self._hits: dict[str, deque[tuple[float, int]]] = {}
        self._used: dict[str, int] = {}
        self._lock = threading.Lock()

    def _expire(self, key: str, now: float) -> None:
        q = self._hits.get(key)
        while q and q[0][0] <= now - self.window:
            _, cost = q.popleft()
            self._used[key] -= cost
        if q is not None and not q:
            del self._hits[key]
            del self._used[key]

    def _prune(self, now: float, keep: int) -> None:
        """Bounded memory: drop expired keys; if more than `keep` remain, forget the least recent."""
        for key in list(self._hits):
            self._expire(key, now)
        if len(self._hits) > keep:
            oldest = sorted(self._hits, key=lambda k: self._hits[k][-1][0])
            for key in oldest[: len(self._hits) - keep]:
                del self._hits[key]
                del self._used[key]

    def hit(self, key: str, cost: int = 1) -> float | None:
        """Counts the hit and returns None, or refuses it and returns the seconds to wait."""
        with self._lock:
            now = self._clock()
            self._expire(key, now)
            used = self._used.get(key, 0)
            if used + cost > self.limit:
                q = self._hits.get(key)
                if not q:  # a single hit larger than the whole limit
                    return self.window
                # Wait until enough old hits leave the window.
                needed = used + cost - self.limit
                freed = 0
                for t, c in q:
                    freed += c
                    if freed >= needed:
                        return max(0.0, t + self.window - now)
                return self.window
            if key not in self._hits and len(self._hits) >= self.max_keys:
                # Down to 90 %: the sort runs once per many new keys, not on every one during a flood.
                self._prune(now, keep=max(0, self.max_keys * 9 // 10 - 1))
            self._hits.setdefault(key, deque()).append((now, cost))
            self._used[key] = used + cost
            return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
            self._used.clear()


def retry_seconds(wait: float) -> int:
    return max(1, math.ceil(wait))
