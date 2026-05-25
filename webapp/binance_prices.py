"""Binance public market-data price fetcher.

Why this module exists
----------------------
Moralis' `/erc20/{contract}/price?to_block=N` endpoint is the most accurate
per-block price source for long-tail DEX tokens, but it costs compute-units
per call. For wallets that trade mostly liquid assets (ETH, BTC, SOL, majors,
L1/L2 tokens, top-200 alts), every one of those Moralis calls is wasted —
Binance has the same data on its free public klines endpoint with no auth
and no CU cost.

This module slots into `history._resolve_price` BEFORE the Moralis fallback:

    stablecoin → price_cache → Binance → Moralis → CoinGecko → current market

…and also exposes `backfill_daily_series` which pulls up to 1000 days of
closes for a symbol in a single HTTP request. The portfolio chart calls
that so the value curve reflects real daily market closes between trades
instead of a flat carry-forward of the last imported price.

API used
--------
- `GET /api/v3/klines` (public, no key required)
  params: symbol, interval=1d, startTime (ms), endTime (ms), limit (<=1000)
  returns: [[openTime, open, high, low, close, volume, closeTime, ...], ...]
  docs: https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints

Rate limits
-----------
Binance caps at ~1200 weight/min per IP for public endpoints. Klines is
weight=2 per request → ~600 req/min headroom, which is several orders of
magnitude beyond what we need for price backfills. We still add a defensive
0.05s sleep between batches.

Symbol mapping
--------------
Not every token in a user's wallet corresponds 1:1 to a Binance pair. Our
static `_BINANCE_PAIR_MAP` normalizes things like WETH→ETHUSDT, WBTC→BTCUSDT,
STETH→ETHUSDT. Unmapped symbols return 0 from the lookup so the caller
falls through to the next price source (Moralis / CoinGecko).
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

CACHE_DB = Path(__file__).resolve().parent / "portfolio.db"

# Binance public REST host. We use /api/v3/klines; no auth header.
_BINANCE_BASE = "https://api.binance.com/api/v3/klines"

# Session with a sane User-Agent — Binance will 418 you for looking like a
# naked scraper on enough volume.
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "portfolio-tracker/1.0 (+https://github.com/x4rde)",
    "Accept": "application/json",
})

# Static symbol → Binance spot pair map. USDT pairs are the most liquid and
# the longest-running (some USDC pairs only exist post-2022). We intentionally
# map wrapped / liquid-staked variants back to their underlying pair — users
# who hold WETH/STETH/WBTC want ETH/BTC prices, not the negligible staking
# premium/discount which is <0.5% and doesn't move the portfolio chart in
# any meaningful way.
#
# Keep entries uppercased — the caller always normalizes via symbol.upper().
_BINANCE_PAIR_MAP: dict[str, str] = {
    # ---- Majors ----
    "BTC": "BTCUSDT",
    "WBTC": "BTCUSDT",
    "CBBTC": "BTCUSDT",   # Coinbase-wrapped BTC tracks BTC 1:1
    "TBTC": "BTCUSDT",    # threshold BTC
    "ETH": "ETHUSDT",
    "WETH": "ETHUSDT",
    "STETH": "ETHUSDT",
    "WSTETH": "ETHUSDT",
    "RETH": "ETHUSDT",    # Rocket Pool ETH — near 1:1 w/ ETH price
    "CBETH": "ETHUSDT",
    "WEETH": "ETHUSDT",
    "EZETH": "ETHUSDT",
    "RSETH": "ETHUSDT",
    "PUFETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "WSOL": "SOLUSDT",
    "JITOSOL": "SOLUSDT", # liquid-staked SOL ≈ SOL
    "MSOL": "SOLUSDT",
    "BNSOL": "SOLUSDT",
    "BNB": "BNBUSDT",
    "WBNB": "BNBUSDT",
    "MATIC": "MATICUSDT",
    "POL": "POLUSDT",     # MATIC→POL rename, Binance carries both
    "WMATIC": "MATICUSDT",
    "AVAX": "AVAXUSDT",
    "WAVAX": "AVAXUSDT",

    # ---- L1s ----
    "TRX": "TRXUSDT",
    "XRP": "XRPUSDT",
    "CBXRP": "XRPUSDT",   # Coinbase wrapped XRP
    "ADA": "ADAUSDT",
    "DOGE": "DOGEUSDT",
    "TON": "TONUSDT",
    "LTC": "LTCUSDT",
    "BCH": "BCHUSDT",
    "ICP": "ICPUSDT",
    "FIL": "FILUSDT",
    "HBAR": "HBARUSDT",
    "DOT": "DOTUSDT",
    "ATOM": "ATOMUSDT",
    "NEAR": "NEARUSDT",
    "APT": "APTUSDT",
    "SUI": "SUIUSDT",
    "TIA": "TIAUSDT",
    "SEI": "SEIUSDT",
    "INJ": "INJUSDT",
    "KAS": "KASUSDT",
    "ALGO": "ALGOUSDT",
    "XLM": "XLMUSDT",
    "XTZ": "XTZUSDT",
    "EGLD": "EGLDUSDT",
    "FLOW": "FLOWUSDT",
    "KAVA": "KAVAUSDT",
    "MINA": "MINAUSDT",

    # ---- L2 / rollup tokens ----
    "ARB": "ARBUSDT",
    "OP": "OPUSDT",
    "STRK": "STRKUSDT",
    "ZK": "ZKUSDT",
    "BLAST": "BLASTUSDT",
    "MNT": "MNTUSDT",     # Mantle
    "METIS": "METISUSDT",
    "MANTA": "MANTAUSDT",

    # ---- DeFi blue chips ----
    "UNI": "UNIUSDT",
    "AAVE": "AAVEUSDT",
    "COMP": "COMPUSDT",
    "CRV": "CRVUSDT",
    "CVX": "CVXUSDT",
    "MKR": "MKRUSDT",
    "SNX": "SNXUSDT",
    "SUSHI": "SUSHIUSDT",
    "1INCH": "1INCHUSDT",
    "BAL": "BALUSDT",
    "GMX": "GMXUSDT",
    "DYDX": "DYDXUSDT",
    "FXS": "FXSUSDT",
    "YFI": "YFIUSDT",
    "LDO": "LDOUSDT",
    "RPL": "RPLUSDT",
    "LINK": "LINKUSDT",
    "PENDLE": "PENDLEUSDT",
    "ENA": "ENAUSDT",
    "EIGEN": "EIGENUSDT",
    "MORPHO": "MORPHOUSDT",
    "ETHFI": "ETHFIUSDT",
    "REZ": "REZUSDT",
    "OMNI": "OMNIUSDT",

    # ---- Solana ecosystem ----
    "JUP": "JUPUSDT",
    "JTO": "JTOUSDT",
    "W": "WUSDT",
    "TNSR": "TNSRUSDT",
    "PYTH": "PYTHUSDT",
    "DRIFT": "DRIFTUSDT",
    "KMNO": "KMNOUSDT",
    "RAY": "RAYUSDT",
    "ORCA": "ORCAUSDT",
    "RENDER": "RENDERUSDT",
    "RNDR": "RENDERUSDT",  # old ticker, same asset post-migration
    "FARTCOIN": "FARTCOINUSDT",

    # ---- Memes ----
    "PEPE": "PEPEUSDT",
    "SHIB": "SHIBUSDT",
    "FLOKI": "FLOKIUSDT",
    "BONK": "BONKUSDT",
    "WIF": "WIFUSDT",
    "BRETT": "BRETTUSDT",
    "DEGEN": "DEGENUSDT",
    "POPCAT": "POPCATUSDT",
    "MOG": "MOGUSDT",
    "NEIRO": "NEIROUSDT",
    "TURBO": "TURBOUSDT",
    "TRUMP": "TRUMPUSDT",
    "MEME": "MEMEUSDT",
    "ACT": "ACTUSDT",
    "GOAT": "GOATUSDT",
    "PNUT": "PNUTUSDT",
    "PUMP": "PUMPUSDT",

    # ---- Base / Aerodrome ecosystem ----
    "AERO": "AEROUSDT",
    "WELL": "WELLUSDT",
    "TOSHI": "TOSHIUSDT",

    # ---- Farming / RWA / gaming ----
    "CAKE": "CAKEUSDT",
    "RDNT": "RDNTUSDT",
    "MAGIC": "MAGICUSDT",
    "GNS": "GNSUSDT",
    "FET": "FETUSDT",
    "OCEAN": "OCEANUSDT",
    "GRT": "GRTUSDT",
    "AKT": "AKTUSDT",
    "THETA": "THETAUSDT",
    "IMX": "IMXUSDT",
    "SAND": "SANDUSDT",
    "MANA": "MANAUSDT",
    "AXS": "AXSUSDT",
    "GALA": "GALAUSDT",
    "ENJ": "ENJUSDT",
    "CHR": "CHRUSDT",
    "HIGH": "HIGHUSDT",
    "ONDO": "ONDOUSDT",
    "ENS": "ENSUSDT",
    "ZRO": "ZROUSDT",
    "NOT": "NOTUSDT",
    "DOG": "DOGUSDT",
    "WLD": "WLDUSDT",
    "PEOPLE": "PEOPLEUSDT",
    "APE": "APEUSDT",
    "BLUR": "BLURUSDT",
    "BOME": "BOMEUSDT",
    "ORDI": "ORDIUSDT",
    "SATS": "SATSUSDT",
    "RATS": "RATSUSDT",
    "SAGA": "SAGAUSDT",
    "ZETA": "ZETAUSDT",
    "PORTAL": "PORTALUSDT",
    "TAO": "TAOUSDT",

    # ---- Commodity-backed ----
    "PAXG": "PAXGUSDT",
}


# ---- Wrapped / liquid-staked token canonicalization ----------------------
#
# A portfolio that holds WBTC, BTCB, cbBTC and TBTC has FOUR separate symbols
# that all track BTC 1:1 (give or take 0.1% wrapping premium). Storing four
# independent cache rows per day is wasteful and — worse — lets them drift
# apart depending on which row was filled by Binance vs CoinGecko vs a stale
# holdings snapshot, which the dashboard then renders as four different value
# curves that don't reconcile.
#
# The fix: collapse every wrapped/liquid-staked variant down to its canonical
# underlying symbol at the cache read/write boundary. The user still SEES
# WBTC in their wallet listing (holdings are stored separately, untouched),
# but the price lookup always asks for BTC. This gives us:
#   - one cache row per (underlying, date) instead of N
#   - perfect consistency across variants (they literally read the same cell)
#   - zero extra Binance calls (the pair map was already collapsing them to
#     BTCUSDT/ETHUSDT, we're just now sharing the storage key too)
#
# If a wrapped variant ever meaningfully de-pegs (e.g. UST-style depeg of
# a liquid-staking token) we can remove it from this map — the cache falls
# back to storing it under its own symbol and the variant gets priced
# independently.
_CANONICAL_SYMBOL: dict[str, str] = {
    # BTC family
    "WBTC":   "BTC",
    "CBBTC":  "BTC",
    "TBTC":   "BTC",
    "BTCB":   "BTC",   # Binance-pegged BTC on BSC
    "HBTC":   "BTC",   # Huobi BTC
    "RENBTC": "BTC",
    "SBTC":   "BTC",   # Synthetix sBTC
    # ETH family (incl. LSTs/LRTs — all track ETH within <1%)
    "WETH":   "ETH",
    "STETH":  "ETH",
    "WSTETH": "ETH",
    "RETH":   "ETH",
    "CBETH":  "ETH",
    "WEETH":  "ETH",
    "EZETH":  "ETH",
    "RSETH":  "ETH",
    "PUFETH": "ETH",
    "SETH":   "ETH",   # Synthetix sETH
    "OSETH":  "ETH",   # StakeWise osETH
    "ANKRETH": "ETH",
    "SWETH":  "ETH",   # Swell ETH
    # SOL family
    "WSOL":   "SOL",
    "JITOSOL": "SOL",
    "MSOL":   "SOL",
    "BNSOL":  "SOL",
    "JUPSOL": "SOL",
    "BSOL":   "SOL",   # BlazeStake
    # Others
    "WBNB":   "BNB",
    "WMATIC": "MATIC",
    "WPOL":   "POL",
    "WAVAX":  "AVAX",
    "WFTM":   "FTM",
    "CBXRP":  "XRP",   # Coinbase-wrapped XRP
    "WTRX":   "TRX",
    "WDOGE":  "DOGE",
}


def canonical_symbol(symbol: str) -> str:
    """Collapse wrapped/liquid-staked variants down to their underlying symbol.

    Returns the upper-cased symbol unchanged if it's not a known wrapper.
    This is the *storage key* used by the price cache — always go through
    this function when reading from or writing to `price_cache` so wrapped
    variants share a row with their underlying.

    Safe to call with None/empty string (returns "").
    """
    if not symbol:
        return ""
    s = symbol.upper()
    return _CANONICAL_SYMBOL.get(s, s)


def pair_for_symbol(symbol: str) -> str | None:
    """Return the Binance spot pair for a symbol (or None if unmapped).

    Canonicalizes first — so WBTC/CBBTC/TBTC all resolve via BTCUSDT even
    if we forget to add them to `_BINANCE_PAIR_MAP` explicitly.

    Callers should treat None as "fall through to next price source" — we
    don't want to query Binance for a long-tail ERC-20 that isn't listed,
    because a miss still costs a round trip.
    """
    canon = canonical_symbol(symbol)
    return _BINANCE_PAIR_MAP.get(canon)


def supported_symbols() -> set[str]:
    """All symbols we can resolve via Binance. Handy for diagnostics.

    Includes both directly-mapped symbols and the canonical wrappers
    (so callers iterating this set cover every wrapped variant too).
    """
    return set(_BINANCE_PAIR_MAP.keys()) | set(_CANONICAL_SYMBOL.keys())


def _ensure_cache_table() -> None:
    with sqlite3.connect(CACHE_DB) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS price_cache (
                symbol TEXT NOT NULL,
                date   TEXT NOT NULL,
                price  REAL NOT NULL,
                PRIMARY KEY (symbol, date)
            )"""
        )


