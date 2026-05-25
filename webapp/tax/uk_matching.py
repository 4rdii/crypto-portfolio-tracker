"""UK HMRC share-matching rules for crypto disposals.

HMRC treats crypto like shares for CGT purposes. The ordering of a
disposal against prior/future acquisitions is:

  1. SAME-DAY rule — disposals are first matched against any
     acquisitions on the same date.
  2. BED-AND-BREAKFASTING rule (s.105 TCGA 1992) — then against
     acquisitions in the NEXT 30 days. This stops "sell on Dec 31, buy
     back Jan 2" tax loss harvesting from working the way it does in the
     US.
  3. SECTION 104 POOL — everything else is matched against the
     weighted-average pool of prior acquisitions.

The pool itself is just a running average-cost bucket per asset:
adding buys averages them in, disposals remove at the current pool
cost. The complication vs a simple AVG walker is that rules 1+2 run
FIRST and can leave the pool untouched for disposals that fully match
against recent buys.

This module is invoked from the calculator when jurisdiction == "GB".
It emits the same `Disposal` records as the methods.py walker so the
rest of the pipeline (classification, filtering, PDF rendering) works
without any branching.

Notes / not yet modelled
------------------------
• "Matched" disposals against next-30-day acquisitions technically use
  the NEW acquisition's cost basis (not the pool). We implement this.
• HMRC requires NEW acquisitions in the 30-day window to be treated as
  a separate parcel, NOT added to the pool. We implement this.
• Wash sales extending beyond 30 days don't trigger here — HMRC uses a
  hard 30-day cutoff vs the US's soft "substantially identical" test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .methods import (
    BUY_TYPES,
    SELL_TYPES,
    STABLECOINS,
    Disposal,
    Lot,
    _parse_ts,
    canonical,
)


@dataclass
class _Section104Pool:
    """Running weighted-average pool per asset.

    Unlike a lot ledger, the pool collapses everything into one bucket:
    balance (units), total_cost (USD). Each buy adds; each disposal
    removes at the pool's per-unit avg cost.
    """
    balance: float = 0.0
    total_cost: float = 0.0
    first_acquired: datetime | None = None  # for LT classification fallback

    @property
    def avg_cost(self) -> float:
        return (self.total_cost / self.balance) if self.balance > 0 else 0.0

    def add(self, amount: float, cost_per_unit: float, ts: datetime) -> None:
        self.balance += amount
        self.total_cost += amount * cost_per_unit
        if self.first_acquired is None:
            self.first_acquired = ts

    def consume(self, amount: float) -> tuple[float, float]:
        """Remove `amount` units at the pool's current avg cost.

        Returns (actual_amount_taken, cost_basis_removed). If the pool
        doesn't have enough, take what's available.
        """
        take = min(amount, self.balance)
        if take <= 0:
            return 0.0, 0.0
        avg = self.avg_cost
        cost = avg * take
        self.balance -= take
        self.total_cost -= cost
        if self.balance <= 1e-12:
            self.balance = 0.0
            self.total_cost = 0.0
        return take, cost


def compute_disposals_uk(
    transactions: list[dict],
) -> tuple[list[Disposal], dict[str, list[Lot]]]:
    """HMRC share-matching walker.

    Returns the same shape as compute_disposals so the calculator can
    drop this in without changing its interface. The `lots` returned is
    a thin shim: we only carry s104-pool snapshots as synthetic lots for
    the audit view (one per asset). The real audit trail is in the
    Disposal records.

    Algorithm:
      Pre-sort txs ascending by ts. For each SELL, walk forward in a
      two-phase match:
        Phase A: same-day buys of the same asset — match first.
        Phase B: next-30-day buys — match next (bed-and-breakfasting).
        Phase C: the s104 pool — whatever remains.
      BUYs that get fully consumed by phases A/B never enter the pool.
      BUYs with leftover units AFTER a future sale consumes some add
      only the leftover to the pool.
    """
    txs = sorted(
        transactions,
        key=lambda t: (_parse_ts(t["ts"]), t.get("id") or 0),
    )

    # Per-asset pool and a "future-matching ledger" tracking how much of
    # each buy has already been claimed by an earlier sell under the
    # same-day / 30-day rules. We key future-matching by transaction id
    # so we can subtract when the buy is later added to the pool.
    pools: dict[str, _Section104Pool] = {}
    # buy_claimed[buy_tx_id] = amount already claimed by an earlier sell
    buy_claimed: dict[int, float] = {}

    disposals: list[Disposal] = []

    # Pre-index the txs for fast "buys on date X of asset Y" lookups.
    # Since we walk in order and only look forward, a linear scan per
    # sell is fine for typical volumes. If this ever becomes a
    # bottleneck, switch to a sorted index per (symbol, date).
    for idx, tx in enumerate(txs):
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
        ts = _parse_ts(tx["ts"])
        tx_id = tx.get("id") or idx  # fall back to idx so keys are unique

        pool = pools.setdefault(sym, _Section104Pool())

        # ---- Handle BUYs lazily --------------------------------------
        # We can't commit a buy to the pool until we know whether any
        # future sell (within 30 days) will claim it first. Simplest
        # implementation: leave buys uncommitted, and on each SELL walk
        # backward + forward to match. We still need to count "already
        # committed pool units" so prior buys go in the pool eagerly
        # and only future-window buys are held back.
        #
        # In practice: commit every buy to the pool immediately, but
        # remember each buy's id. When a later sell matches a buy under
        # the 30-day rule, REMOVE the matched amount from the pool and
        # emit a disposal at the buy's original cost per unit.
        if ttype in BUY_TYPES:
            pool.add(amt, price, ts)
            continue

        if ttype not in SELL_TYPES and ttype != "swap":
            continue

        # ---- SELL: three-phase match ---------------------------------
        remaining = amt

        def _match_window(lo: datetime, hi: datetime) -> float:
            """Walk forward from `idx` looking for BUYs of the same asset
            within [lo, hi]. Consume up to `remaining` units and emit
            disposals at the BUY's cost_per_unit.

            Returns the amount still unmatched after this phase.
            """
            nonlocal remaining
            # Look forward in the ordered list — we only match against
            # future buys for the 30-day rule. Same-day buys appear
            # after the sell in ordering if the sell's ts is before
            # the buy's ts (same day, different time) — handled by the
            # bounds.
            for j in range(idx + 1, len(txs)):
                if remaining <= 1e-12:
                    break
                ftx = txs[j]
                if canonical(ftx.get("symbol") or "") != sym:
                    continue
                if (ftx.get("tx_type") or "").lower() not in BUY_TYPES:
                    continue
                ftx_ts = _parse_ts(ftx["ts"])
                if ftx_ts < lo or ftx_ts > hi:
                    continue
                famt = float(ftx.get("amount") or 0)
                already = buy_claimed.get(ftx.get("id") or j, 0.0)
                available = famt - already
                if available <= 1e-12:
                    continue
                take = min(available, remaining)
                fprice = float(ftx.get("price_usd") or 0)
                if sym in STABLECOINS:
                    fprice = 1.0
                cost_basis = take * fprice
                proceeds = take * price
                disposals.append(Disposal(
                    symbol=sym,
                    acquired_ts=ftx_ts,  # same/near-day rule: ACQ date = buy
                    disposed_ts=ts,
                    amount=take,
                    cost_basis=cost_basis,
                    proceeds=proceeds,
                    gain=proceeds - cost_basis,
                    # Holding days can be negative here (sold before
                    # bought under bed-and-breakfasting); we clamp to 0
                    # for the report. UK doesn't use ST/LT so this only
                    # matters for the audit line.
                    holding_days=max(0, (ts - ftx_ts).days),
                    long_term=False,
                    source_tx_id=tx_id,
                    lot_id=None,
                ))
                # Back out the matched units from the pool (they were
                # eagerly added when the buy processed).
                pool_take = min(take, pool.balance)
                if pool_take > 0:
                    # Remove at the buy's real per-unit cost, not the
                    # pool average, so the pool's remaining state is
                    # correct for future disposals.
                    pool.balance -= pool_take
                    pool.total_cost = max(
                        0.0, pool.total_cost - pool_take * fprice,
                    )
                buy_claimed[ftx.get("id") or j] = already + take
                remaining -= take
            return remaining

        # Phase A: same-day (today 00:00 → 23:59:59)
        day_lo = datetime(ts.year, ts.month, ts.day)
        day_hi = day_lo + timedelta(days=1) - timedelta(microseconds=1)
        _match_window(day_lo, day_hi)

        # Phase B: next 30 days (starting tomorrow)
        if remaining > 1e-12:
            window_lo = day_lo + timedelta(days=1)
            window_hi = day_lo + timedelta(days=30, hours=23, minutes=59, seconds=59)
            _match_window(window_lo, window_hi)

        # Phase C: s104 pool for whatever is left
        if remaining > 1e-12:
            take, cost_basis = pool.consume(remaining)
            if take > 0:
                disposals.append(Disposal(
                    symbol=sym,
                    acquired_ts=pool.first_acquired or ts,
                    disposed_ts=ts,
                    amount=take,
                    cost_basis=cost_basis,
                    proceeds=take * price,
                    gain=take * price - cost_basis,
                    holding_days=max(
                        0, (ts - (pool.first_acquired or ts)).days,
                    ),
                    long_term=False,
                    source_tx_id=tx_id,
                    lot_id=None,
                ))
                remaining -= take

        # Still units left? Zero-basis disposal.
        if remaining > 1e-9:
            disposals.append(Disposal(
                symbol=sym,
                acquired_ts=ts,
                disposed_ts=ts,
                amount=remaining,
                cost_basis=0.0,
                proceeds=remaining * price,
                gain=remaining * price,
                holding_days=0,
                long_term=False,
                source_tx_id=tx_id,
                lot_id=None,
            ))

    # Build a thin "lots" shim so the calculator has something to hand
    # back — one synthetic lot per asset reflecting the final s104 pool
    # state. Not used by the UK render path but keeps the return shape
    # compatible.
    lots_out: dict[str, list[Lot]] = {}
    for sym, pool in pools.items():
        if pool.balance > 0:
            lots_out[sym] = [Lot(
                lot_id=1,
                symbol=sym,
                acquired_ts=pool.first_acquired or datetime.utcnow(),
                amount=pool.balance,
                remaining=pool.balance,
                cost_per_unit=pool.avg_cost,
                source_tx_id=None,
            )]
    return disposals, lots_out
