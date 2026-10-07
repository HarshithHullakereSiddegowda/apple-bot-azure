"""In-memory sliding-window rate limiting.

Each key (a visitor's IP, or "global") keeps the timestamps of its recent requests. A request is allowed
only if every window (e.g. 10 per 60 s AND 100 per day) still has room.

Limits are per replica: with --max-replicas 2 the effective ceiling is up to 2x. That is acceptable here.
A shared store (Redis) or an edge service (Azure Front Door / API Management) is the production fix.
"""
import hashlib
import threading
import time
from collections import deque

from fastapi import Request

MAX_KEYS = 10_000  # bound memory: drop idle visitors when this many are tracked


class RateLimiter:
    def __init__(self, limits: list[tuple[int, int]]):
        """limits: [(max_requests, window_seconds), ...], e.g. [(10, 60), (100, 86_400)]."""
        self.limits = limits
        self.longest = max(w for _, w in limits)
        self.hits: dict[str, deque[float]] = {}
        self.lock = threading.Lock()  # sync endpoints run in a thread pool

    def check(self, key: str) -> int | None:
        """Record a hit and return None if allowed; otherwise return seconds to wait (nothing recorded)."""
        now = time.time()
        with self.lock:
            q = self.hits.setdefault(key, deque())
            while q and q[0] <= now - self.longest:
                q.popleft()
            for max_requests, window in self.limits:
                in_window = [t for t in q if t > now - window]
                if len(in_window) >= max_requests:
                    return max(1, int(in_window[0] + window - now) + 1)
            q.append(now)
            if len(self.hits) > MAX_KEYS:
                self._evict_idle(now)
            return None

    def _evict_idle(self, now: float) -> None:
        idle = [k for k, q in self.hits.items() if not q or q[-1] <= now - self.longest]
        for k in idle:
            del self.hits[k]


def client_ip(request: Request) -> str:
    """The visitor's IP as seen by Azure's ingress.

    Behind Container Apps, the ingress appends the address it actually saw to X-Forwarded-For, so the
    LAST entry is trustworthy; earlier entries can be typed in by the client to dodge limits.
    """
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def visitor_id(ip: str) -> str:
    """Short one-way hash of the IP for logs, so raw IP addresses are never stored."""
    return hashlib.sha256(ip.encode()).hexdigest()[:12]