def _cached(symbol: str, date: str) -> float | None:
    # Always read under the canonical key so wrapped variants share rows
    key = canonical_symbol(symbol)
    with sqlite3.connect(CACHE_DB) as c:
        r = c.execute(
            "SELECT price FROM price_cache WHERE symbol = ? AND date = ?",
            (key, date),
        ).fetchone()
        return r[0] if r else None


def _store(symbol: str, date: str, price: float) -> None:
    if price <= 0:
        return
    key = canonical_symbol(symbol)
    with sqlite3.connect(CACHE_DB) as c:
        c.execute(
            "INSERT OR REPLACE INTO price_cache VALUES (?, ?, ?)",
            (key, date, price),
        )


def _store_many(symbol: str, rows: list[tuple[str, float]]) -> int:
    """Bulk-insert daily closes for one symbol. Returns rows written.

    The input symbol is canonicalized before writing — so backfilling WBTC
    actually populates the BTC row, which any code reading WBTC prices will
    then hit via its own canonicalization.
    """
    if not rows:
        return 0
    key = canonical_symbol(symbol)
    with sqlite3.connect(CACHE_DB) as c:
        c.executemany(
            "INSERT OR REPLACE INTO price_cache VALUES (?, ?, ?)",
            [(key, d, p) for d, p in rows if p > 0],
        )
    return len(rows)


