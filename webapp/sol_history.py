"""Solana transaction history importer.

Architecture overview
---------------------
This module is the Solana counterpart to history.py's EVM walker. It calls the
Helius Enhanced Transactions API — a Helius-specific endpoint that parses raw
Solana transactions into structured, human-readable events (SWAP, TRANSFER, etc.)
rather than raw instruction logs. This is significantly more reliable than parsing
raw Solana transactions ourselves: detecting a Jupiter swap from raw logs requires
parsing program-specific borsh layouts that change with each Jupiter version;
Helius does that heavy lifting and exposes a consistent schema.

Output contract
---------------
Every row emitted by fetch_sol_history() matches the shape consumed by
db.add_transaction() and, transitively, by the tax engine (tax/methods.py).
Required keys: ts (ISO string), tx_type, symbol, amount, price_usd, notes.
Optional: wallet_id and user_id are None at emit time; the caller fills them
before insert (matching the pattern in app.py's _run_import_for_wallet).

Transaction type mapping
------------------------
Helius Enhanced API assigns a `type` field to each transaction:
  SWAP               → two rows: sell (input token) + buy (output token)
  TRANSFER           → one row: deposit (inbound) or withdraw (outbound)
  UNKNOWN            → passes through spam filter; kept if LEGIT_MINTS hit
  NFT_*/COMPRESSED_* → dropped (not fungible token events)
  <anything else>    → passes through spam filter

Price resolution waterfall
--------------------------
  1. Stablecoin (USDC/USDT): hard $1.00 — no API call.
  2. Binance klines (daily): covers SOL, BONK, WIF, JUP, JTO, W, PYTH, RAY,
     ORCA, and other exchange-listed Solana tokens. Zero cost, most accurate
     for liquid tokens.
  3. Birdeye 15m OHLC: covers any SPL token by mint address. Requires
     BIRDEYE_API_KEY. Rate-limited; used only when Binance misses.
  4. $0.0 with a log warning: means we couldn't price it. The user can
     fix the row manually via the UI. Better to have an explicit zero than
     a fabricated price.

Spam filter
-----------
Unlisted (not-in-allowlist) mints go through tax/solana_tokens.is_spam_token()
inside _parse_transfer. The tx type passed to the filter reflects the ORIGINAL
Helius type: SWAP/TRANSFER contexts preserve unlisted mints (the user
voluntarily engaged with them), while UNKNOWN/AIRDROP/other contexts drop
them — that's the dust-airdrop filter path. See tax/solana_tokens.py for the
allowlist rationale and the decision rules.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from applog import log

# ---------------------------------------------------------------------------
# Environment loading — reuse scanner.py's pattern
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
# Do not log this key — it appears in Helius URLs so we strip it before
# printing any URL in log output.
HELIUS_API_KEY: str = _ENV.get("HELIUS_API_KEY", os.getenv("HELIUS_API_KEY", ""))

_HELIUS_BASE = "https://api.helius.xyz/v0/addresses"

# One HTTP session with connection pooling across the pagination loop.
# Helius Enhanced Transactions uses persistent TCP well; re-connecting per
# page wastes ~100ms of TLS handshake on every one of potentially dozens of
# pages.
_SESSION = requests.Session()
_SESSION.headers.update({"accept": "application/json"})

# ---------------------------------------------------------------------------
# Mint → symbol cache (populated on demand via Helius DAS getAsset)
# ---------------------------------------------------------------------------
_MINT_SYMBOL_CACHE: dict[str, str | None] = {}

_HELIUS_RPC = "https://mainnet.helius-rpc.com/"


def _resolve_mint_symbol(mint: str) -> str | None:
    """Resolve a Solana mint to its ticker symbol via Helius DAS getAsset.

    Results are cached for the lifetime of the process so repeated calls for
    the same mint (e.g. many swaps involving the same token) only hit the API
    once. Returns None on failure or if the asset has no symbol.

    Used as a fallback when the token is not in the solana_tokens allowlist and
    Helius's Enhanced Transactions API didn't embed a symbol in the transfer
    (which is common for newer or exotic mints like xStock RWA tokens).
    """
    if mint in _MINT_SYMBOL_CACHE:
        return _MINT_SYMBOL_CACHE[mint]
    if not HELIUS_API_KEY:
        _MINT_SYMBOL_CACHE[mint] = None
        return None
    try:
        r = _SESSION.post(
            f"{_HELIUS_RPC}?api-key={HELIUS_API_KEY}",
            json={"jsonrpc": "2.0", "id": "sym", "method": "getAsset",
                  "params": {"id": mint}},
            timeout=8,
        )
        if r.ok:
            result = (r.json().get("result") or {})
            token_info = result.get("token_info") or {}
            meta = (result.get("content") or {}).get("metadata") or {}
            sym = (token_info.get("symbol") or meta.get("symbol") or "").strip().upper()
            _MINT_SYMBOL_CACHE[mint] = sym or None
            return _MINT_SYMBOL_CACHE[mint]
    except Exception:
        pass
    _MINT_SYMBOL_CACHE[mint] = None
    return None

# ---------------------------------------------------------------------------
# Lazy imports of price modules
# ---------------------------------------------------------------------------
# We import lazily (inside functions) rather than at module top-level to keep
# this file importable in test environments that don't have a live DB or API
# key. Tests mock the price calls directly.

def _import_price_modules():
    """Return (binance_prices, birdeye_prices, solana_tokens) or raise."""
    import binance_prices as bp
    import birdeye_prices as bep
    from tax import solana_tokens as st
    return bp, bep, st


# ---------------------------------------------------------------------------
# Price resolution
# ---------------------------------------------------------------------------

def _resolve_price(symbol: str, mint: str, unix_ts: int) -> float:
    """Resolve USD price for an SPL token via the cheapest available source.

    Waterfall:
      1. Stablecoin → $1.00 flat (no I/O).
      2. binance_prices.binance_historical_price(symbol, date) → free,
         covers exchange-listed SOL ecosystem tokens.
      3. birdeye_prices.get_sol_price_cached(mint, unix_ts) → Birdeye API,
         covers any SPL token by mint address (rate-limited).
      4. 0.0 with a warning — user can fix manually.

    The symbol argument uses the canonical ticker (SOL, BONK, …) for Binance
    routing; the mint argument is used for Birdeye when Binance misses. Both
    are required.
    """
    date = datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    symu = (symbol or "").upper()

    try:
        bp, bep, st = _import_price_modules()
    except ImportError as exc:
        log.warning("Price module import failed: %s", exc)
        return 0.0

    # Step 1: stablecoin short-circuit
    if mint in st.STABLECOIN_MINTS:
        return 1.0

    # Step 2: Binance (free, covers liquid tokens including most Solana majors)
    try:
        price = bp.binance_historical_price(symu, date)
        if price and price > 0:
            return price
    except Exception as exc:
        log.debug("Binance price lookup failed for %s/%s: %s", symu, date, exc)

    # Step 3: Birdeye (SPL-specific, rate-limited — only call when Binance misses)
    try:
        price = bep.get_sol_price_cached(mint, unix_ts)
        if price and price > 0:
            return price
    except Exception as exc:
        log.debug("Birdeye price lookup failed for %s/%s: %s", mint, unix_ts, exc)

    log.warning(
        "Could not resolve price for %s (mint=%s date=%s) — defaulting to 0.0",
        symu, mint, date,
    )
    return 0.0


# ---------------------------------------------------------------------------
# Helius Enhanced Transactions fetcher
# ---------------------------------------------------------------------------

def _helius_page(
    address: str,
    before: str | None,
    limit: int = 100,
    max_retries: int = 3,
) -> list[dict] | None:
    """Fetch one page of Enhanced Transactions for `address`.

    Helius pagination uses a cursor-style 'before' parameter: pass the
    signature of the LAST transaction from the previous page to get the next
    batch. The first call omits 'before' and gets the most-recent `limit` txs.

    We request 100 per page (Helius's maximum for this endpoint) to minimize
    round-trips. Helius returns transactions in descending time order (newest
    first), which is fine — we re-sort chronologically at insert time.

    Return contract (important — callers rely on this to distinguish
    end-of-history from a transient failure):
      - ``list[dict]`` (possibly empty): success; an empty list means Helius
        has no more transactions older than the current cursor, i.e. the
        wallet walk is complete.
      - ``None``: persistent error after ``max_retries`` attempts. The
        caller MUST abort the walk rather than silently treat it as
        end-of-history — otherwise a single blip on page 3 would drop all
        older pages and corrupt the cost basis.

    Transient failures (429, 5xx, network errors) are retried with
    exponential backoff (2s → 4s → 8s). Non-retryable 4xx statuses (403
    bad key, 404 no such address, …) return ``None`` immediately.
    """
    if not HELIUS_API_KEY:
        log.error("HELIUS_API_KEY not set — cannot fetch Solana history")
        return None

    # Scrub the API key from any URL we log. The key is in the query string;
    # we only log the path portion.
    url = f"{_HELIUS_BASE}/{address}/transactions/"
    params: dict = {"api-key": HELIUS_API_KEY, "limit": limit}
    if before:
        params["before"] = before

    backoff = 2.0
    for attempt in range(1, max_retries + 1):
        try:
            r = _SESSION.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            log.warning(
                "Helius network error (attempt %d/%d): %s",
                attempt, max_retries, exc,
            )
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None

        # Success path.
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                log.warning("Helius returned non-JSON for address %s", address)
                return None
            # Helius Enhanced Transactions returns a plain list, not {result: [...]}
            return data if isinstance(data, list) else []

        # Retryable: 429 rate-limit and 5xx server errors.
        if r.status_code == 429 or 500 <= r.status_code < 600:
            log.warning(
                "Helius returned %d (attempt %d/%d) — backing off %.1fs",
                r.status_code, attempt, max_retries, backoff,
            )
            if attempt < max_retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            return None

        # Non-retryable 4xx (401 bad key, 403 forbidden, 404 not found, …).
        log.warning(
            "Helius returned %d for address %s (non-retryable)",
            r.status_code, address,
        )
        return None

    return None


# ---------------------------------------------------------------------------
# Transaction parser
# ---------------------------------------------------------------------------

def _unix_ts(tx: dict) -> int:
    """Extract unix timestamp from a Helius tx. Returns 0 on failure."""
    return int(tx.get("timestamp") or 0)


def _iso_ts(unix: int) -> str:
    """Convert unix seconds → ISO-8601 UTC string."""
    return datetime.fromtimestamp(unix, tz=timezone.utc).isoformat()


def _parse_swap(tx: dict, wallet: str, st) -> list[dict]:
    """Parse a SWAP transaction into sell (input) + buy (output) rows.

    Helius Enhanced API represents a swap's token movements in the
    `tokenTransfers` array. Each element has:
      {
        "fromUserAccount": str,   # sender
        "toUserAccount": str,     # receiver
        "mint": str,              # SPL mint address
        "tokenAmount": float,     # human-readable amount (not lamports)
      }

    A typical Jupiter swap has exactly two tokenTransfers:
      - transfer A: user → Jupiter vault (the token the user is selling)
      - transfer B: Jupiter vault → user (the token the user is buying)

    We classify them by direction relative to `wallet`:
      - Transfer FROM wallet → "sell" row
      - Transfer TO wallet   → "buy" row

    SOL native transfers (not tokenTransfers) appear in `nativeTransfers`.
    We handle those separately in _parse_native_transfers and then let the
    swap parser pick up WSOL from tokenTransfers if it appears there.
    """
    unix = _unix_ts(tx)
    ts = _iso_ts(unix)
    rows: list[dict] = []
    wallet_lower = wallet.lower()
    tx_sig = tx.get("signature") or ""

    for transfer in (tx.get("tokenTransfers") or []):
        mint = transfer.get("mint") or ""
        amount = float(transfer.get("tokenAmount") or 0)
        if amount <= 0:
            continue

        from_acc = (transfer.get("fromUserAccount") or "").lower()
        to_acc = (transfer.get("toUserAccount") or "").lower()

        # Self-transfer inside a swap (e.g. self-wrap/unwrap SOL↔WSOL) is
        # not a taxable disposal — wallet is on both sides, no economic
        # change. Skip defensively before classifying sell/buy.
        if from_acc == wallet_lower and to_acc == wallet_lower:
            continue

        # Resolve symbol — use allowlist first, fall back to Helius metadata
        symbol = st.resolve_symbol(mint)
        if symbol is None:
            # Not in allowlist. For a SWAP the filter keeps it (user
            # voluntarily engaged), but we still run it through
            # is_spam_token for consistency and future-proofing.
            if st.is_spam_token(mint, "SWAP"):
                continue
            symbol = (transfer.get("symbol") or "").upper() or "UNKNOWN"
            if not symbol or symbol == "UNKNOWN":
                # Enhanced Transactions API didn't embed a symbol — fall back
                # to the DAS getAsset API. Covers newer/exotic mints (e.g.
                # xStock RWA tokens) that Helius doesn't embed in tokenTransfers.
                symbol = _resolve_mint_symbol(mint) or ""
                if not symbol:
                    continue

        # Normalize Wrapped SOL to SOL (economic substance is identical)
        if mint == "So11111111111111111111111111111111111111112":
            symbol = "SOL"

        if from_acc == wallet_lower:
            tx_type = "sell"
        elif to_acc == wallet_lower:
            tx_type = "buy"
        else:
            # Neither sender nor receiver is our wallet — internal pool
            # movement, skip it.
            continue

        price = _resolve_price(symbol, mint, unix)
        rows.append({
            "ts": ts,
            "tx_type": tx_type,
            "symbol": symbol,
            "amount": amount,
            "price_usd": price,
            "notes": f"swap · {tx_sig[:16]}",
            "wallet_id": None,
            "user_id": None,
        })

    return rows


def _parse_transfer(
    tx: dict,
    wallet: str,
    st,
    *,
    spam_filter_type: str = "TRANSFER",
) -> list[dict]:
    """Parse a TRANSFER transaction into deposit or withdraw rows.

    Unlike swaps, transfers have a single economic direction: the wallet
    either sent or received tokens. We emit one row per tokenTransfer leg
    that involves our wallet.

    Self-transfers (wallet is both sender AND receiver — e.g. consolidating
    token accounts) are skipped here since they're not taxable events. We
    check at the transfer level rather than building a tx-hash-based skip
    set (as the EVM bridge collapser does) because Solana txs can legitimately
    contain one self-transfer leg alongside a real external transfer.

    ``spam_filter_type`` controls how ``is_spam_token`` treats unlisted
    mints. When this parser is called from the SWAP or TRANSFER branch of
    ``_parse_tx`` the user voluntarily engaged with the token so unknown
    mints are KEPT (is_spam_token returns False for SWAP/TRANSFER). When
    it's called from the UNKNOWN/AIRDROP else branch the caller passes
    the original tx type, and spam airdrops with a Helius-provided symbol
    get dropped instead of smuggled through via the metadata fallback.
    """
    unix = _unix_ts(tx)
    ts = _iso_ts(unix)
    rows: list[dict] = []
    wallet_lower = wallet.lower()
    tx_sig = tx.get("signature") or ""

    for transfer in (tx.get("tokenTransfers") or []):
        mint = transfer.get("mint") or ""
        amount = float(transfer.get("tokenAmount") or 0)
        if amount <= 0:
            continue

        from_acc = (transfer.get("fromUserAccount") or "").lower()
        to_acc = (transfer.get("toUserAccount") or "").lower()

        # Skip self-transfers
        if from_acc == wallet_lower and to_acc == wallet_lower:
            continue

        # Only emit rows where our wallet is one of the parties
        if from_acc != wallet_lower and to_acc != wallet_lower:
            continue

        symbol = st.resolve_symbol(mint)
        if symbol is None:
            # Unknown mint: consult the spam filter. For SWAP/TRANSFER it
            # keeps the row (user actively engaged); for AIRDROP/UNKNOWN
            # and other non-voluntary contexts it drops spam that would
            # otherwise smuggle in a Helius-metadata symbol.
            if st.is_spam_token(mint, spam_filter_type):
                continue
            symbol = (transfer.get("symbol") or "").upper() or "UNKNOWN"
            if not symbol or symbol == "UNKNOWN":
                continue

        if mint == "So11111111111111111111111111111111111111112":
            symbol = "SOL"

        tx_type = "withdraw" if from_acc == wallet_lower else "deposit"
        price = _resolve_price(symbol, mint, unix)
        rows.append({
            "ts": ts,
            "tx_type": tx_type,
            "symbol": symbol,
            "amount": amount,
            "price_usd": price,
            "notes": f"transfer · {tx_sig[:16]}",
            "wallet_id": None,
            "user_id": None,
        })

    return rows


def _parse_native_transfers(tx: dict, wallet: str) -> list[dict]:
    """Extract native SOL transfers from a transaction's nativeTransfers array.

    Helius separates native SOL movements from tokenTransfers. These appear
    in nearly every transaction (as gas fees go to validators, internal
    program calls move lamports, etc.), so we apply a minimum-amount filter
    of 0.001 SOL to drop gas-redistribution noise. Actual user-visible SOL
    sends/receives are typically ≥0.01 SOL.

    We do NOT call _resolve_price here — instead we return rows without a
    price so the caller can batch-resolve them (or the rows come back as
    price_usd=0 if resolution is skipped for native transfers). Actually,
    for correctness we DO resolve price so every row has a usable cost basis.
    """
    unix = _unix_ts(tx)
    ts = _iso_ts(unix)
    rows: list[dict] = []
    wallet_lower = wallet.lower()
    tx_sig = tx.get("signature") or ""

    # Minimum SOL to bother recording. Fees, rent-exemption deposits, and
    # internal SOL movements between program-owned accounts are typically
    # <0.001 SOL. Recording them produces huge numbers of zero-value noise
    # rows that confuse the cost basis engine and make reports unreadable.
    MIN_SOL = 0.001

    for nt in (tx.get("nativeTransfers") or []):
        amount_lamports = float(nt.get("amount") or 0)
        amount_sol = amount_lamports / 1e9
        if amount_sol < MIN_SOL:
            continue

        from_acc = (nt.get("fromUserAccount") or "").lower()
        to_acc = (nt.get("toUserAccount") or "").lower()

        if from_acc == wallet_lower and to_acc == wallet_lower:
            continue  # self-transfer
        if from_acc != wallet_lower and to_acc != wallet_lower:
            continue  # doesn't involve our wallet

        tx_type = "withdraw" if from_acc == wallet_lower else "deposit"
        mint = "So11111111111111111111111111111111111111112"
        price = _resolve_price("SOL", mint, unix)
        rows.append({
            "ts": ts,
            "tx_type": tx_type,
            "symbol": "SOL",
            "amount": amount_sol,
            "price_usd": price,
            "notes": f"sol-transfer · {tx_sig[:16]}",
            "wallet_id": None,
            "user_id": None,
        })

    return rows


def _parse_tx(tx: dict, wallet: str, st) -> list[dict]:
    """Dispatch a single Helius transaction to the appropriate parser.

    The `type` field from Helius Enhanced API tells us what kind of
    transaction this is. We handle the three main categories (SWAP,
    TRANSFER, UNKNOWN) and skip NFT / compressed-NFT events entirely since
    those are outside the tax engine's current scope (NFT disposals need
    separate handling that the current row schema doesn't support).
    """
    tx_type = (tx.get("type") or "UNKNOWN").upper()

    # NFT / cNFT / program-specific events with no fungible token movement
    # that we want to record for tax purposes.
    skip_prefixes = ("NFT_", "COMPRESSED_NFT_", "CREATE_POOL", "CLOSE_POSITION",
                     "BURN", "INITIALIZE_ACCOUNT", "CLOSE_ACCOUNT")
    if any(tx_type.startswith(p) for p in skip_prefixes):
        return []

    rows: list[dict] = []

    if tx_type == "SWAP":
        rows = _parse_swap(tx, wallet, st)
        # Swaps sometimes also involve a native SOL leg (e.g. SOL → USDC on
        # Jupiter routes through WSOL). If tokenTransfers didn't catch any SOL
        # rows (Helius may put SOL in nativeTransfers for native-input swaps)
        # we add native transfer rows to capture that leg.
        if not any(r["symbol"] == "SOL" for r in rows):
            rows.extend(_parse_native_transfers(tx, wallet))

    elif tx_type == "TRANSFER":
        rows = _parse_transfer(tx, wallet, st)
        # Pure SOL transfers use nativeTransfers (no tokenTransfers on-chain)
        if not rows:
            rows = _parse_native_transfers(tx, wallet)

    else:
        # UNKNOWN / AIRDROP / CONTRACT_INTERACTION / anything else: the user
        # did NOT voluntarily initiate this so unlisted mints are assumed
        # spam. We pass the real tx_type down into _parse_transfer so that
        # is_spam_token drops unlisted mints at the source; native SOL is
        # always in the allowlist so the native fallback is unaffected.
        rows = _parse_transfer(tx, wallet, st, spam_filter_type=tx_type)
        if not rows:
            rows = _parse_native_transfers(tx, wallet)

    return rows


# ---------------------------------------------------------------------------
# Main entrypoint
# ---------------------------------------------------------------------------

def fetch_sol_history(address: str, max_txs: int = 5000) -> list[dict]:
    """Fetch and parse a Solana wallet's transaction history.

    Walks the Helius Enhanced Transactions API in reverse-chronological order
    (newest first) using `before=<lastSig>` pagination. Continues until:
      (a) Helius returns an empty page (wallet fully exhausted), or
      (b) We've processed `max_txs` raw transactions (safety ceiling).

    Each transaction is parsed by _parse_tx() into zero or more row dicts
    matching the db.add_transaction() schema. Progress is logged every 100
    processed transactions so slow imports are observable.

    Returns a flat list of all emitted rows, unsorted. The caller (app.py's
    _run_import_for_wallet) handles deduplication against already-inserted
    rows before calling db.add_transaction().

    ``max_txs`` defaults to 5000 — deliberately generous for a newest-first
    walk, because truncating the tail on Solana means we lose the OLDEST
    history (the buys that provide cost basis for retained sells). The EVM
    walker walks oldest-first specifically to avoid that failure mode;
    Helius doesn't expose an ``after=`` cursor so we can't do the same here.
    If the ceiling is actually hit the function emits a loud WARNING so the
    user knows their cost basis may be incomplete.

    Persistent Helius failures raise ``RuntimeError`` rather than silently
    returning a truncated result: a partial import is strictly worse than
    no import for tax reporting, because missing buys turn future sells
    into phantom zero-basis disposals.
    """
    if not HELIUS_API_KEY:
        log.error("HELIUS_API_KEY not configured — cannot fetch Solana history")
        return []

    try:
        _, _, st = _import_price_modules()
    except ImportError as exc:
        log.error("Failed to import price modules: %s", exc)
        return []

    all_rows: list[dict] = []
    before: str | None = None
    processed = 0
    hit_ceiling = False

    while processed < max_txs:
        batch = _helius_page(address, before, limit=min(100, max_txs - processed))
        if batch is None:
            # Persistent error — abort loudly rather than silently truncate.
            raise RuntimeError(
                f"Helius pagination aborted for {address} after retries — "
                f"import is incomplete. Retry later. Processed {processed} "
                f"txs before the failure."
            )
        if not batch:
            # Empty page = end of history
            break

        for tx in batch:
            if processed >= max_txs:
                hit_ceiling = True
                break
            rows = _parse_tx(tx, address, st)
            all_rows.extend(rows)
            processed += 1

            if processed % 100 == 0:
                log.info(
                    "[sol_history] Processed %d txs, emitted %d rows so far (wallet=%s)",
                    processed, len(all_rows), address,
                )

        # Helius pagination: `before` = signature of the last tx in the batch.
        # The next call returns transactions older than this signature.
        before = batch[-1].get("signature")
        if not before:
            break

    if hit_ceiling:
        log.warning(
            "[sol_history] Hit max_txs ceiling (%d) for wallet %s — oldest "
            "history was NOT fetched. Cost basis may be incomplete for sells "
            "whose matching buys predate the %dth most recent tx. Raise "
            "max_txs and re-import, or expect zero-basis disposals.",
            max_txs, address, max_txs,
        )

    log.info(
        "[sol_history] Done: processed %d txs, emitted %d rows for wallet %s",
        processed, len(all_rows), address,
    )
    return all_rows


# ---------------------------------------------------------------------------
# CLI debug entrypoint
# ---------------------------------------------------------------------------

def main():
    """Dump parsed rows as JSON to stdout for debugging.

    Usage:
        python3 sol_history.py <address> [--max-txs N]

    Example:
        python3 sol_history.py F1iLXdL7rSUvjxomqxj8HcWTnFgSkTMGhj9G84XoAfKz --max-txs 50

    Useful for verifying row shapes and price resolution before wiring into
    the app. Output goes to stdout; logs go to stderr (set LOG_LEVEL=DEBUG
    to see price waterfall decisions).
    """
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    parser = argparse.ArgumentParser(
        description="Fetch Solana transaction history and dump as JSON"
    )
    parser.add_argument("address", help="Solana wallet address (base58)")
    parser.add_argument(
        "--max-txs", type=int, default=5000,
        help="Maximum number of raw transactions to process (default: 5000)",
    )
    args = parser.parse_args()

    rows = fetch_sol_history(args.address, max_txs=args.max_txs)
    print(json.dumps(rows, indent=2, default=str))


if __name__ == "__main__":
    main()
