"""On-chain transaction history importer.

Walks Moralis /wallets/{address}/history for EVM chains and parses transfers
into internal transaction rows. Skips self-transfers between wallets the user
owns. Enriches with historical prices via CoinGecko (cached in SQLite).

Output rows have the shape consumed by db.add_transaction():
    {ts, tx_type, symbol, amount, price_usd, notes, tx_hash}
"""
from __future__ import annotations

import time
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import requests

from scanner import ENV, _moralis_get, EVM_CHAINS_EXTENDED  # reuse env loader + rotating GET wrapper
from moralis_keys import pool_size as _moralis_pool_size

# Legacy single-key var kept only for the "is any key configured?" guards
# below — actual requests go through _moralis_get which uses the rotator.
MORALIS_API_KEY = ENV.get("MORALIS_API_KEY", "")
EVM_CHAINS = ["eth", "polygon", "bsc", "arbitrum", "optimism", "base"]

# Stablecoins — always $1.00, no historical lookups needed. Eliminates rounding
# noise in realized P&L and avoids wasting CoinGecko requests on obvious $1 assets.
STABLECOINS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "PYUSD", "GUSD",
    "USDE", "SUSDE", "FRAX", "LUSD", "USDD", "USDP", "USDB", "USDS",
    "AXLUSDC", "USDC.E", "USDBC", "CRVUSD", "GHO", "EURS", "EURT",
    "USDT.E", "MUSD", "XUSD", "DOLA",
}

CACHE_DB = Path(__file__).resolve().parent / "portfolio.db"
_CG_ID_MAP = {
    # Majors
    "ETH": "ethereum", "WETH": "weth", "STETH": "staked-ether",
    "BTC": "bitcoin", "WBTC": "wrapped-bitcoin", "CBBTC": "coinbase-wrapped-btc",
    "SOL": "solana", "WSOL": "wrapped-solana", "JITOSOL": "jito-staked-sol",
    "BNB": "binancecoin", "WBNB": "wbnb",
    "MATIC": "matic-network", "POL": "polygon-ecosystem-token", "WMATIC": "wmatic",
    "AVAX": "avalanche-2", "WAVAX": "wrapped-avax",
    # Stables
    "USDC": "usd-coin", "USDT": "tether", "DAI": "dai",
    "USDE": "ethena-usde", "SUSDE": "ethena-staked-usde",
    "FRAX": "frax", "LUSD": "liquity-usd", "FDUSD": "first-digital-usd",
    "PYUSD": "paypal-usd", "GUSD": "gemini-dollar", "TUSD": "true-usd",
    # L2 / rollup tokens
    "ARB": "arbitrum", "OP": "optimism", "BASE": "base-protocol",
    "STRK": "starknet", "ZK": "zksync", "LINEA": "linea", "BLAST": "blast",
    # LSTs / LRTs
    "RETH": "rocket-pool-eth", "CBETH": "coinbase-wrapped-staked-eth",
    "WEETH": "wrapped-eeth", "EZETH": "renzo-restaked-eth",
    "RSETH": "kelp-dao-restaked-eth", "PUFETH": "pufeth",
    "LDO": "lido-dao", "RPL": "rocket-pool",
    # DeFi blue chips
    "UNI": "uniswap", "AAVE": "aave", "COMP": "compound-governance-token",
    "CRV": "curve-dao-token", "CVX": "convex-finance", "MKR": "maker",
    "SNX": "havven", "SUSHI": "sushi", "1INCH": "1inch", "BAL": "balancer",
    "GMX": "gmx", "DYDX": "dydx-chain", "FXS": "frax-share", "YFI": "yearn-finance",
    # L1s
    "LINK": "chainlink", "DOT": "polkadot", "ATOM": "cosmos",
    "NEAR": "near", "APT": "aptos", "SUI": "sui", "TIA": "celestia",
    "SEI": "sei-network", "INJ": "injective-protocol",
    # Farming season
    "PENDLE": "pendle", "ENA": "ethena", "EIGEN": "eigenlayer",
    "JUP": "jupiter-exchange-solana", "JTO": "jito-governance-token",
    "W": "wormhole", "TNSR": "tensor", "PYTH": "pyth-network",
    "DRIFT": "drift-protocol", "KMNO": "kamino",
    # Memes
    "PEPE": "pepe", "SHIB": "shiba-inu", "FLOKI": "floki", "BONK": "bonk",
    "WIF": "dogwifcoin", "BRETT": "based-brett", "DEGEN": "degen-base",
    "TOSHI": "toshi", "POPCAT": "popcat", "MOG": "mog-coin",
    # Aero/Base ecosystem
    "AERO": "aerodrome-finance", "WELL": "moonwell", "CBXRP": "wrapped-xrp",
    # Others
    "TRX": "tron", "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin",
    "TON": "the-open-network", "LTC": "litecoin", "BCH": "bitcoin-cash",
    "ICP": "internet-computer", "FIL": "filecoin", "HBAR": "hedera-hashgraph",
    # Commodity-backed
    "PAXG": "pax-gold", "XAUT": "tether-gold", "KAU": "kinesis-gold",
    "KAG": "kinesis-silver",
    # More farming
    "CAKE": "pancakeswap-token", "RDNT": "radiant-capital",
    "MAGIC": "magic", "GNS": "gains-network", "JONES": "jones-dao",
    "RNDR": "render-token", "FET": "fetch-ai", "OCEAN": "ocean-protocol",
    "GRT": "the-graph", "AKT": "akash-network", "THETA": "theta-token",
}


