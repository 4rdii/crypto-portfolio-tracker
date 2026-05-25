"""Birdeye historical SPL token price fetcher.

Why Birdeye instead of CoinGecko or Binance for Solana tokens?
---------------------------------------------------------------
Binance klines only cover exchange-listed tokens — a great fit for ETH/BTC/SOL
majors, but useless for Solana-native tokens like BONK, JUP, RAY, or ORCA that
trade primarily on DEXes (Jupiter/Raydium/Orca) rather than centralized venues.
CoinGecko covers more tokens via its /coins/{id}/history endpoint but: (a) it
requires a symbol→CoinGecko-ID mapping that goes stale as new tokens launch,
and (b) its free-tier rate limit (30 req/min) causes real back-pressure during
bulk imports.

Birdeye solves both problems for Solana: it indexes every SPL token via mint
address (no ID mapping needed), provides 15-minute OHLC candles going back
to token launch, and the public API gives enough headroom for typical import
workloads. The downside is an API key requirement (BIRDEYE_API_KEY in .env) and
the absence of EVM chain coverage — so Birdeye is a SOLANA-ONLY price source
that slots into sol_history.py's _resolve_price waterfall.

Rate-limit contract
-------------------
Birdeye's public tier allows roughly 100 req/min on the history_price endpoint.
We implement: exponential backoff (2s → 4s → 8s) on 429, up to 3 retries.
A 401/403 response means the key is bad — we log once and stop retrying for the
entire Python process lifetime (module-level flag). This prevents a bad key from
burning 3×N retries across every price lookup in a bulk import.

Cache contract
--------------
Resolved prices are stored in the same `price_cache` SQLite table used by
binance_prices and history.py, under the composite key 'sol:{mint}:{YYYY-MM-DD}'.
The 'sol:' prefix namespaces Birdeye rows away from symbol-keyed Binance rows so
the two sources never collide even if a symbol appears on both. Cache misses on
the same (mint, date) triple hit Birdeye once and then never again — safe to call
in a loop without burning API quota on repeat imports.
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from applog import log

# ---------------------------------------------------------------------------
# Environment loading — mirrors scanner.py's pattern exactly
# ---------------------------------------------------------------------------

def _find_env() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here / ".env", here.parent / ".env"):
        if candidate.exists():
            return candidate
    return here / ".env"


def _load_env() -> dict[str, str]:
    path = _find_env()
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


_ENV = _load_env()
# Never log this — it's a secret key.
BIRDEYE_API_KEY: str = _ENV.get("BIRDEYE_API_KEY", os.getenv("BIRDEYE_API_KEY", ""))

# ---------------------------------------------------------------------------
# HTTP session with connection pooling + sane headers
# ---------------------------------------------------------------------------

_SESSION = requests.Session()
_SESSION.headers.update({
    "accept": "application/json",
    "x-chain": "solana",
})

# Guard flag: if we ever receive a 401 or 403, the key is invalid for this
# session. Set to True once and we stop calling Birdeye entirely — no point
# burning retries on a bad key that will reject every subsequent request too.
_KEY_INVALID = False

# ---------------------------------------------------------------------------
# SQLite cache — shared with binance_prices / history
# ---------------------------------------------------------------------------

_CACHE_DB = Path(__file__).resolve().parent / "portfolio.db"


def _ensure_cache_table() -> None:
    """Create price_cache if it doesn't exist yet.

    Called lazily before any read/write — safe to call repeatedly because
    CREATE TABLE IF NOT EXISTS is a no-op when the table already exists.
    This avoids an import-time DB connection that could race with the main
    app's init_db() during startup.
    """
    with sqlite3.connect(_CACHE_DB) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS price_cache (
                symbol TEXT NOT NULL,
                date   TEXT NOT NULL,
                price  REAL NOT NULL,
                PRIMARY KEY (symbol, date)
            )"""
        )


def _cache_key(mint: str, date: str) -> str:
    """Build the price_cache key for a Birdeye-sourced SPL price.

    The 'sol:' prefix namespaces Birdeye rows so they never collide with
    the symbol-keyed rows that binance_prices.py writes (which use the
    canonical symbol like 'BTC' or 'SOL' as the key). Without the prefix
    a Birdeye lookup for the USDC mint would write key='EPjFW...:2024-05-01'
    and a Binance lookup for USDC would later REPLACE it with key='USDC:2024-05-01' —
    the two namespaces never overlap, so both can coexist in the same table.
    """
    return f"sol:{mint}:{date}"


def _cached(mint: str, date: str) -> float | None:
    """Return a cached price or None if not yet resolved."""
    key = _cache_key(mint, date)
    with sqlite3.connect(_CACHE_DB) as c:
        row = c.execute(
            "SELECT price FROM price_cache WHERE symbol = ? AND date = ?",
            (key, date),
        ).fetchone()
        return float(row[0]) if row else None


def _store(mint: str, date: str, price: float) -> None:
    """Write a resolved price to cache. Only positive prices are stored —
    a zero/None result from Birdeye might be transient (new token, thin
    order book) so we leave it uncached to allow retry on next import."""
    if not price or price <= 0:
        return
    key = _cache_key(mint, date)
    with sqlite3.connect(_CACHE_DB) as c:
        c.execute(
            "INSERT OR REPLACE INTO price_cache VALUES (?, ?, ?)",
            (key, date, price),
        )