def _date_to_ms(date: str) -> int:
    """YYYY-MM-DD → Unix ms at 00:00 UTC. Binance expects ms, not seconds."""
    dt = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def binance_intraday_klines(
    symbol: str,
    date: str,
    interval: str = "5m",
) -> list[tuple[int, int, float]]:
    """Fetch a full UTC day of intraday klines for `symbol`.

    Returns a list of (openTime_ms, closeTime_ms, close_price) tuples sorted
    by openTime ASC. Intervals supported by Binance: 1m, 3m, 5m, 15m, 30m,
    1h, 2h, 4h, 6h, 8h, 12h.

    For the default `5m` interval a full day is 288 candles — well under the
    1000-row limit, so one HTTP call covers the whole day. Callers can then
    match each tx timestamp to the candle containing it without any further
    API traffic for that (symbol, day) pair.

    Returns [] for unmapped symbols or on any HTTP failure so the caller can
    fall through to the next price source.
    """
    pair = pair_for_symbol(symbol)
    if not pair:
        return []
    try:
        start_ms = _date_to_ms(date)
        # End of the day = 23:59:59.999 UTC. Binance closes the last candle
        # of the day at 23:55 close, 23:59:59 close for 1m, etc — the
        # +23h59m bump ensures we pull the final candle regardless of
        # interval.
        end_ms = start_ms + (24 * 3600 - 1) * 1000
        r = _SESSION.get(
            _BINANCE_BASE,
            params={
                "symbol": pair,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=20,
        )
        if r.status_code != 200:
            return []
        data = r.json() or []
        out: list[tuple[int, int, float]] = []
        for k in data:
            # Kline row: [openTime, open, high, low, close, volume, closeTime, ...]
            try:
                open_ms = int(k[0])
                close_ms = int(k[6])
                close_price = float(k[4])
                if close_price > 0:
                    out.append((open_ms, close_ms, close_price))
            except (ValueError, IndexError):
                continue
        return out
    except Exception:
        return []


def price_at_timestamp(
    candles: list[tuple[int, int, float]],
    ts_ms: int,
) -> float:
    """Look up the close price of the candle that contains `ts_ms`.

    Used with the output of `binance_intraday_klines` — a pre-fetched list
    of (openTime, closeTime, close) tuples for one day. Binary search
    would be faster but at 288 entries it's not worth the complexity.

    Returns 0.0 if no candle contains the timestamp (e.g. ts is before
    the day's first candle or Binance had a trading halt). Caller should
    fall through to a different price source in that case.
    """
    if not candles:
        return 0.0
    # Linear scan — 288 iterations max for 5m candles, tiny cost
    for open_ms, close_ms, price in candles:
        if open_ms <= ts_ms <= close_ms:
            return price
    # ts is outside the day's candle range. If it's AFTER the last candle,
    # return the last candle's close (the price just before the boundary).
    # If BEFORE the first candle, return the first candle's open-time price
    # as the best available approximation. This keeps cost basis calculations
    # stable across timezone-edge cases.
    if ts_ms > candles[-1][1]:
        return candles[-1][2]
    if ts_ms < candles[0][0]:
        return candles[0][2]
    return 0.0


def binance_historical_price(symbol: str, date: str) -> float:
    """Return the USD close price for `symbol` on `date` (YYYY-MM-DD).

    Uses the 1d kline whose openTime matches the requested UTC date. The
    result is the candle's close — standard way exchanges report end-of-day
    prices. Returns 0 for unmapped symbols or any HTTP failure so the
    caller can fall through to the next price source.

    Caches positive hits in the shared `price_cache` table (same table
    `history.cg_historical_price` uses) so subsequent requests short-circuit
    without even hitting us.
    """
    pair = pair_for_symbol(symbol)
    if not pair:
        return 0.0

    _ensure_cache_table()
    cached = _cached(symbol, date)
    if cached is not None and cached > 0:
        return cached

    try:
        start_ms = _date_to_ms(date)
        # Request exactly the one candle that starts at 00:00 UTC on `date`.
        # Using limit=1 with startTime is safer than relying on endTime
        # math — Binance returns the candle whose openTime >= startTime.
        r = _SESSION.get(
            _BINANCE_BASE,
            params={
                "symbol": pair,
                "interval": "1d",
                "startTime": start_ms,
                "limit": 1,
            },
            timeout=15,
        )
        if r.status_code != 200:
            return 0.0
        data = r.json() or []
        if not data:
            return 0.0
        # Kline row format: [openTime, open, high, low, close, volume, closeTime, ...]
        close = float(data[0][4])
        if close > 0:
            _store(symbol, date, close)
        return close
    except Exception:
        return 0.0


def backfill_daily_series(
    symbol: str,
    start_date: str,
    end_date: str | None = None,
) -> int:
    """Fetch & cache every daily close for `symbol` in [start_date, end_date].

    ONE HTTP request covers up to 1000 daily candles — so a ~3-year backfill
    for a single asset is a single call. This is what makes the portfolio
    value chart cheap to keep current: we can refresh every held symbol
    with N HTTP calls where N = number of distinct symbols, no matter how
    many days of history the chart is showing.

    Returns the number of cached rows written (0 if symbol unmapped, the
    symbol isn't on Binance, or the HTTP call failed).
    """
    pair = pair_for_symbol(symbol)
    if not pair:
        return 0
    if end_date is None:
        end_date = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    _ensure_cache_table()
    try:
        start_ms = _date_to_ms(start_date)
        # endTime is inclusive of the candle whose openTime <= endTime. Bump
        # by ~23h so the candle for `end_date` itself is included.
        end_ms = _date_to_ms(end_date) + (23 * 3600 + 59 * 60) * 1000
    except Exception:
        return 0

    written = 0
    cursor_ms = start_ms
    # Binance klines cap: 1000 candles per request. Loop until we either
    # cover the whole range or the response is empty.
    while cursor_ms <= end_ms:
        try:
            r = _SESSION.get(
                _BINANCE_BASE,
                params={
                    "symbol": pair,
                    "interval": "1d",
                    "startTime": cursor_ms,
                    "endTime": end_ms,
                    "limit": 1000,
                },
                timeout=20,
            )
            if r.status_code != 200:
                break
            data = r.json() or []
            if not data:
                break
            # Materialize (date, close) pairs. Binance returns strings for
            # numeric fields — float() parses them cleanly.
            rows: list[tuple[str, float]] = []
            for k in data:
                open_ms = int(k[0])
                close = float(k[4])
                rows.append((_ms_to_date(open_ms), close))
            written += _store_many(symbol, rows)
            # Advance cursor past the last returned candle's close time
            last_close_ms = int(data[-1][6])
            if last_close_ms <= cursor_ms:
                # No forward progress → bail so we don't loop forever
                break
            cursor_ms = last_close_ms + 1
            # Be polite to Binance even though we're well under the limits
            time.sleep(0.05)
        except Exception:
            break

    return written


def backfill_multi(
    symbols: list[str],
    start_date: str,
    end_date: str | None = None,
) -> dict[str, int]:
    """Convenience: backfill a basket of symbols in one call.

    Returns {symbol: rows_written} for the ones that are mapped. Unmapped
    symbols are silently skipped — use `pair_for_symbol()` first if you
    need to know which ones we can actually cover. Dedupes canonical
    symbols so `backfill_multi(['WBTC', 'BTC'])` only hits Binance once.
    """
    out: dict[str, int] = {}
    seen_canonical: set[str] = set()
    for s in symbols:
        if not pair_for_symbol(s):
            continue
        canon = canonical_symbol(s)
        if canon in seen_canonical:
            continue
        seen_canonical.add(canon)
        out[canon] = backfill_daily_series(canon, start_date, end_date)
    return out


# Curated list of "important" coins we want to proactively keep fresh even
# if the current user doesn't hold them yet. Used by the top-coins backfill
# endpoint — lets the dashboard trend chart and the reprice-all action
# work instantly for any new wallet the user imports, without a cold-cache
# round-trip to Binance on the first render.
#
# Ordering roughly follows market cap + "things you're likely to have
# touched" — BTC/ETH/SOL first, then majors, then DeFi / L2 / memes that
# retail wallets commonly hold. All entries must be in `_BINANCE_PAIR_MAP`
# (they're verified by `pair_for_symbol()` at call time).
TOP_COINS: list[str] = [
    # Tier 1: majors
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX",
    # Tier 2: L1s / L2s most wallets touch
    "MATIC", "DOT", "LINK", "NEAR", "APT", "SUI", "ARB", "OP", "ATOM", "LTC",
    # Tier 3: DeFi blue chips
    "UNI", "AAVE", "MKR", "LDO", "CRV", "PENDLE", "ENA", "EIGEN", "GMX", "INJ",
    # Tier 4: memes / ecosystem — common in retail wallets
    "PEPE", "SHIB", "WIF", "BONK", "JUP", "JTO", "FET", "RENDER", "TIA", "SEI",
]


def backfill_top_coins(
    lookback_days: int = 365 * 5,
    end_date: str | None = None,
) -> dict[str, int]:
    """Proactively backfill the curated top-coins list.

    Fetches up to `lookback_days` of daily closes for every symbol in
    `TOP_COINS`. At ~5 years that's ~1800 candles — two HTTP calls per
    symbol (1000-candle limit), so ~60 calls for the whole list. Each
    call is weight=2, so we burn ~120 weight against the 1200/min IP
    budget — completes comfortably under a minute.

    Returns {canonical_symbol: rows_written}. Safe to call repeatedly —
    `_store_many` uses INSERT OR REPLACE so re-runs just refresh existing
    rows with the latest closes.
    """
    start_date = (
        datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    ).strftime("%Y-%m-%d")
    return backfill_multi(TOP_COINS, start_date, end_date)
