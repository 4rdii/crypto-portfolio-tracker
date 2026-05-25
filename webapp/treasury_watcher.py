"""Treasury wallet watcher — polls inbound transfers and credits user accounts.

Runs as a background task inside the webapp. Every TREASURY_POLL_SECONDS it
walks the latest incoming transfers to TREASURY_ADDRESS across every EVM
chain we support, looks up the sender in the wallets table, and credits
that user's account for the USD value of the deposit.

Supported inbound tokens:
    USDC, USDT, DAI (stablecoins — priced at $1.00)
    WBTC, cbBTC   (priced via Moralis historical)
    ETH, WETH      (native + wrapped, priced via Moralis)
    BNB            (native on BSC, priced via Moralis)
    SOL            (Solana-only — poller TBD, currently deferred)

Sender matching:
    lower(from_address) → wallets.address
    If no match, the deposit is recorded in unclaimed_deposits so the user
    can still prove ownership later ("hey I sent from wallet X that I hadn't
    linked yet — please credit me").

Idempotency:
    Every credit-topup insert uses a UNIQUE(chain, tx_hash) index so the
    watcher can safely re-poll overlapping windows without double-crediting.

Security — contract allowlist:
    We DO NOT trust the `token_symbol` field returned by Moralis. An attacker
    can deploy an ERC-20 named "USDC" on any chain and transfer a billion of
    them to the treasury from a linked wallet. The fake USDC would then be
    priced at $1 by our STABLE_SYMBOLS path and credit the attacker's
    account with a billion dollars of free balance.

    Defense: every accepted ERC-20 must match a canonical contract address
    in ACCEPTED_CONTRACTS[(chain, contract_lower)]. Any ERC-20 whose contract
    isn't on the allowlist is ignored (not even logged as unclaimed, because
    we can't safely put a USD value on it). Native transfers are safe because
    they're not token contracts — the protocol guarantees the value.

Security — confirmation delay:
    We wait MIN_AGE_SECONDS[chain] before crediting a deposit so a chain
    reorg can't undo a credited tx. The age is computed from block_timestamp
    in the Moralis history response (no extra API call needed). Unmature
    txs are re-examined on the next poll so nothing is dropped, just delayed.
"""
from __future__ import annotations

import os
import time
import threading
import traceback
from datetime import datetime, timezone

import db
from applog import log
from scanner import _moralis_get, ENV, EVM_CHAINS

# Treasury address — all EVM chains use this single address (deterministic
# same private key → same address on every EVM chain). Read from env so it
# can be rotated without a code change; the hardcoded fallback is the
# address originally configured in production.
TREASURY_ADDRESS = (
    os.getenv("TREASURY_ADDRESS")
    or ENV.get("TREASURY_ADDRESS")
    or "0x0317eBA55CEDA9c4910BA105539c96Ee2FA0d773"
).lower()

# How often the background loop polls Moralis per chain. Set conservatively
# so we don't burn through rate limit on an idle treasury.
TREASURY_POLL_SECONDS = 90

# Minimum age (seconds) a tx must reach before we credit it. This is how we
# achieve "N confirmations" without making an extra chain-head lookup — we
# just check block_timestamp against wall clock. The buffer accounts for
# reorgs on each chain's typical depth of finality:
#   eth:        ~3 min (≥12 blocks at 12s each)
#   arbitrum:   ~3 min (soft finality from L1 after ~2.5m)
#   optimism:   ~3 min (soft finality from L1 after ~3m)
#   base:       ~3 min (same as op)
#   polygon:    ~3 min (≥128 blocks at 2s each — polygon reorgs can be deep)
#   bsc:        ~1 min (15 blocks at 3s each)
MIN_AGE_SECONDS: dict[str, int] = {
    "eth":      180,
    "arbitrum": 180,
    "optimism": 180,
    "base":     180,
    "polygon":  180,
    "bsc":       60,
}

# Accepted tokens and their USD pricing strategy.
# 'stable' → locked at $1.00
# 'moralis' → use Moralis historical price at the transfer block
STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDC.E", "USDBC"}
# Native token symbol per chain (for native transfers the Moralis history
# endpoint reports token_symbol=None but populates the "value" field).
NATIVE_SYMBOL = {
    "eth": "ETH", "arbitrum": "ETH", "optimism": "ETH", "base": "ETH",
    "polygon": "MATIC", "bsc": "BNB",
}

