"""Round-robin Moralis API key rotator.

Motivation: Moralis free/compute tiers cap per-key daily requests. When the
portfolio gains real users the single-key setup will hit 429 before anyone's
import finishes. Keeping a pool of keys lets us multiply the effective quota
and survive the occasional single-key rate limit without any user-visible
failure.

Env contract (back-compat):
  MORALIS_API_KEYS  — preferred. Comma-separated list of keys.
  MORALIS_API_KEY   — legacy single-key var. Still honored; merged into
                      the pool if set.

Usage:
    from moralis_keys import get_moralis_key, mark_key_failed

    key = get_moralis_key()
    r = requests.get(url, headers={"X-API-Key": key, "accept": "application/json"})
    if r.status_code in (401, 429):
        mark_key_failed(key)   # 60s cooldown, rotator skips it
        key = get_moralis_key()
        r = requests.get(url, headers={"X-API-Key": key, ...})

Thread-safe: the round-robin pointer is guarded by a lock so multiple scans
running concurrently (history imports + live scans) don't desync.
"""
from __future__ import annotations

import os
import time
import threading
from pathlib import Path

_LOCK = threading.Lock()
_INDEX = 0
_COOLDOWN: dict[str, float] = {}   # key -> unix ts at which it becomes usable again
_COOLDOWN_SECONDS = 60.0


def _find_env() -> Path:
    here = Path(__file__).resolve().parent
    for c in (here / ".env", here.parent / ".env"):
        if c.exists():
            return c
    return here / ".env"


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = _find_env()
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _parse_keys() -> list[str]:
    """Pull the current key pool from env. Re-read on every call so that
    operators can hot-add keys by editing .env without restarting the process
    — the module-level caches in scanner.py / history.py would miss that,
    but by routing every HTTP call through get_moralis_key() we pick up
    changes at the next call.
    """
    env = _load_env()
    # Allow runtime env overrides to take precedence — useful for tests.
    multi = os.getenv("MORALIS_API_KEYS") or env.get("MORALIS_API_KEYS") or ""
    single = os.getenv("MORALIS_API_KEY") or env.get("MORALIS_API_KEY") or ""
    keys: list[str] = []
    if multi:
        keys.extend(k.strip() for k in multi.split(",") if k.strip())
    if single and single not in keys:
        keys.append(single)
    return keys


def get_moralis_key() -> str:
    """Return the next Moralis API key, round-robin, skipping cooled-down keys.

    Falls back to the first-cooldown key if every key is currently cooling,
    so we never return an empty string — worst case the caller sees a 429
    again, marks it failed again, and the overall system keeps making
    progress once the cooldown clears.
    """
    global _INDEX
    keys = _parse_keys()
    if not keys:
        return ""
    now = time.time()
    with _LOCK:
        n = len(keys)
        # Walk up to n keys starting at _INDEX, return the first non-cooling one.
        for _ in range(n):
            key = keys[_INDEX % n]
            _INDEX = (_INDEX + 1) % n
            if _COOLDOWN.get(key, 0) <= now:
                return key
        # Every key is cooling — return the one with the earliest cooldown
        # expiry so the caller at least tries the most-recovered key.
        key = min(keys, key=lambda k: _COOLDOWN.get(k, 0))
        return key


def mark_key_failed(key: str, cooldown: float = _COOLDOWN_SECONDS) -> None:
    """Put a key on ice for `cooldown` seconds. Call this on 401/429/5xx so
    the rotator stops handing out a dead key on every subsequent request.
    """
    if not key:
        return
    with _LOCK:
        _COOLDOWN[key] = time.time() + cooldown


def moralis_headers() -> dict[str, str]:
    """Convenience: ready-to-use request headers using the next available key."""
    return {"X-API-Key": get_moralis_key(), "accept": "application/json"}


def pool_size() -> int:
    """Diagnostics: how many keys are loaded right now."""
    return len(_parse_keys())
