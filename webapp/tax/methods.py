"""Per-lot cost basis engine — the heart of the tax report.

Unlike `pnl.py`, which uses a running weighted average (good enough for
a "how much did I make" dashboard), tax reporting needs per-lot tracking.
Every buy is a lot with its own cost basis and acquisition date; every
sell is matched against one or more lots producing *disposals* with
  - cost basis (what that chunk of the lot cost)
  - proceeds (what we sold it for)
  - gain / loss
  - holding period (short or long term, jurisdiction dependent)

The five supported methods differ only in which lot(s) get picked when
a sale occurs:

  FIFO  (First In, First Out)   oldest lot first — most tax-unfriendly
                                in a bull market, the default in most
                                jurisdictions when no method is chosen.
  LIFO  (Last In, First Out)    newest lot first — illegal in many
                                jurisdictions but still commonly requested.
  HIFO  (Highest In, First Out) highest-cost lot first — minimizes gains,
                                maximizes losses, the power user favorite.
  AVG   (Average cost)          collapses every open lot into one weighted
                                average lot. Simple but gives up
                                optimization headroom.
  SPEC  (Specific ID)           caller supplies lot_id → amount mapping.
                                The most flexible but only legal with
                                explicit documentation at time of sale.

The functions in this module are PURE: they take a transaction list and
emit lots + disposals. No I/O, no DB, no side effects — that makes them
trivially testable and lets the calculator layer cache / parallelize
safely.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable


BUY_TYPES = {"buy", "deposit"}
SELL_TYPES = {"sell", "withdraw"}


# Wrapped → underlying canonicalization. Mirrors the price-cache rules in
# binance_prices.py so tax lots treat WBTC the same as BTC (a user who
# bridges BTC ↔ WBTC shouldn't trigger a taxable event).
_CANONICAL_SYMBOL = {
    # Bitcoin family
    "WBTC": "BTC", "CBBTC": "BTC", "TBTC": "BTC", "BTCB": "BTC",
    "HBTC": "BTC", "RENBTC": "BTC", "SBTC": "BTC",
    # Ethereum + LSTs
    "WETH": "ETH", "STETH": "ETH", "WSTETH": "ETH", "RETH": "ETH",
    "CBETH": "ETH", "WEETH": "ETH", "EZETH": "ETH", "RSETH": "ETH",
    "PUFETH": "ETH", "SETH": "ETH", "OSETH": "ETH", "ANKRETH": "ETH",
    "SWETH": "ETH",
    # Solana LSTs
    "WSOL": "SOL", "JITOSOL": "SOL", "MSOL": "SOL", "BNSOL": "SOL",
    "JUPSOL": "SOL", "BSOL": "SOL",
    # Others
    "WBNB": "BNB", "WMATIC": "MATIC", "WPOL": "POL", "WAVAX": "AVAX",
    "WFTM": "FTM", "CBXRP": "XRP", "WTRX": "TRX", "WDOGE": "DOGE",
}

STABLECOINS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "PYUSD", "GUSD",
    "USDE", "SUSDE", "FRAX", "LUSD", "USDD", "USDP", "USDB", "USDS",
    "AXLUSDC", "USDC.E", "USDBC", "CRVUSD", "GHO", "EURS", "EURT",
    "USDT.E", "MUSD", "XUSD", "DOLA",
}


def canonical(symbol: str) -> str:
    """Return BTC for WBTC, ETH for WETH etc. Unknown symbols pass through."""
    if not symbol:
        return ""
    s = symbol.upper()
    return _CANONICAL_SYMBOL.get(s, s)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Lot:
    """A parcel of units acquired at a single cost + time.

    `remaining` shrinks as the lot is consumed by sales. Once it hits 0 the
    lot is effectively closed but we keep it in the ledger for audit.
    """
    lot_id: int            # monotonic within a symbol's ledger
    symbol: str            # CANONICAL symbol (BTC not WBTC)
    acquired_ts: datetime  # when the buy happened
    amount: float          # original units (never mutated)
    remaining: float       # units still in this lot
    cost_per_unit: float   # USD per unit at acquisition
    source_tx_id: int | None = None  # link back to the originating tx


@dataclass
class Disposal:
    """One slice of a sale matched against one lot.

    A single "sell 2 BTC" transaction can produce multiple Disposal rows
    if the 2 BTC comes out of more than one lot. The UI renders them as a
    nested breakdown under the sale.
    """
    symbol: str
    acquired_ts: datetime      # lot acquisition
    disposed_ts: datetime      # sale time
    amount: float              # units sold from this lot
    cost_basis: float          # USD cost of those units
    proceeds: float            # USD received for those units
    gain: float                # proceeds - cost_basis
    holding_days: int          # disposed - acquired, in whole days
    long_term: bool            # jurisdiction-set flag (fills in later)
    source_tx_id: int | None = None
    lot_id: int | None = None


# ---------------------------------------------------------------------------
# Core walker
# ---------------------------------------------------------------------------

def _parse_ts(ts) -> datetime:
    """Coerce whatever the DB gives us into an aware-naive datetime.

    SQLite returns strings for TIMESTAMP columns unless the row factory
    does the conversion; real-world mix of ISO formats survives this.
    """
    if isinstance(ts, datetime):
        # Normalize to naive UTC — mixing aware/naive breaks comparisons
        # in the UK matcher (which constructs naive day-boundary datetimes).
        return ts.replace(tzinfo=None) if ts.tzinfo is not None else ts
    if isinstance(ts, str):
        # Accept "2024-05-01 12:34:56" or "2024-05-01T12:34:56" or
        # with trailing Z / offset.
        s = ts.strip().replace("Z", "")
        if "T" not in s and " " in s:
            s = s.replace(" ", "T", 1)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            # Last-ditch: strip microseconds + tz
            dt = datetime.fromisoformat(s.split("+")[0].split(".")[0])
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    raise ValueError(f"unparseable timestamp: {ts!r}")


# ---------------------------------------------------------------------------
# Lot-picking strategies
# ---------------------------------------------------------------------------
#
# Each picker is a function: (open_lots: list[Lot]) -> Iterable[Lot]
# yielding lots in the order they should be consumed. We use generators
# so the walker can stop as soon as the sale amount is filled.
# The `open_lots` list is sorted oldest-first; pickers re-sort as needed.

def _pick_fifo(open_lots: list[Lot]) -> Iterable[Lot]:
    # Already oldest-first, just yield.
    yield from open_lots


def _pick_lifo(open_lots: list[Lot]) -> Iterable[Lot]:
    yield from reversed(open_lots)


def _pick_hifo(open_lots: list[Lot]) -> Iterable[Lot]:
    # Highest cost per unit first. Ties broken by newest first — doesn't
    # really matter, but deterministic.
    yield from sorted(
        open_lots,
        key=lambda lot: (-lot.cost_per_unit, -lot.lot_id),
    )


PICKERS: dict[str, Callable[[list[Lot]], Iterable[Lot]]] = {
    "FIFO": _pick_fifo,
    "LIFO": _pick_lifo,
    "HIFO": _pick_hifo,
}


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_disposals(
    transactions: list[dict],
    method: str = "FIFO",
    spec_id_map: dict[int, dict[int, float]] | None = None,
) -> tuple[list[Disposal], dict[str, list[Lot]]]:
    """Walk `transactions` chronologically and emit disposals + open lots.

    Args:
        transactions: list of tx dicts with keys id, ts, tx_type, symbol,
            amount, price_usd. Symbols are canonicalized on the way in.
        method: one of FIFO / LIFO / HIFO / AVG / SPEC.
        spec_id_map: only for SPEC — tx_id → {lot_id: amount} telling us
            exactly which lots to consume for each sell. Missing or
            partially specified sells fall back to FIFO for the remainder.

    Returns:
        (disposals_list, lots_by_symbol) where lots_by_symbol[sym] is every
        lot ever opened (closed + still open). Holding-period classification
        (short vs long term) is NOT applied here — that's jurisdiction-
        dependent and handled in the calculator.
    """
    method = method.upper()
    if method not in ("FIFO", "LIFO", "HIFO", "AVG", "SPEC"):
        raise ValueError(f"unknown method: {method}")

    # Stable chronological sort. Ties broken by original insertion order
    # so two txs at the same second behave deterministically.
    txs = sorted(transactions, key=lambda t: (_parse_ts(t["ts"]), t.get("id") or 0))

    lots_by_symbol: dict[str, list[Lot]] = {}
    next_lot_id_by_symbol: dict[str, int] = {}
    disposals: list[Disposal] = []

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
        ts = _parse_ts(tx["ts"])
        tx_id = tx.get("id")

        lots = lots_by_symbol.setdefault(sym, [])
        next_id = next_lot_id_by_symbol.get(sym, 1)

        # ---- BUY: open a new lot ----------------------------------------
        if ttype in BUY_TYPES:
            lot = Lot(
                lot_id=next_id,
                symbol=sym,
                acquired_ts=ts,
                amount=amt,
                remaining=amt,
                cost_per_unit=price,
                source_tx_id=tx_id,
            )
            lots.append(lot)
            next_lot_id_by_symbol[sym] = next_id + 1
            continue

        # ---- SELL: consume lots according to method ----------------------
        if ttype in SELL_TYPES or ttype == "swap":
            open_lots = [lot for lot in lots if lot.remaining > 1e-12]
            if not open_lots:
                # Sold without a cost basis on record. Emit a disposal with
                # zero cost — the tax report will flag it as "no basis"
                # rather than silently dropping it. We use the sale ts for
                # both dates and holding_days=0 (treated short-term).
                disposals.append(Disposal(
                    symbol=sym,
                    acquired_ts=ts,
                    disposed_ts=ts,
                    amount=amt,
                    cost_basis=0.0,
                    proceeds=amt * price,
                    gain=amt * price,
                    holding_days=0,
                    long_term=False,
                    source_tx_id=tx_id,
                    lot_id=None,
                ))
                continue

            remaining_to_sell = amt

            if method == "AVG":
                # Collapse all open lots into one weighted-average lot for
                # purposes of THIS sale. We still drain individual lots
                # proportionally so the per-lot ledger stays consistent
                # (this matches how most "average cost" jurisdictions
                # actually handle it — the pool is shared but withdrawals
                # reduce every lot's remaining proportionally).
                total_remaining = sum(lot.remaining for lot in open_lots)
                if total_remaining <= 0:
                    continue
                avg_cost = sum(
                    lot.remaining * lot.cost_per_unit for lot in open_lots
                ) / total_remaining
                sold = min(amt, total_remaining)
                # Drain proportionally.
                for lot in open_lots:
                    share = (lot.remaining / total_remaining) * sold
                    lot.remaining = max(0.0, lot.remaining - share)
                disposals.append(Disposal(
                    symbol=sym,
                    # In AVG the "acquired" ts isn't meaningful — use the
                    # earliest open lot's ts so long-term classification
                    # still works the way most filers expect (if ANY of
                    # the pool has been held >1y, the whole disposal is
                    # long-term).
                    acquired_ts=min(lot.acquired_ts for lot in open_lots),
                    disposed_ts=ts,
                    amount=sold,
                    cost_basis=avg_cost * sold,
                    proceeds=price * sold,
                    gain=(price - avg_cost) * sold,
                    holding_days=(
                        ts - min(lot.acquired_ts for lot in open_lots)
                    ).days,
                    long_term=False,  # set later
                    source_tx_id=tx_id,
                    lot_id=None,
                ))
                continue

            if method == "SPEC" and spec_id_map and tx_id in spec_id_map:
                # Consume exactly what the caller requested, in lot_id order.
                picks = spec_id_map[tx_id]
                for lot in open_lots:
                    if remaining_to_sell <= 1e-12:
                        break
                    want = picks.get(lot.lot_id, 0.0)
                    if want <= 0:
                        continue
                    take = min(want, lot.remaining, remaining_to_sell)
                    if take <= 0:
                        continue
                    _emit_disposal(
                        disposals, lot, ts, take, price, tx_id,
                    )
                    lot.remaining -= take
                    remaining_to_sell -= take
                # Fall through: anything left over uses FIFO (so a
                # partial spec-id plan still produces a complete report).

            picker = PICKERS.get(method if method != "SPEC" else "FIFO")
            for lot in picker(open_lots):
                if remaining_to_sell <= 1e-12:
                    break
                if lot.remaining <= 1e-12:
                    continue
                take = min(lot.remaining, remaining_to_sell)
                _emit_disposal(disposals, lot, ts, take, price, tx_id)
                lot.remaining -= take
                remaining_to_sell -= take

            if remaining_to_sell > 1e-9:
                # Sold more than we held — emit a zero-basis disposal for
                # the leftover so the totals reconcile.
                disposals.append(Disposal(
                    symbol=sym,
                    acquired_ts=ts,
                    disposed_ts=ts,
                    amount=remaining_to_sell,
                    cost_basis=0.0,
                    proceeds=remaining_to_sell * price,
                    gain=remaining_to_sell * price,
                    holding_days=0,
                    long_term=False,
                    source_tx_id=tx_id,
                    lot_id=None,
                ))

    return disposals, lots_by_symbol


def _emit_disposal(
    disposals: list[Disposal],
    lot: Lot,
    sale_ts: datetime,
    take: float,
    sale_price: float,
    tx_id: int | None,
) -> None:
    """Helper: push one slice of a lot onto the disposals list."""
    cost_basis = lot.cost_per_unit * take
    proceeds = sale_price * take
    disposals.append(Disposal(
        symbol=lot.symbol,
        acquired_ts=lot.acquired_ts,
        disposed_ts=sale_ts,
        amount=take,
        cost_basis=cost_basis,
        proceeds=proceeds,
        gain=proceeds - cost_basis,
        holding_days=(sale_ts - lot.acquired_ts).days,
        long_term=False,  # calculator sets this per jurisdiction
        source_tx_id=tx_id,
        lot_id=lot.lot_id,
    ))


# Canonical list of supported methods. Exposed via __init__ so the UI
# picker and API validator can share a single source of truth.
METHODS = ["FIFO", "LIFO", "HIFO", "AVG", "SPEC"]