# ---------- Contract allowlist ----------
# Every accepted ERC-20 deposit MUST match one of these canonical contract
# addresses. If Moralis reports a transfer of a token whose contract isn't
# on this list we skip it — no credit, no unclaimed row. Addresses are all
# lowercased for constant-time dict lookup.
#
# When expanding this list: only add tokens whose USD value we can price
# defensibly (stablecoins are $1 by definition; WBTC/WETH/etc. are priced
# via Moralis historical at the block of the transfer). Never add a
# low-liquidity or fee-on-transfer token.

# USDC / USDT / DAI — all six chains we currently support.
STABLECOIN_CONTRACTS: dict[tuple[str, str], str] = {
    # USDC (official Circle-issued where available, plus bridged .e variants)
    ("eth",      "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"): "USDC",
    ("polygon",  "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"): "USDC",
    ("polygon",  "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"): "USDC.E",
    ("bsc",      "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"): "USDC",
    ("arbitrum", "0xaf88d065e77c8cc2239327c5edb3a432268e5831"): "USDC",
    ("arbitrum", "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8"): "USDC.E",
    ("optimism", "0x0b2c639c533813f4aa9d7837caf62653d097ff85"): "USDC",
    ("optimism", "0x7f5c764cbc14f9669b88837ca1490cca17c31607"): "USDC.E",
    ("base",     "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"): "USDC",
    ("base",     "0xd9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca"): "USDBC",
    # USDT
    ("eth",      "0xdac17f958d2ee523a2206206994597c13d831ec7"): "USDT",
    ("polygon",  "0xc2132d05d31c914a87c6611c10748aeb04b58e8f"): "USDT",
    ("bsc",      "0x55d398326f99059ff775485246999027b3197955"): "USDT",
    ("arbitrum", "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"): "USDT",
    ("optimism", "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58"): "USDT",
    # DAI (no DAI on BSC mainline — Binance-Peg DAI omitted deliberately)
    ("eth",      "0x6b175474e89094c44da98b954eedeac495271d0f"): "DAI",
    ("polygon",  "0x8f3cf7ad23cd3cadbd9735aff958023239c6a063"): "DAI",
    ("arbitrum", "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1"): "DAI",
    ("optimism", "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1"): "DAI",
    ("base",     "0x50c5725949a6f0c72e6c4a641f24049a917db0cb"): "DAI",
}

# Wrapped natives + BTC wrappers. Priced via Moralis historical at the tx
# block (there is no "stable" shortcut for these).
WRAPPED_TOKEN_CONTRACTS: dict[tuple[str, str], str] = {
    # WETH
    ("eth",      "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"): "WETH",
    ("arbitrum", "0x82af49447d8a07e3bd95bd0d56f35241523fbab1"): "WETH",
    ("optimism", "0x4200000000000000000000000000000000000006"): "WETH",
    ("base",     "0x4200000000000000000000000000000000000006"): "WETH",
    # WMATIC / POL
    ("polygon",  "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270"): "WMATIC",
    # WBNB
    ("bsc",      "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"): "WBNB",
    # WBTC
    ("eth",      "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"): "WBTC",
    ("polygon",  "0x1bfd67037b42cf73acf2047067bd4f2c47d9bfd6"): "WBTC",
    ("arbitrum", "0x2f2a2543b76a4166549f7aab2e75bef0aefc5b0f"): "WBTC",
    ("optimism", "0x68f180fcce6836688e9084f035309e29bf0a2095"): "WBTC",
    # cbBTC (Coinbase-wrapped BTC on Base + Ethereum)
    ("base",     "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"): "CBBTC",
    ("eth",      "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"): "CBBTC",
}

# Merged dict the hot path uses. `(chain, contract_lower)` → canonical symbol.
ACCEPTED_CONTRACTS: dict[tuple[str, str], str] = {
    **STABLECOIN_CONTRACTS,
    **WRAPPED_TOKEN_CONTRACTS,
}

