"""Pricing + cost estimation for paid operations.

Currently priced operation: `import_history` (pulls all on-chain transfers
from Moralis for a wallet, enriches with historical prices). Cost is driven
by the number of transactions the wallet has on the chains the user asked
us to import — the bigger the trading history, the more Moralis API calls
we have to pay for.

Pricing model (already includes the 2x safety margin discussed in chat):
  - $0.002 per transaction
  - $0.20 minimum per import
  - 10 free transactions per PROFILE (lifetime, shared across all the
    user's wallets — not per wallet). Applied to the first N imported
    txs and then exhausted.
  - Delta re-imports priced at 2x the new-tx delta (buffer for cursor
    overlap, re-fetched prices, and the occasional bad page)

All public functions return plain dicts so they're trivially JSONable from
the FastAPI routes.
"""
from __future__ import annotations

from scanner import _moralis_get, EVM_CHAINS

# All pricing constants in ONE place so we can tune them later.
PRICE_PER_TX = 0.002              # $/tx charged, already 2x margin
MIN_CHARGE   = 0.20               # $ floor per import
FREE_TX_ALLOWANCE = 10            # free txs per profile (lifetime, not per wallet)
DELTA_MULTIPLIER = 2.0            # re-imports charged 2x the new delta


def count_wallet_txs(address: str, chain: str) -> int:
    """Query Moralis /wallets/{addr}/stats → total transactions on a chain.

    Much cheaper than fetching the actual history: /stats is a single call
    that returns aggregate counters. Returns 0 on any failure (we'd rather
    under-estimate and eat a few txs than block an import on a transient
    Moralis hiccup).
    """
    try:
        r = _moralis_get(
            f"https://deep-index.moralis.io/api/v2.2/wallets/{address}/stats",
            params={"chain": chain},
            timeout=15,
        )
        if r is None or r.status_code != 200:
            return 0
        data = r.json() or {}
        # Moralis returns {"transactions": {"total": "123"}}. Can also be
        # wrapped as an int directly depending on chain — handle both.
        tx = data.get("transactions")
        if isinstance(tx, dict):
            return int(tx.get("total") or 0)
        if isinstance(tx, (int, str)):
            return int(tx)
        return 0
    except Exception:
        return 0


def detect_active_chains(address: str) -> list[str]:
    """Return the subset of EVM_CHAINS where the wallet has any tx activity.

    Uses /wallets/{addr}/chains which returns only chains where the address
    has ever transacted. Falls back to counting stats per chain if the
    /chains endpoint is unavailable on a given key tier.
    """
    try:
        r = _moralis_get(
            f"https://deep-index.moralis.io/api/v2.2/wallets/{address}/chains",
            params={"chains": ",".join(EVM_CHAINS)},
            timeout=15,
        )
        if r is not None and r.status_code == 200:
            data = r.json() or {}
            active = data.get("active_chains") or []
            out = []
            for row in active:
                chain = row.get("chain")
                # Moralis returns chains that literally had activity. Some
                # tiers also include inactive ones with first_transaction=None;
                # filter those out.
                if chain and row.get("first_transaction"):
                    out.append(chain)
            if out:
                return out
    except Exception:
        pass
    # Fallback: iterate stats
    return [c for c in EVM_CHAINS if count_wallet_txs(address, c) > 0]


def quote_import(
    address: str,
    chains: list[str] | None = None,
    already_imported_tx_count: int = 0,
    free_allowance_remaining: int = 0,
) -> dict:
    """Return a cost quote for importing this wallet's history.

    `chains` — list of chain slugs to include (e.g. ['eth','base']). Defaults
    to all EVM_CHAINS, but the caller should pass the user's selected subset
    so we don't over-charge for chains they don't care about.

    `already_imported_tx_count` — how many txs are already on file for this
    wallet. Used to price delta re-imports: only the new transactions get
    charged, and they're multiplied by DELTA_MULTIPLIER so re-running the
    import stays roughly proportional to the added history.

    `free_allowance_remaining` — how many free txs the user has left in
    their profile-wide budget. The free allowance is a LIFETIME pool
    shared across every wallet the user imports, so the caller needs to
    pass the remaining count (FREE_TX_ALLOWANCE - txs_already_imported).
    Pass 0 if the user has already exhausted their allowance.
    """
    chains = chains or EVM_CHAINS
    per_chain: list[dict] = []
    total = 0
    for chain in chains:
        n = count_wallet_txs(address, chain)
        per_chain.append({"chain": chain, "tx_count": n})
        total += n

    is_delta = already_imported_tx_count > 0
    # Base chargeable count — what we'd charge for BEFORE applying the
    # profile-wide free allowance. Delta imports bill only the growth
    # multiplied by DELTA_MULTIPLIER; fresh imports bill the full count.
    if is_delta:
        new_delta = max(0, total - already_imported_tx_count)
        base_chargeable = int(round(new_delta * DELTA_MULTIPLIER))
    else:
        base_chargeable = total

    # Apply the user's remaining profile-wide free allowance. Even delta
    # imports can tap the allowance if there's any left — it's a flat
    # "first N txs across your whole account are free" rule.
    free_applied = min(base_chargeable, max(0, int(free_allowance_remaining)))
    chargeable = base_chargeable - free_applied

    raw_cost = chargeable * PRICE_PER_TX
    cost = 0.0 if chargeable == 0 else max(MIN_CHARGE, raw_cost)

    return {
        "address": address,
        "chains": per_chain,
        "total_tx": total,
        "already_imported": already_imported_tx_count,
        "is_delta": is_delta,
        "free_tx_applied": free_applied,
        "free_allowance_remaining_before": int(free_allowance_remaining),
        "free_allowance_remaining_after": max(0, int(free_allowance_remaining) - free_applied),
        "chargeable_tx": chargeable,
        "cost_usd": round(cost, 4),
        "price_per_tx": PRICE_PER_TX,
        "min_charge": MIN_CHARGE,
        "free_allowance": FREE_TX_ALLOWANCE,
    }
