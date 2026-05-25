"""Self-transfer detection: skip wallet→wallet moves in the tax walker.

When you send ETH from Binance to your MetaMask, most importers record
a `withdraw` row on the Binance side and a `deposit` row on the MetaMask
side. Both sides see the same symbol, same amount, and the timestamps
are usually seconds-to-minutes apart.

From the TAX engine's perspective this is NOT a disposal: you still
own the asset, you just moved it between your own wallets. But the
naive walker (buy / sell / deposit / withdraw) treats the withdraw as a
sell and the deposit as a fresh buy at the current market price. That
does two bad things:

  1. Invents a phantom capital gain or loss at the transfer price.
  2. Resets the cost basis of the moved units to the transfer-day price,
     which understates future gains when you actually dispose of it.

This module walks the transaction list once BEFORE the tax engine runs
and tags matched pairs as `self_transfer=True` so the walker can skip
them. The pairing heuristic is deliberately conservative:

  • same canonical symbol (WBTC/BTC, WETH/ETH, etc. via canonical())
  • amount within `AMOUNT_TOL_PCT` (default 0.5%) to tolerate gas burn
    on the sending side (e.g. you withdraw 1.0 ETH but the fee token
    deducts 0.0005 so the receiving wallet sees 0.9995)
  • timestamps within `TIME_WINDOW_SECONDS` (default 1 hour)
  • one must be `withdraw`, the other `deposit`
  • both belong to the same user's imported wallet set (the caller is
    responsible for only feeding one user's txs in)

Swaps, trades, and buys are NEVER matched — only withdraw/deposit.

When multiple candidates are in the window we greedy-match the closest
timestamp first. A given withdraw can match only one deposit and vice
versa.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .methods import _parse_ts, canonical

# Matching tolerances. Tuned against x4rde's real wallet data where a
# Binance→MetaMask ETH transfer appeared as 0.1 ETH withdraw / 0.1 ETH
# deposit 34 seconds apart at the same price. 1 hour + 0.5% comfortably
# covers typical transfer latency while staying tight enough to not
# accidentally pair a same-day buy with a same-day sell.
TIME_WINDOW_SECONDS = 3600  # 1 hour
AMOUNT_TOL_PCT = 0.005       # 0.5%


def detect_self_transfer_ids(transactions: list[dict]) -> set:
    """Return the set of tx ids that are one half of a self-transfer pair.

    Caller passes the raw transaction list; this returns the ids that
    should be filtered out before running the tax walker. Both legs of
    each matched pair are returned (so you skip both the withdraw AND
    the deposit).

    Transactions without an `id` are skipped entirely — we can't address
    them safely. In practice every row out of the DB has one; this
    guard is for test fixtures and hand-built lists.
    """
    # Bucket all withdraws and deposits by canonical symbol so the
    # pairing loop is O(W·D_per_symbol) instead of O(N²).
    withdraws_by_sym: dict[str, list[dict]] = {}
    deposits_by_sym: dict[str, list[dict]] = {}

    for tx in transactions:
        if tx.get("id") is None:
            continue
        raw_sym = (tx.get("symbol") or "").strip()
        if not raw_sym:
            continue
        sym = canonical(raw_sym)
        ttype = (tx.get("tx_type") or "").lower()
        if ttype == "withdraw":
            withdraws_by_sym.setdefault(sym, []).append(tx)
        elif ttype == "deposit":
            deposits_by_sym.setdefault(sym, []).append(tx)

    matched_ids: set = set()
    # Walk withdraws chronologically and greedy-match the nearest eligible
    # deposit. Sorting the deposit pool once per symbol keeps this cheap.
    for sym, withdraws in withdraws_by_sym.items():
        deposits = deposits_by_sym.get(sym, [])
        if not deposits:
            continue
        # Annotate with parsed ts + amount once so we don't re-parse inside
        # the inner loop.
        w_list = [
            (w, _parse_ts(w["ts"]), float(w.get("amount") or 0))
            for w in withdraws
        ]
        d_list = [
            (d, _parse_ts(d["ts"]), float(d.get("amount") or 0))
            for d in deposits
        ]
        w_list.sort(key=lambda x: x[1])
        d_list.sort(key=lambda x: x[1])

        used_deposits: set = set()  # indices into d_list already matched
        for w, w_ts, w_amt in w_list:
            if w_amt <= 0:
                continue
            best_idx = -1
            best_delta = None
            for i, (d, d_ts, d_amt) in enumerate(d_list):
                if i in used_deposits:
                    continue
                if d_amt <= 0:
                    continue
                # Time window (absolute delta so withdraws can be AFTER
                # deposits too — some exchanges backdate).
                delta = abs((d_ts - w_ts).total_seconds())
                if delta > TIME_WINDOW_SECONDS:
                    continue
                # Amount tolerance. Use the larger side as the denominator
                # so a withdraw of 1.0 vs deposit of 0.9995 is still a
                # 0.05% mismatch, not 0.05/0.9995.
                bigger = max(w_amt, d_amt)
                if bigger <= 0:
                    continue
                if abs(w_amt - d_amt) / bigger > AMOUNT_TOL_PCT:
                    continue
                if best_delta is None or delta < best_delta:
                    best_idx = i
                    best_delta = delta
            if best_idx >= 0:
                used_deposits.add(best_idx)
                d = d_list[best_idx][0]
                matched_ids.add(w["id"])
                matched_ids.add(d["id"])

    return matched_ids


def filter_self_transfers(transactions: list[dict]) -> tuple[list[dict], set]:
    """Return (filtered_txs, matched_ids).

    Thin convenience wrapper over `detect_self_transfer_ids` for the
    calculator / lots_view pipelines that just want a clean tx list.
    """
    matched = detect_self_transfer_ids(transactions)
    if not matched:
        return transactions, matched
    filtered = [t for t in transactions if t.get("id") not in matched]
    return filtered, matched