# User-facing symbol list (billing.html pills). Derived from the allowlist
# plus natives that arrive as "native_transfers" rather than ERC-20 legs.
ACCEPTED_SYMBOLS = set(ACCEPTED_CONTRACTS.values()) | {"ETH", "MATIC", "BNB"}


def _price_token(symbol: str, contract: str | None, chain: str, block: str | None) -> float:
    """Return USD price for a single token unit at the given block.

    Stablecoins are flat $1. Everything else goes through Moralis historical
    pricing (falls back to 0 if Moralis can't resolve — which would mean the
    deposit gets logged but credits $0, safer than guessing).
    """
    sym = (symbol or "").upper()
    if sym in STABLE_SYMBOLS:
        return 1.0
    if not contract:
        return 0.0
    try:
        from history import moralis_historical_price
        return float(moralis_historical_price(contract, chain, block) or 0)
    except Exception:
        return 0.0


def _fetch_recent_transfers(chain: str, limit: int = 100) -> list[dict]:
    """Pull the most recent transfers involving the treasury address on a chain.

    Uses /wallets/{addr}/history which returns both native and ERC-20
    transfers in a single call. We only care about inbound rows (the
    treasury as recipient). Sorted DESC so we see the newest first and can
    stop early if we hit an already-credited tx.
    """
    try:
        r = _moralis_get(
            f"https://deep-index.moralis.io/api/v2.2/wallets/{TREASURY_ADDRESS}/history",
            params={"chain": chain, "limit": limit, "order": "DESC"},
            timeout=30,
        )
        if r is None or r.status_code != 200:
            return []
        return (r.json() or {}).get("result") or []
    except Exception as e:
        print(f"[treasury] {chain} fetch failed: {e}")
        return []


def _tx_mature(tx: dict, chain: str) -> bool:
    """Return True iff the tx is old enough to safely credit (reorg protection).

    Unknown / unparseable timestamps are treated as NOT mature so we err on
    the safe side and wait another poll cycle. Freshly mined txs come back
    around on the next loop so nothing is lost permanently.
    """
    ts_str = tx.get("block_timestamp") or ""
    if not ts_str:
        return False
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return False
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    return age >= MIN_AGE_SECONDS.get(chain, 180)


def _process_chain(chain: str) -> dict:
    """Walk recent treasury deposits on one chain, credit each eligible one.

    Returns a small summary dict for logging.
    """
    transfers = _fetch_recent_transfers(chain)
    counts = {"credited": 0, "unclaimed": 0, "skipped": 0, "pending": 0}
    treasury_low = TREASURY_ADDRESS.lower()

    for tx in transfers:
        tx_hash = tx.get("hash")
        block = tx.get("block_number")
        if not tx_hash:
            continue

        # Confirmation/reorg guard: ignore txs that haven't aged past the
        # chain's finality buffer. They'll be picked up on the next poll.
        if not _tx_mature(tx, chain):
            counts["pending"] += 1
            continue

        # Native transfer leg(s) — ETH/MATIC/BNB sent TO the treasury. These
        # are protocol-level and can't be spoofed with a fake contract.
        for nt in (tx.get("native_transfers") or []):
            if (nt.get("to_address") or "").lower() != treasury_low:
                continue
            from_addr = (nt.get("from_address") or "").lower()
            amount = float(nt.get("value_formatted") or 0)
            if amount <= 0:
                counts["skipped"] += 1
                continue
            sym = NATIVE_SYMBOL.get(chain, "ETH")
            price = _price_native(chain, sym, block)
            usd = amount * price
            action = _credit_or_log(chain, tx_hash, from_addr, sym, amount, usd)
            counts[action] += 1

        # ERC-20 leg(s) — must match the contract allowlist. Any token whose
        # (chain, contract) isn't canonical gets silently ignored: we never
        # trust Moralis's `token_symbol` string alone because an attacker can
        # deploy a fake ERC-20 with symbol="USDC" and transfer an arbitrary
        # balance from a linked wallet.
        for er in (tx.get("erc20_transfers") or []):
            if (er.get("to_address") or "").lower() != treasury_low:
                continue
            contract = (er.get("address") or "").lower()
            canonical = ACCEPTED_CONTRACTS.get((chain, contract))
            if canonical is None:
                # Unknown contract — cannot safely value. Drop silently.
                counts["skipped"] += 1
                continue
            from_addr = (er.get("from_address") or "").lower()
            amount = float(er.get("value_formatted") or 0)
            if amount <= 0:
                counts["skipped"] += 1
                continue
            # Use the canonical symbol from the allowlist, NOT the Moralis
            # symbol field. This is what gets priced and stored in the ledger.
            price = _price_token(canonical, contract, chain, block)
            usd = amount * price
            action = _credit_or_log(chain, tx_hash, from_addr, canonical, amount, usd)
            counts[action] += 1

    return {"chain": chain, **counts}


