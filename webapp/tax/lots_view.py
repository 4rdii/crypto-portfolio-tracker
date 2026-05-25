"""Lot snapshots at each sell timestamp — powers the Spec-ID picker UI.

When a user wants to manually pick which lots get consumed by a sale,
the frontend needs to know *what lots were open at that moment*. This
module walks the transaction history once, and for every sell emits a
snapshot of the open lots (lot_id, acquired_ts, remaining units, cost
per unit) just before the sell executes.

The snapshot is computed under the FIFO-default walker — the user can
then assign their own lot picks on top of it. If the user has no
overrides we fall back to the default method's picks for that sale.

Why a separate module from methods.py? Because this view is read-only
audit data — it doesn't produce disposals, just a snapshot per sell.
Keeping it separate means the hot path of tax report generation stays
focused while the picker UI can load a richer view on demand.
"""
from __future__ import annotations

from dataclasses import asdict

from .methods import (
    BUY_TYPES,
    SELL_TYPES,
    STABLECOINS,
    Lot,
    _parse_ts,
    canonical,
)
from .self_transfers import filter_self_transfers


def build_sell_lot_snapshots(transactions: list[dict]) -> list[dict]:
    """For every sell / withdraw, return a dict:

        {
            "sell_tx_id": int,
            "symbol": str,             # canonical
            "disposed_ts": str,        # ISO
            "amount": float,           # units being sold
            "proceeds_usd": float,     # sell price × amount
            "open_lots": [
                {"lot_id": int, "acquired_ts": str, "remaining": float,
                 "cost_per_unit": float},
                ...
            ],
        }

    The snapshot list is ordered by sell ts ascending — matches the
    chronological view the picker UI wants to render. Only sells with
    AT LEAST ONE open lot of the same (canonical) symbol are included;
    if a sale has no basis on record it can't be spec-id'd anyway.
    """
    # Strip self-transfers first — the picker must see the same cleaned
    # history the calculator sees, otherwise the user would be offered
    # "ghost" lots from a wallet-to-wallet move that never happened from
    # a tax perspective.
    transactions, _ = filter_self_transfers(transactions)

    txs = sorted(
        transactions,
        key=lambda t: (_parse_ts(t["ts"]), t.get("id") or 0),
    )

    # Active lot ledger: {symbol → [Lot]} with running `remaining` values.
    lots_by_symbol: dict[str, list[Lot]] = {}
    next_lot_id: dict[str, int] = {}

    snapshots: list[dict] = []

    for tx in txs:
        raw_sym = (tx.get("symbol") or "").strip()
        if not raw_sym:
            continue
        sym = canonical(raw_sym)
        ttype = (tx.get("tx_type") or "").lower()
        amt = float(tx.get("amount") or 0)
        if amt <= 0:
            continue
        price = float(tx.get("price_usd") or 0)
        if sym in STABLECOINS:
            price = 1.0
        tx_id = tx.get("id")
        ts = _parse_ts(tx["ts"])

        if ttype in BUY_TYPES:
            lots = lots_by_symbol.setdefault(sym, [])
            nid = next_lot_id.get(sym, 1)
            lots.append(Lot(
                lot_id=nid,
                symbol=sym,
                acquired_ts=ts,
                amount=amt,
                remaining=amt,
                cost_per_unit=price,
                source_tx_id=tx_id,
            ))
            next_lot_id[sym] = nid + 1
            continue

        if ttype not in SELL_TYPES and ttype != "swap":
            continue

        # SNAPSHOT: capture the state of open lots BEFORE we drain them.
        lots = lots_by_symbol.get(sym, [])
        open_lots_snapshot = [
            {
                "lot_id": lot.lot_id,
                "acquired_ts": lot.acquired_ts.isoformat(),
                "remaining": lot.remaining,
                "cost_per_unit": lot.cost_per_unit,
                # Helpful for the UI's hover: original units + source tx
                "original_amount": lot.amount,
                "source_tx_id": lot.source_tx_id,
            }
            for lot in lots
            if lot.remaining > 1e-12
        ]
        if open_lots_snapshot:
            snapshots.append({
                "sell_tx_id": tx_id,
                "symbol": sym,
                "disposed_ts": ts.isoformat(),
                "amount": amt,
                "proceeds_usd": amt * price,
                "open_lots": open_lots_snapshot,
            })

        # Drain the lots FIFO so the snapshot for the NEXT sell reflects
        # the correct remaining balances. Spec-ID overrides are applied
        # later at report time — this walker is just for the picker view.
        remaining = amt
        for lot in lots:
            if remaining <= 1e-12:
                break
            if lot.remaining <= 1e-12:
                continue
            take = min(lot.remaining, remaining)
            lot.remaining -= take
            remaining -= take

    return snapshots
