"""Reconstruct daily portfolio value from transaction history + historical prices.

Walks through every transaction chronologically to build a per-day balance ledger,
then prices each day's balances using the price_cache table (populated by the
history importer + the Binance daily-close backfill) with last-known-price
carry-forward for days between txs.

Returns a time series suitable for the dashboard trend chart — replaces the old
"periodic snapshots" approach, which was inaccurate (only captured scan times)
and gap-prone (no history before the user started using the app).

Freshness:
    `ensure_fresh_price_cache()` runs a throttled Binance backfill for every
    held symbol, so the chart reflects real daily market closes between
    trades instead of a stale carry-forward from the last imported tx.
    Throttled per-symbol so repeat dashboard loads are cheap.
"""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pnl import STABLECOINS, BUY_TYPES, SELL_TYPES

DB_PATH = Path(__file__).resolve().parent / "portfolio.db"


def _ensure_backfill_meta_table() -> None:
    """Metadata table tracking when each symbol's Binance backfill last ran.

    Used to throttle the per-symbol refresh — 6h is plenty for a chart that
    shows daily closes, and it keeps dashboard loads cheap.
    """
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS binance_backfill_runs (
                symbol      TEXT PRIMARY KEY,
                last_run_ts REAL NOT NULL
            )"""
        )


def _held_symbols_for_user(user_id: int) -> list[str]:
    """Every symbol this user has ever transacted in, uppercased."""
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT DISTINCT UPPER(symbol) FROM transactions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return [r[0] for r in rows if r[0]]


def ensure_fresh_price_cache(
    user_id: int,
    lookback_days: int = 365,
    throttle_hours: float = 6.0,
) -> dict[str, int]:
    """Backfill the SQLite price cache with up-to-date daily closes.

    For every symbol the user has ever transacted in AND which we can map
    to a Binance spot pair, runs `binance_prices.backfill_daily_series` —
    a single HTTP call that returns up to 1000 daily candles. Results are
    written to `price_cache` by symbol+date.

    Throttled per-symbol via `binance_backfill_runs`: if the same symbol
    was refreshed less than `throttle_hours` ago, we skip it. This keeps
    dashboard loads cheap while still guaranteeing the chart is never
    more than `throttle_hours` stale between refreshes.

    Returns {symbol: rows_written} for the symbols that actually ran.
    Symbols skipped due to throttle or missing Binance coverage are
    omitted from the result. Exceptions are swallowed per-symbol so one
    bad HTTP call doesn't kill the whole refresh.
    """
    try:
        from binance_prices import backfill_daily_series, pair_for_symbol, canonical_symbol
    except Exception as e:
        print(f"[portfolio_history] binance backfill unavailable: {e}")
        return {}

    _ensure_backfill_meta_table()

    held = _held_symbols_for_user(user_id)
    if not held:
        return {}

    # Collapse wrapped variants down to their underlying so we don't
    # backfill BTC twice when the user holds both BTC and WBTC. The
    # canonical key is what gets stored in price_cache AND what we use
    # as the throttle meta key — so re-imports are consistent across
    # variants too.
    canonical_held: list[str] = []
    seen: set[str] = set()
    for sym in held:
        canon = canonical_symbol(sym)
        if canon in seen:
            continue
        seen.add(canon)
        canonical_held.append(canon)

    now_ts = time.time()
    throttle_s = throttle_hours * 3600.0
    start_date = (
        datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")

    refreshed: dict[str, int] = {}
    with sqlite3.connect(DB_PATH) as c:
        last_runs = dict(
            c.execute("SELECT symbol, last_run_ts FROM binance_backfill_runs").fetchall()
        )

    for sym in canonical_held:
        if not pair_for_symbol(sym):
            # Long-tail token, not on Binance — Moralis/CG will have to
            # cover it at import time. Skip silently.
            continue
        last = float(last_runs.get(sym) or 0)
        if last and (now_ts - last) < throttle_s:
            continue
        try:
            wrote = backfill_daily_series(sym, start_date)
        except Exception as e:
            print(f"[portfolio_history] backfill {sym} failed: {e}")
            continue
        refreshed[sym] = wrote
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "INSERT OR REPLACE INTO binance_backfill_runs VALUES (?, ?)",
                (sym, now_ts),
            )

    return refreshed


def _parse_date(s: str) -> datetime:
    """Normalize any ISO-ish timestamp down to a UTC date."""
    s = str(s)
    if "T" in s:
        s2 = s.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s2).astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        except Exception:
            pass
    # Fallback: bare YYYY-MM-DD or "YYYY-MM-DD HH:MM:SS"
    try:
        return datetime.fromisoformat(s[:19].replace(" ", "T"))
    except Exception:
        return datetime.fromisoformat(s[:10])


def _load_price_history() -> dict[str, list[tuple[str, float]]]:
    """Load all cached historical prices, grouped by symbol, sorted by date ASC.

    Returns {SYMBOL: [(YYYY-MM-DD, price), ...]}.
    """
    by_sym: dict[str, list[tuple[str, float]]] = defaultdict(list)
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT symbol, date, price FROM price_cache WHERE price > 0 ORDER BY symbol, date ASC"
        ).fetchall()
    for sym, date, price in rows:
        by_sym[sym.upper()].append((date, float(price)))
    return by_sym


def _current_prices() -> dict[str, float]:
    """Pull the latest market price the scanner recorded for every held symbol.

    Used as the price for "today" (no historical entry exists for the current day
    yet) and as the carry-forward anchor for any symbol that only ever had one
    recorded price.
    """
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            """SELECT UPPER(symbol) AS symbol, MAX(usd_price) AS price
               FROM holdings
               WHERE usd_price > 0
               GROUP BY UPPER(symbol)"""
        ).fetchall()
    return {r[0]: float(r[1]) for r in rows if r[1]}


def _canonical(sym: str) -> str:
    """Collapse wrapped variants to their underlying for cache lookups.

    Lazy-imported so this module keeps working even if binance_prices fails
    to load. See binance_prices._CANONICAL_SYMBOL for the full map.
    """
    try:
        from binance_prices import canonical_symbol
        return canonical_symbol(sym)
    except Exception:
        return (sym or "").upper()


def _price_for_day(sym: str, day: str, price_history: dict, current: dict) -> float:
    """Last-known-price lookup: the most recent cached price on or before `day`.

    Falls back to the current market price if we have no historical data at all
    (e.g. the user added a brand-new token via manual entry with no price).

    Canonicalizes the symbol first — WBTC holdings read BTC's cached price,
    WETH holdings read ETH's, etc. This is the whole point of the wrapped
    token aliasing: the user's wallet still *lists* WBTC, but the chart
    reads one consolidated BTC price row.
    """
    symu = sym.upper()
    if symu in STABLECOINS:
        return 1.0
    canon = _canonical(symu)
    series = price_history.get(canon) or price_history.get(symu) or []
    if not series:
        return current.get(canon) or current.get(symu, 0.0)
    # Binary search would be faster but series are tiny (days, not years of ticks)
    last = 0.0
    for d, p in series:
        if d > day:
            break
        last = p
    if last > 0:
        return last
    # Day is earlier than the first known price — use the first known price as
    # the best approximation (better than $0 which would flatten the chart)
    return series[0][1] if series else current.get(symu, 0.0)


def _load_transactions(user_id: int) -> list[dict]:
    """Load every transaction row for a user with the fields we need for ledger math."""
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT ts, tx_type, symbol, amount FROM transactions WHERE user_id = ? ORDER BY ts ASC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def _current_prices_for_user(user_id: int) -> dict[str, float]:
    """Latest market prices the scanner recorded for this user's holdings."""
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            """SELECT UPPER(symbol) AS symbol, MAX(usd_price) AS price
               FROM holdings
               WHERE user_id = ? AND usd_price > 0
               GROUP BY UPPER(symbol)""",
            (user_id,),
        ).fetchall()
    return {r[0]: float(r[1]) for r in rows if r[1]}