def _price_native(chain: str, symbol: str, block) -> float:
    """Look up the USD price of a native token at a given block via its wrapper."""
    try:
        from history import _NATIVE_WRAPPED, moralis_historical_price
        contract = _NATIVE_WRAPPED.get((chain, symbol))
        if not contract:
            return 0.0
        return float(moralis_historical_price(contract, chain, str(block)) or 0)
    except Exception:
        return 0.0


def _credit_or_log(chain: str, tx_hash: str, from_address: str,
                   symbol: str, amount: float, usd_value: float) -> str:
    """Credit the matching user, or log as unclaimed if we can't match the sender.

    Returns one of 'credited' / 'unclaimed' / 'skipped' so the caller can
    keep stats without touching globals.
    """
    user_id = db.find_user_by_wallet_address(from_address)
    if user_id is None:
        db.record_unclaimed_deposit(chain, tx_hash, from_address, symbol, amount, usd_value)
        return "unclaimed"
    if usd_value <= 0:
        # Sender is known but we couldn't price the token — still record
        # it as unclaimed so ops can resolve it, don't credit zero.
        db.record_unclaimed_deposit(chain, tx_hash, from_address, symbol, amount, usd_value)
        return "unclaimed"
    did = db.credit_topup(
        user_id=user_id,
        delta_usd=usd_value,
        chain=chain,
        tx_hash=tx_hash,
        from_address=from_address,
        token_symbol=symbol,
        token_amount=amount,
        notes=f"Deposit via {chain} · {amount} {symbol}",
    )
    return "credited" if did else "skipped"


# ---------- Background loop ----------

_WORKER_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()
# Wake-up flag — poll_now() sets this so the loop immediately does another
# pass instead of waiting out the full sleep. Thread-safe via the Event.
_WAKE_EVENT = threading.Event()


def _loop():
    print(f"[treasury] watcher started, polling every {TREASURY_POLL_SECONDS}s, "
          f"treasury={TREASURY_ADDRESS}")
    while not _STOP_EVENT.is_set():
        for chain in EVM_CHAINS:
            if _STOP_EVENT.is_set():
                break
            try:
                summary = _process_chain(chain)
                if summary["credited"] or summary["unclaimed"]:
                    log.info("treasury.chain_summary %s", summary)
            except Exception as e:
                log.error("treasury.chain_error chain=%s err=%s", chain, e)
                traceback.print_exc()
        # Sleep, but wake early on poll_now requests.
        _WAKE_EVENT.clear()
        # Wait returns True if the wake event was set during the interval —
        # in which case we immediately loop back without also waiting on the
        # stop event.
        for _ in range(TREASURY_POLL_SECONDS):
            if _STOP_EVENT.is_set() or _WAKE_EVENT.is_set():
                break
            time.sleep(1)
    print("[treasury] watcher stopped")


def start_background_worker():
    """Spawn the treasury poller thread. Called once from app startup."""
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, name="treasury-watcher", daemon=True
    )
    _WORKER_THREAD.start()


def stop_background_worker():
    _STOP_EVENT.set()


def request_poll():
    """Wake the background loop so it runs another pass immediately.

    Non-blocking: the caller returns immediately and the worker picks up
    the wake signal on its next loop iteration. Used by /api/billing/poll-now
    instead of running the full chain walk in the request thread (which
    would let any authenticated user DoS our Moralis quota).
    """
    _WAKE_EVENT.set()


def run_once_sync() -> list[dict]:
    """Manual-trigger version for ops / debugging. Blocks the caller."""
    results = []
    for chain in EVM_CHAINS:
        results.append(_process_chain(chain))
    return results