def _ensure_price_cache_table():
    with sqlite3.connect(CACHE_DB) as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS price_cache (
                symbol TEXT NOT NULL,
                date   TEXT NOT NULL,
                price  REAL NOT NULL,
                PRIMARY KEY (symbol, date)
            )"""
        )


def _canonical(symbol: str) -> str:
    """Canonicalize a symbol to its underlying for cache lookups.

    Wrapped/liquid-staked variants (WBTC, WETH, STETH, WSOL, …) share a
    cache row with their underlying asset so every code path that asks
    for WBTC gets BTC's price. See binance_prices._CANONICAL_SYMBOL for
    the full mapping. Import is lazy so history.py keeps working even if
    binance_prices fails to load (e.g. bad deploy).
    """
    try:
        from binance_prices import canonical_symbol
        return canonical_symbol(symbol)
    except Exception:
        return (symbol or "").upper()


def _cached_price(symbol: str, date: str) -> float | None:
    key = _canonical(symbol)
    with sqlite3.connect(CACHE_DB) as c:
        r = c.execute(
            "SELECT price FROM price_cache WHERE symbol = ? AND date = ?",
            (key, date),
        ).fetchone()
        return r[0] if r else None


def _store_price(symbol: str, date: str, price: float):
    key = _canonical(symbol)
    with sqlite3.connect(CACHE_DB) as c:
        c.execute(
            "INSERT OR REPLACE INTO price_cache VALUES (?, ?, ?)",
            (key, date, price),
        )


def cg_historical_price(symbol: str, date: str) -> float:
    """Fetch USD price for a symbol on a specific date (YYYY-MM-DD) from CoinGecko.

    Cached in SQLite. Returns 0 if not found. We only cache *positive* prices,
    so failed lookups can be retried later after the ID map is extended or the
    rate limit clears. Stablecoins short-circuit to $1.00.
    """
    sym = symbol.upper()
    if sym in STABLECOINS:
        return 1.0
    cached = _cached_price(sym, date)
    if cached is not None and cached > 0:
        return cached
    cid = _CG_ID_MAP.get(sym)
    if not cid:
        return 0
    # CoinGecko wants DD-MM-YYYY
    try:
        d = datetime.strptime(date, "%Y-%m-%d")
        cg_date = d.strftime("%d-%m-%Y")
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{cid}/history",
            params={"date": cg_date, "localization": "false"},
            timeout=20,
        )
        if r.status_code == 429:
            # Rate-limited; back off and leave uncached for retry
            time.sleep(5)
            return 0
        if r.status_code != 200:
            return 0
        data = r.json()
        price = float(((data.get("market_data") or {}).get("current_price") or {}).get("usd") or 0)
        if price > 0:
            _store_price(sym, date, price)
        time.sleep(1.5)  # respect CG free-tier rate limit
        return price
    except Exception:
        return 0


# ---------- Moralis fetcher ----------

def fetch_evm_history(address: str, chain: str, max_pages: int = 200) -> list[dict]:
    """Page through Moralis wallet history for one chain.

    Walks the full cursor chain until Moralis runs out of pages (or we hit the
    safety ceiling). Uses ASC order (oldest first) so the cost-basis engine
    sees buys before the sells that depend on them — and if we ever do get
    truncated by the ceiling, we lose the MOST RECENT history (less harmful)
    rather than the OLDEST (the buys we need for cost basis).

    `max_pages=200` → up to 20,000 transactions per chain per wallet, which
    covers anything short of a market-maker wallet.
    """
    out: list[dict] = []
    cursor: str | None = None
    pages = 0
    while pages < max_pages:
        params = {"chain": chain, "limit": 100, "order": "ASC"}
        if cursor:
            params["cursor"] = cursor
        try:
            r = _moralis_get(
                f"https://deep-index.moralis.io/api/v2.2/wallets/{address}/history",
                params=params,
                timeout=30,
            )
            if r is None:
                raise RuntimeError("no Moralis response (all keys exhausted)")
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[history] {chain} page {pages} failed: {e}")
            break
        batch = data.get("result") or []
        out.extend(batch)
        cursor = data.get("cursor")
        pages += 1
        if not cursor or not batch:
            break
    if pages >= max_pages:
        print(f"[history] {chain} hit max_pages ceiling at {len(out)} rows — some history may be missing")
    return out


# ---------- Parser ----------

def parse_transfers(tx_records: list[dict], own_addresses: set[str], chain: str) -> list[dict]:
    """Convert Moralis history records into internal transaction rows.

    Each row from Moralis has `native_transfers`, `erc20_transfers`, `nft_transfers`
    and a `category` hint. We turn each transfer into one of:
      - buy      (receive from external, token swap output)
      - sell     (send to external, token swap input)
      - deposit  (airdrop, incoming transfer from exchange/unknown)
      - withdraw (outgoing to exchange/unknown)
    Self-transfers between wallets in `own_addresses` are dropped.
    """
    own_lower = {a.lower() for a in own_addresses}
    rows: list[dict] = []

    for tx in tx_records:
        if tx.get("possible_spam"):
            continue
        category = (tx.get("category") or "").lower()
        ts_iso = tx.get("block_timestamp") or ""
        try:
            ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
            date_only = ts[:10]
        except Exception:
            continue
        tx_hash = tx.get("hash") or ""
        block_number = str(tx.get("block_number") or "")

        # Handle native transfers (ETH / MATIC / BNB / etc.)
        for t in (tx.get("native_transfers") or []):
            if float(t.get("value_formatted") or 0) == 0:
                continue
            direction = (t.get("direction") or "").lower()
            symbol = (t.get("token_symbol") or "").upper()
            if not symbol:
                continue
            from_addr = (t.get("from_address") or "").lower()
            to_addr = (t.get("to_address") or "").lower()
            # Drop transfers between the user's own wallets
            if from_addr in own_lower and to_addr in own_lower:
                continue
            amount = float(t.get("value_formatted") or 0)
            rows.append({
                "ts": ts,
                "date": date_only,
                "tx_type": _classify(direction, category),
                "symbol": symbol,
                "amount": amount,
                "chain": chain,
                "contract": "",  # native — no ERC-20 contract
                "block_number": block_number,
                "tx_hash": tx_hash,
                "counterparty": to_addr if direction == "send" else from_addr,
            })

        # Handle ERC-20 transfers (tokens, stablecoins, swap legs)
        for t in (tx.get("erc20_transfers") or []):
            if t.get("possible_spam"):
                continue
            if not t.get("verified_contract", True):
                # still include, but skip obvious scam/unverified if flag says so
                pass
            amount = float(t.get("value_formatted") or 0)
            if amount == 0:
                continue
            symbol = (t.get("token_symbol") or "").upper()
            if not symbol or symbol in {"UNKNOWN", ""}:
                continue
            direction = (t.get("direction") or "").lower()
            from_addr = (t.get("from_address") or "").lower()
            to_addr = (t.get("to_address") or "").lower()
            if from_addr in own_lower and to_addr in own_lower:
                continue
            rows.append({
                "ts": ts,
                "date": date_only,
                "tx_type": _classify(direction, category),
                "symbol": symbol,
                # Preserve the raw token name — the aToken collapser reads this
                # to identify Aave wrapper tokens (e.g. "Aave Ethereum WBTC").
                # Matching on name is safer than on the upper-cased symbol
                # because Moralis preserves the "aEthWBTC" casing on name but
                # the symbol gets lost to str.upper() above.
                "token_name": t.get("token_name") or "",
                "amount": amount,
                "chain": chain,
                "contract": (t.get("address") or "").lower(),
                "block_number": block_number,
                "tx_hash": tx_hash,
                "counterparty": to_addr if direction == "send" else from_addr,
            })

    return rows


# Detect Aave wrapper tokens from a parsed history row. aTokens are 1:1 claims
# on an underlying asset (supply-side) and debt tokens are 1:1 negative claims
# (borrow-side). For cost-basis purposes we treat any tx that touches an Aave
# wrapper as an internal transfer — the user's economic position is unchanged
# (depositing WBTC into Aave doesn't realize P&L, and neither does withdrawing
# it back). See `_collapse_aave_legs` for the drop logic.
def _is_aave_wrapper_row(r: dict) -> bool:
    name = (r.get("token_name") or "").lower()
    if name.startswith("aave "):
        return True
    # Symbol fallback for when moralis didn't populate the name. Debt tokens
    # use the "variableDebt*" / "stableDebt*" prefix; aTokens follow
    # "a<NetworkPrefix><UNDERLYING>" (aEthWBTC, aPolUSDC, ...). We already
    # upper-cased symbol in parse_transfers so check the all-caps forms.
    sym = (r.get("symbol") or "")
    if sym.startswith(("VARIABLEDEBT", "STABLEDEBT")):
        return True
    return False


def _collapse_aave_legs(rows: list[dict]) -> list[dict]:
    """Drop every transfer belonging to a tx that contains an Aave wrapper.

    Why drop the whole tx, not just the wrapper leg? A supply looks like::

        erc20_transfer 1:  user -> Aave pool     WBTC        (dir=send)
        erc20_transfer 2:  0x0  -> user          aEthWBTC    (dir=receive, mint)

    Both legs are emitted by parse_transfers. If we only dropped the aToken
    mint we'd be left with a bare "WBTC withdraw" that the cost-basis engine
    would treat as sending WBTC to a third party — creating a phantom
    realized-P&L event at the moment of deposit, which is wrong (the user
    still owns the claim, it just moved into a rebasing wrapper). Dropping
    BOTH legs keeps the original WBTC cost basis intact until the user
    actually disposes of the position via a swap or transfer out.

    The same logic applies in reverse for withdraws (aEthWBTC burn + WBTC
    receive from the pool) and for borrow/repay txs that touch
    variableDebt/stableDebt receipts.

    Runs BEFORE `_collapse_bridges` so the bridge matcher doesn't get confused
    by a WBTC "deposit" leg from an Aave withdraw pairing up with a WBTC
    "sell" leg from a downstream swap a few minutes later — that was causing
    real swap dispositions to vanish completely.
    """
    bad_tx_hashes: set[tuple[str, str]] = set()
    for r in rows:
        if _is_aave_wrapper_row(r):
            bad_tx_hashes.add((r.get("chain") or "", r.get("tx_hash") or ""))
    if not bad_tx_hashes:
        return rows
    kept = [
        r for r in rows
        if (r.get("chain") or "", r.get("tx_hash") or "") not in bad_tx_hashes
    ]
    print(
        f"[history] collapsed {len(bad_tx_hashes)} Aave interaction tx(s), "
        f"dropping {len(rows) - len(kept)} rows"
    )
    return kept


def _classify(direction: str, category: str) -> str:
    """Map Moralis direction + category → our tx_type."""
    cat = category.lower()
    if "airdrop" in cat:
        return "deposit"  # $0 cost basis
    if "swap" in cat:
        return "buy" if direction == "receive" else "sell"
    if "purchase" in cat or "buy" in cat:
        return "buy" if direction == "receive" else "sell"
    if "sale" in cat or "sell" in cat:
        return "sell" if direction == "send" else "buy"
    if direction == "receive":
        return "deposit"
    if direction == "send":
        return "withdraw"
    return "deposit"


def _current_market_price(symbol: str) -> float:
    """Fallback: grab the most recent USD price the scanner wrote for this symbol."""
    with sqlite3.connect(CACHE_DB) as c:
        r = c.execute(
            "SELECT usd_price FROM holdings WHERE UPPER(symbol) = ? ORDER BY updated_at DESC LIMIT 1",
            (symbol.upper(),),
        ).fetchone()
        return float(r[0]) if r and r[0] else 0.0


# ---------- Moralis historical pricing ----------

# Native tokens need a wrapped-token contract to price on Moralis (e.g. WETH on ETH).
# Map (chain, symbol) → wrapped contract so we can route natives through Moralis too.
_NATIVE_WRAPPED = {
    ("eth",      "ETH"):   "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    ("arbitrum", "ETH"):   "0x82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH arb
    ("optimism", "ETH"):   "0x4200000000000000000000000000000000000006",  # WETH op
    ("base",     "ETH"):   "0x4200000000000000000000000000000000000006",  # WETH base
    ("polygon",  "MATIC"): "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",  # WMATIC
    ("polygon",  "POL"):   "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",  # WMATIC/POL
    ("bsc",      "BNB"):   "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
}


def moralis_historical_price(contract: str, chain: str, block: str) -> float:
    """Fetch historical ERC-20 price from Moralis by contract + block number.

    Much higher rate limit than CoinGecko free tier and supports any ERC-20
    directly by contract address — no symbol mapping needed. Returns 0 on failure.
    """
    if _moralis_pool_size() == 0 or not contract or not block:
        return 0
    try:
        r = _moralis_get(
            f"https://deep-index.moralis.io/api/v2.2/erc20/{contract}/price",
            params={"chain": chain, "to_block": str(block)},
            timeout=20,
        )
        if r is None or r.status_code != 200:
            return 0
        data = r.json()
        return float(data.get("usdPrice") or 0)
    except Exception:
        return 0


def _resolve_price(sym: str, date: str, ctx: dict) -> tuple[float, str]:
    """Resolve a single (symbol, date) price.

    Priority cascade (cheapest + most reliable first):
      1. Stablecoin short-circuit ($1.00)
      2. SQLite price_cache (any source that wrote it)
      3. Binance klines — free, covers 95% of liquid tokens
      4. Moralis /erc20/price — accurate for long-tail DEX tokens, costs CUs
      5. CoinGecko /coins/{id}/history — free but rate-limited
      6. Current market price from holdings table — last resort
      7. 0.0 (flagged as "unknown" so the user can fix manually)

    Binance goes BEFORE Moralis because: it's free, doesn't burn Moralis CUs,
    and for any token that shows up on a centralized exchange the Binance
    daily close is at least as accurate as Moralis's block-level price
    (Binance has deeper order books than most DEXes). The CU savings are
    the whole point — a typical retail wallet resolves 90%+ of its prices
    at step 3 and never touches step 4.
    """
    symu = sym.upper()
    # Stables always $1
    if symu in STABLECOINS:
        return 1.0, "stablecoin"
    # Hit cache first
    cached = _cached_price(symu, date)
    if cached is not None and cached > 0:
        return cached, "cache"

    # Binance klines — free, covers all liquid tokens. Returns 0 for
    # unmapped symbols so long-tail ERC-20s fall through cleanly.
    try:
        from binance_prices import binance_historical_price
        p = binance_historical_price(symu, date)
        if p > 0:
            # binance_historical_price already writes to price_cache, but
            # belt-and-braces here in case the module contract changes.
            _store_price(symu, date, p)
            return p, "binance"
    except Exception:
        pass

    chain = ctx.get("chain") or ""
    block = ctx.get("block") or ""
    contract = ctx.get("contract") or ""

    # Native tokens: use wrapped-token contract so we can still route via Moralis
    if not contract and chain and symu:
        contract = _NATIVE_WRAPPED.get((chain, symu), "")

    # Fallback 1: Moralis historical price by contract + block (long-tail)
    if contract and block and chain:
        p = moralis_historical_price(contract, chain, block)
        if p > 0:
            _store_price(symu, date, p)
            return p, "moralis"

    # Fallback 2: CoinGecko (symbols in our hardcoded map not on Binance)
    p = cg_historical_price(symu, date)
    if p > 0:
        return p, "coingecko"

    # Fallback 3: current market price from the scanner
    p = _current_market_price(symu)
    if p > 0:
        return p, "current_market"

    return 0, "unknown"


def _ts_to_ms(ts_iso: str) -> int:
    """Parse an ISO timestamp string → unix ms. Falls back to 0 on errors.

    Binance wants millis for kline lookups. The transaction `ts` column is
    already in ISO-8601 with timezone, so this is a clean one-liner with
    an Exception catch for paranoia (Moralis has been known to return
    funky datestamps on chains that were new to their indexer).
    """
    try:
        s = ts_iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def enrich_with_prices(rows: list[dict], progress_cb=None) -> list[dict]:
    """Resolve USD price for every transaction row.

    Pricing is PER-ROW (not deduped by date) so intraday buys/sells get
    their actual minute-level price rather than a daily close. Buying ETH
    at 09:00 and selling at 15:00 on the same day now resolves to two
    distinct 5-minute candle closes — much better cost-basis accuracy.

    Batching strategy: rows are grouped by (symbol, date). For each group
    we fetch ONE Binance call covering all 288 five-minute candles for
    that UTC day, then match every row in the group to the candle
    containing its timestamp. That keeps the HTTP fan-out identical to
    the old daily-granularity path (one call per (symbol, date) pair)
    while giving us 5-minute accuracy for free.

    Priority per row:
      1. Stablecoin short-circuit ($1.00 flat)
      2. Binance 5m kline at tx timestamp (via batched day-fetch)
      3. SQLite price cache (daily close — populated by earlier imports
         or the portfolio-chart backfill)
      4. Moralis /erc20/{contract}/price?to_block=N (long-tail DEX tokens)
      5. CoinGecko /coins/{id}/history
      6. Current market price from holdings table
      7. Zero (flagged as "unknown")
    """
    _ensure_price_cache_table()

    try:
        from binance_prices import (
            binance_intraday_klines,
            pair_for_symbol,
            price_at_timestamp,
        )
        _binance_available = True
    except Exception:
        _binance_available = False

    # Group rows by (symbol, UTC-date) so we make exactly one Binance call
    # per (symbol, day). The group also collects the first row's Moralis
    # context (contract/block/chain) so long-tail fallbacks still work
    # when Binance doesn't cover the symbol.
    from collections import defaultdict
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    ctx_for_group: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["symbol"].upper(), r["date"])
        groups[key].append(r)
        cur = ctx_for_group.get(key)
        if cur is None or (not cur.get("contract") and r.get("contract")):
            ctx_for_group[key] = {
                "contract": r.get("contract") or "",
                "block": r.get("block_number") or "",
                "chain": r.get("chain") or "",
            }

    total = len(groups)
    # Per-symbol daily close cache (populated from Binance 5m candles'
    # final close) — written to the SQLite price cache so the portfolio
    # chart picks it up without a separate backfill round-trip.
    daily_close_writes: list[tuple[str, str, float]] = []

    for i, ((symu, date), group_rows) in enumerate(groups.items()):
        # Stablecoin fast path
        if symu in STABLECOINS:
            for r in group_rows:
                r["price_usd"] = 1.0
                r["price_source"] = "stablecoin"
            if progress_cb:
                progress_cb(i + 1, total, symu, date)
            continue

        resolved_all = False
        if _binance_available and pair_for_symbol(symu):
            # One call fetches 288 five-minute candles for the whole day.
            candles = binance_intraday_klines(symu, date, interval="5m")
            if candles:
                for r in group_rows:
                    ts_ms = _ts_to_ms(r.get("ts") or "")
                    p = price_at_timestamp(candles, ts_ms) if ts_ms else 0.0
                    if p > 0:
                        r["price_usd"] = p
                        r["price_source"] = "binance-5m"
                    else:
                        # Outside the day boundary or empty candle — fall
                        # back to the day's final close (last candle).
                        r["price_usd"] = candles[-1][2]
                        r["price_source"] = "binance-5m-fallback"
                # Cache the day's closing price for the portfolio chart
                daily_close_writes.append((symu, date, candles[-1][2]))
                resolved_all = True

        if not resolved_all:
            # Per-row fallback through the slower cascade (Moralis/CG/etc).
            # Re-use the group's shared context for contract-based Moralis
            # lookups. This path still dedupes by (sym, date) since the
            # fallback sources don't expose minute-level history.
            ctx = ctx_for_group.get((symu, date), {})
            price, source = _resolve_price(symu, date, ctx)
            for r in group_rows:
                r["price_usd"] = price
                r["price_source"] = source

        if progress_cb:
            progress_cb(i + 1, total, symu, date)

    # Bulk-write the day closes we resolved via Binance so the portfolio
    # chart has them without needing its own backfill pass. Canonicalize
    # each symbol so wrapped variants share storage with the underlying.
    if daily_close_writes:
        canonicalized = [
            (_canonical(sym), date, price)
            for sym, date, price in daily_close_writes
        ]
        with sqlite3.connect(CACHE_DB) as c:
            c.executemany(
                "INSERT OR REPLACE INTO price_cache VALUES (?, ?, ?)",
                canonicalized,
            )

    return rows


def _collapse_bridges(rows: list[dict], window_sec: int = 900,
                      amount_tolerance: float = 0.05) -> list[dict]:
    """Detect & drop bridge legs.

    A bridge shows up on-chain as two transfers we see because the bridge contract
    itself is not one of the user's wallets:
      • a SEND (classified "sell") of token X on chain A
      • a RECEIVE (classified "deposit") of the same token X on chain B
    …within a few minutes, with near-matching amounts (bridges take a small fee).

    Neither leg represents a real disposition — the user still holds X, just on a
    different chain. Keeping them in the cost-basis engine creates phantom P&L if
    the two legs resolve to different historical prices. Drop both.
    """
    from datetime import datetime as _dt

    def _ts(r: dict):
        try:
            return _dt.fromisoformat(r["ts"].replace("Z", "+00:00"))
        except Exception:
            return None

    drop = set()
    # Index deposits by symbol for quick matching
    deposit_idx = [
        (i, r) for i, r in enumerate(rows) if r.get("tx_type") == "deposit"
    ]

    for si, s in enumerate(rows):
        if si in drop:
            continue
        if s.get("tx_type") != "sell":
            continue
        s_ts = _ts(s)
        if s_ts is None:
            continue
        s_amt = float(s.get("amount") or 0)
        if s_amt <= 0:
            continue
        for di, d in deposit_idx:
            if di in drop or di == si:
                continue
            if d.get("symbol") != s.get("symbol"):
                continue
            d_ts = _ts(d)
            if d_ts is None:
                continue
            if abs((d_ts - s_ts).total_seconds()) > window_sec:
                continue
            d_amt = float(d.get("amount") or 0)
            if d_amt <= 0:
                continue
            # Match within tolerance (bridges skim a small fee → deposit slightly less)
            ratio = abs(d_amt - s_amt) / s_amt
            if ratio > amount_tolerance:
                continue
            drop.add(si)
            drop.add(di)
            break

    if drop:
        print(f"[history] collapsed {len(drop) // 2} bridge pair(s), dropping {len(drop)} rows")
    return [r for i, r in enumerate(rows) if i not in drop]


def import_wallet_history(wallet_address: str, own_addresses: set[str],
                          fetch_prices: bool = True,
                          chains: list[str] | None = None) -> list[dict]:
    """Full pipeline for one EVM wallet: fetch → parse → collapse bridges → enrich → filter scams.

    `chains` lets the caller restrict the import to a subset of EVM_CHAINS
    (e.g. only `['base']` when the user's Moralis wallet.chains lookup said
    that's the only chain with activity). Defaults to all supported EVM
    chains to preserve backward compat.
    """
    target_chains = chains if chains else EVM_CHAINS
    # Defensive: ignore unknown chain slugs the caller may pass through from
    # a compromised / stale client. Never fetch from a chain we don't price.
    target_chains = [c for c in target_chains if c in EVM_CHAINS_EXTENDED]
    if not target_chains:
        return []
    all_rows: list[dict] = []
    for chain in target_chains:
        print(f"[history] fetching {chain} for {wallet_address[:10]}...")
        records = fetch_evm_history(wallet_address, chain)
        parsed = parse_transfers(records, own_addresses, chain)
        print(f"[history]   → {len(parsed)} transfers")
        all_rows.extend(parsed)

    # Collapse Aave supply/withdraw/borrow/repay interactions FIRST. These
    # are tagged by wrapper-token legs (aToken mint/burn + variable/stableDebt
    # receipts). Dropping the whole tx preserves the underlying's cost basis
    # and — importantly — prevents the underlying's "deposit/withdraw" leg
    # from being misidentified as a bridge pair by the next step.
    all_rows = _collapse_aave_legs(all_rows)

    # Collapse bridge legs BEFORE pricing so we don't waste API calls on rows
    # that are about to be dropped.
    all_rows = _collapse_bridges(all_rows)

    if fetch_prices and all_rows:
        print(f"[history] enriching {len(all_rows)} rows with historical prices...")
        enrich_with_prices(all_rows)

    # Scam filter: if a deposit lands with no price at all (CoinGecko doesn't know
    # it AND we have no current holding of it), it's almost certainly a scam airdrop
    # that Moralis's spam flag missed. Dropping these prevents ghost cost-basis rows.
    before = len(all_rows)
    all_rows = [
        r for r in all_rows
        if not (r["tx_type"] == "deposit" and float(r.get("price_usd") or 0) <= 0)
    ]
    filtered = before - len(all_rows)
    if filtered:
        print(f"[history] filtered {filtered} zero-price deposits (likely scam airdrops)")
    return all_rows