def reconstruct_daily_history(
    user_id: int,
    lookback_days: int = 365,
    include_today_current: bool = True,
) -> list[dict]:
    """Rebuild the full daily portfolio value curve.

    Algorithm:
      1. Walk every transaction chronologically, maintaining a running balance
         ledger (symbol → balance).
      2. Emit the end-of-day snapshot at every date that had activity AND at
         every date in between (so the chart is dense, not sparse).
      3. Price each day's balances using the historical price cache with
         last-known-price carry-forward between known points.
      4. Clamp to `lookback_days` from today so the chart stays readable.

    Returns list of {date: "YYYY-MM-DD", total_usd: float}.
    """
    txs = _load_transactions(user_id)
    if not txs:
        return []

    price_history = _load_price_history()
    current = _current_prices_for_user(user_id)

    # Find the earliest tx date — our history starts there
    first_day = _parse_date(txs[0]["ts"])
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    start = max(first_day, today - timedelta(days=lookback_days))

    # Walk every tx and bucket by date
    txs_by_date: dict[str, list[dict]] = defaultdict(list)
    for t in txs:
        d = _parse_date(t["ts"])
        txs_by_date[d.strftime("%Y-%m-%d")].append(t)

    # Pre-compute the opening balance at `start` (apply every tx with date < start)
    balance: dict[str, float] = defaultdict(float)
    start_key = start.strftime("%Y-%m-%d")
    for t in txs:
        d = _parse_date(t["ts"]).strftime("%Y-%m-%d")
        if d >= start_key:
            break
        _apply_tx(balance, t)

    # Walk day-by-day from start to today, applying any txs that day and then
    # snapshotting the end-of-day value
    out: list[dict] = []
    cursor = start
    while cursor <= today:
        day_key = cursor.strftime("%Y-%m-%d")
        for t in txs_by_date.get(day_key, []):
            _apply_tx(balance, t)
        # Compute total USD value using historical prices
        total = 0.0
        for sym, bal in balance.items():
            if bal <= 0:
                continue
            # For "today" we prefer the live market price if available
            if include_today_current and day_key == today.strftime("%Y-%m-%d"):
                p = current.get(sym.upper()) or _price_for_day(sym, day_key, price_history, current)
            else:
                p = _price_for_day(sym, day_key, price_history, current)
            total += bal * p
        out.append({"date": day_key, "total_usd": round(total, 2)})
        cursor += timedelta(days=1)

    return out


def _apply_tx(balance: dict, tx: dict) -> None:
    """Mutate the balance ledger for one transaction."""
    sym = (tx.get("symbol") or "").upper()
    if not sym:
        return
    amt = float(tx.get("amount") or 0)
    ttype = (tx.get("tx_type") or "").lower()
    if ttype in BUY_TYPES:
        balance[sym] = balance.get(sym, 0.0) + amt
    elif ttype in SELL_TYPES:
        balance[sym] = max(0.0, balance.get(sym, 0.0) - amt)
    elif ttype == "swap":
        # Single-row swap: treat as a reduction (the receive leg should appear
        # as a separate tx in Moralis history)
        balance[sym] = max(0.0, balance.get(sym, 0.0) - amt)
