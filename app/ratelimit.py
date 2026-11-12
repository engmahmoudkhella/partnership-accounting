"""In-memory failure counters for password checks (one process, as deployed by systemd).

Every place that checks a password counts failures twice: per account (the normalised username or
the user id) and per client IP. Either one reaching its limit blocks the attempt, even with the right
password, until the window has passed.
"""
from __future__ import annotations

import time
from collections import OrderedDict


class FailureLimiter:
    def __init__(self, limit: int, window: float, max_keys: int = 10_000, clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self.clock = clock
        self._hits: OrderedDict[str, list[float]] = OrderedDict()

    def _recent(self, key: str) -> list[float]:
        now = self.clock()
        hits = [t for t in self._hits.get(key, ()) if now - t < self.window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.limit

    def fail(self, key: str) -> None:
        hits = self._recent(key)
        hits.append(self.clock())
        self._hits[key] = hits
        self._hits.move_to_end(key)
        if len(self._hits) > self.max_keys:
            self._prune()

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def _prune(self) -> None:
        # Never clear everything: flooding new keys must not reset the counter of a targeted account.
        # Drop expired keys, then keys that are not blocked (oldest first); blocked keys go last.
        now = self.clock()
        for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window]:
            del self._hits[k]
        if len(self._hits) > self.max_keys:
            for k in [k for k, v in self._hits.items() if len(v) < self.limit]:
                if len(self._hits) <= self.max_keys:
                    break
                del self._hits[k]
        while len(self._hits) > self.max_keys * 2:  # hard memory cap
            self._hits.popitem(last=False)


def client_ip(request) -> str:
    """The visitor's IP. Behind nginx, uvicorn (proxy_headers) has already put the real address here."""
    return (request.client.host if request.client else "") or "unknown"