# ---------------------------------------------------------------------------
# Birdeye API call
# ---------------------------------------------------------------------------

_BIRDEYE_BASE = "https://public-api.birdeye.so/defi/history_price"

# How wide a window (in seconds) to query around the target timestamp.
# 900 seconds = 15 minutes = one candle interval. Querying [ts-900, ts+900]
# guarantees at least one complete 15m candle contains our timestamp.
_WINDOW_SEC = 900


def get_birdeye_historical_price(mint: str, unix_ts: int) -> float | None:
    """Fetch the closest historical USD price for an SPL token from Birdeye.

    Queries the 15-minute OHLC endpoint for a ±15-minute window around
    `unix_ts`. Returns the close price of the candle whose closeTime is
    nearest to the requested timestamp, or None on any failure.

    The 15m interval is the finest granularity on Birdeye's public tier and
    gives us per-trade accuracy for all but the most rapid-fire bot activity.
    For tax purposes (where the cost basis just needs to be "reasonable") this
    is more than sufficient.

    Error handling:
      401/403 — key invalid; log once, set module-level flag, return None.
      429      — rate limited; exponential backoff 2→4→8s, max 3 retries.
      5xx      — Birdeye server error; retry 3x, then return None.
      network  — requests.RequestException; retry 3x, then return None.
    """
    global _KEY_INVALID
    if _KEY_INVALID:
        # Key was already rejected this session — stop hammering the API.
        return None
    if not BIRDEYE_API_KEY:
        log.debug("BIRDEYE_API_KEY not set — skipping Birdeye lookup")
        return None

    params = {
        "address": mint,
        "address_type": "token",
        "type": "15m",
        "time_from": unix_ts - _WINDOW_SEC,
        "time_to": unix_ts + _WINDOW_SEC,
    }
    headers = {"X-API-KEY": BIRDEYE_API_KEY, "x-chain": "solana"}

    max_retries = 3
    delay = 2.0  # seconds; doubles each retry on 429

    for attempt in range(max_retries):
        try:
            r = _SESSION.get(_BIRDEYE_BASE, params=params, headers=headers, timeout=20)
        except requests.RequestException as exc:
            log.warning("Birdeye network error (attempt %d/%d): %s", attempt + 1, max_retries, exc)
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
            continue

        if r.status_code in (401, 403):
            # Bad key — log once, never retry. Log the status code only,
            # not the key value.
            log.error(
                "Birdeye returned %d — API key may be invalid or expired. "
                "Check BIRDEYE_API_KEY in .env. Disabling Birdeye for this session.",
                r.status_code,
            )
            _KEY_INVALID = True
            return None

        if r.status_code == 429:
            log.warning(
                "Birdeye rate-limited (attempt %d/%d) — backing off %.0fs",
                attempt + 1, max_retries, delay,
            )
            time.sleep(delay)
            delay *= 2
            continue

        if r.status_code >= 500:
            log.warning(
                "Birdeye server error %d (attempt %d/%d)",
                r.status_code, attempt + 1, max_retries,
            )
            if attempt < max_retries - 1:
                time.sleep(delay)
                delay *= 2
            continue

        if r.status_code != 200:
            log.warning("Birdeye unexpected status %d for mint %s", r.status_code, mint)
            return None

        try:
            data = r.json()
        except ValueError:
            log.warning("Birdeye returned non-JSON for mint %s", mint)
            return None

        items = (data.get("data") or {}).get("items") or []
        if not items:
            return None

        # Pick the candle whose unixTime is closest to the requested ts.
        # Each item has {"unixTime": int, "value": float} (close price).
        best = min(items, key=lambda x: abs(int(x.get("unixTime") or 0) - unix_ts))
        price = float(best.get("value") or 0)
        return price if price > 0 else None

    # All retries exhausted
    return None


# ---------------------------------------------------------------------------
# Public API: cached price lookup
# ---------------------------------------------------------------------------

def get_sol_price_cached(mint: str, unix_ts: int) -> float | None:
    """Return the USD price for `mint` at `unix_ts`, using the SQLite cache.

    This is the function sol_history.py calls for price resolution. It follows
    the standard read-cache → fetch → write-cache pattern:

      1. Convert unix_ts to a YYYY-MM-DD date for the cache key.
      2. Check price_cache. Hit → return immediately (no network).
      3. Miss → call get_birdeye_historical_price for a precise per-tx price.
      4. Store the result and return.

    Why daily granularity for the cache key when we query 15m candles?
    Because the Birdeye API is mint-specific: we can't reuse one call across
    all rows the way binance_prices groups by (symbol, date) and pulls 288
    candles at once. Each Birdeye request targets a specific ±15m window
    around a single timestamp. A daily cache avoids re-querying Birdeye if
    the user re-imports the same wallet within the same calendar day — the
    intraday accuracy of the first lookup is cached under the date key, and
    subsequent rows on the same day use that cached value rather than
    firing individual API calls. This is a reasonable accuracy tradeoff:
    price movement within a day is typically ±5-15% for liquid tokens, but
    that's acceptable for cost basis purposes.
    """
    _ensure_cache_table()
    date = datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    cached = _cached(mint, date)
    if cached is not None:
        return cached

    price = get_birdeye_historical_price(mint, unix_ts)
    if price is not None and price > 0:
        _store(mint, date, price)
    return price
