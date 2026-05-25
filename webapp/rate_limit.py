"""In-memory per-user sliding-window rate limiter.

Deliberately minimal: no Redis, no slowapi dependency, no global middleware.
Call `rate_limit(bucket, user_id, limit, window_seconds)` from any route that
needs throttling and it will raise HTTPException(429) if the user has made
too many calls to that bucket in the rolling window.

Because this is in-memory it's reset on every server restart and doesn't
share state across multiple webapp workers. For our current single-process
uvicorn deployment that's fine. If/when we scale horizontally this should
be swapped for Redis.

Buckets let different endpoints have different limits without interfering
with each other. Example:
    rate_limit("poll_now",      user_id, 2, 60)   # 2 calls/min
    rate_limit("import_history", user_id, 5, 3600) # 5 calls/hour
    rate_limit("chain_detect",  user_id, 20, 60)  # 20 calls/min

Cleanup: old entries are discarded lazily whenever a bucket is touched.
Buckets that haven't been touched for a while never get cleaned up, but the
memory footprint is bounded (one deque per (bucket, user) pair).
"""
from __future__ import annotations

import time
import threading
from collections import defaultdict, deque

from fastapi import HTTPException

_LOCK = threading.Lock()
_STORE: dict[tuple[str, int], deque] = defaultdict(deque)


def rate_limit(bucket: str, user_id: int, limit: int, window_seconds: float) -> None:
    """Raise 429 if the user has exceeded `limit` calls in the last `window_seconds`.

    Otherwise records the current call and returns normally. Fail-open on
    any internal error (we'd rather let a call through than take the whole
    app down over an accounting mistake).
    """
    try:
        now = time.monotonic()
        key = (bucket, int(user_id))
        with _LOCK:
            q = _STORE[key]
            # Drop stale entries outside the window
            cutoff = now - window_seconds
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                retry = max(1, int(window_seconds - (now - q[0])))
                raise HTTPException(
                    429,
                    f"Rate limit: {limit} requests per {int(window_seconds)}s. "
                    f"Retry in {retry}s."
                )
            q.append(now)
    except HTTPException:
        raise
    except Exception as e:
        # Fail-open. Log but don't block — the limiter is best-effort.
        print(f"[ratelimit] internal error (allowing request): {e}")
