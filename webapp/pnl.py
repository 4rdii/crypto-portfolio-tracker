"""Cost basis + P&L computation from transactions.

Uses weighted average cost basis:
  - buy / deposit  → adds to position (cost increases)
  - sell / withdraw → reduces position, realizes P&L at current avg cost

Realized P&L = sum of (sell_price - avg_cost_at_time) * sold_amount
Unrealized P&L = (current_price - current_avg_cost) * current_balance
"""
from collections import defaultdict


BUY_TYPES = {"buy", "deposit"}
SELL_TYPES = {"sell", "withdraw"}

# Stablecoins trade at ~$1 by design. Tiny price deviations from Moralis/CG
# (e.g. $0.9997, $1.0018) create phantom realized P&L when you move them
# across wallets. Force them to exactly $1.00 to eliminate the noise.
STABLECOINS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "FDUSD", "PYUSD", "GUSD",
    "USDE", "SUSDE", "FRAX", "LUSD", "USDD", "USDP", "USDB", "USDS",
    "AXLUSDC", "USDC.E", "USDBC", "CRVUSD", "GHO", "EURS", "EURT",
    "USDT.E", "MUSD", "XUSD", "DOLA",
}


def compute_cost_basis(transactions: list[dict]) -> dict:
    """Walk transactions in chronological order, return per-symbol state.

    Returns per-symbol dicts with:
      balance              current units held
      avg_cost             weighted avg cost of REMAINING units (0 when closed)
      total_cost           total $ still on the books for remaining units
      realized_pnl         cumulative realized P&L over the full tx history
      total_bought         cumulative units bought
      total_sold           cumulative units sold
      total_buy_cost       cumulative $ spent buying (used for
                           closed-position "avg buy price" display)
      total_sell_proceeds  cumulative $ received selling (paired with avg
                           sell price display and for the total-return sanity
                           check that total_sell_proceeds - total_buy_cost ≈
                           realized_pnl for fully-closed positions)
    """
    # Ensure chronological order (oldest first)
    txs = sorted(transactions, key=lambda t: t["ts"])
    state = defaultdict(lambda: {
        "balance": 0.0,
        "avg_cost": 0.0,
        "total_cost": 0.0,
        "realized_pnl": 0.0,
        "total_bought": 0.0,
        "total_sold": 0.0,
        "total_buy_cost": 0.0,
        "total_sell_proceeds": 0.0,
    })

    for tx in txs:
        sym = (tx["symbol"] or "").upper()
        if not sym:
            continue
        ttype = tx["tx_type"].lower()
        amt = float(tx["amount"] or 0)
        price = float(tx["price_usd"] or 0)
        # Normalize stablecoins to exactly $1 to kill phantom P&L noise
        if sym in STABLECOINS:
            price = 1.0
        s = state[sym]

        if ttype in BUY_TYPES:
            # Weighted average cost update
            new_bal = s["balance"] + amt
            new_cost = s["total_cost"] + amt * price
            s["balance"] = new_bal
            s["total_cost"] = new_cost
            s["avg_cost"] = new_cost / new_bal if new_bal > 0 else 0
            s["total_bought"] += amt
            s["total_buy_cost"] += amt * price

        elif ttype in SELL_TYPES:
            if s["balance"] <= 0:
                # Sold more than we have — no cost basis on the books (likely a
                # position opened before the history window we imported). Record
                # the disposition but do NOT fabricate realized profit against
                # $0 cost, which would create phantom P&L.
                s["total_sold"] += amt
                s["total_sell_proceeds"] += amt * price
                continue
            sold = min(amt, s["balance"])
            # Realized = (sell price - avg cost at time) * sold amount
            s["realized_pnl"] += (price - s["avg_cost"]) * sold
            # Reduce balance + total cost proportionally (keeps avg_cost stable)
            s["total_cost"] -= s["avg_cost"] * sold
            s["balance"] -= sold
            s["total_sold"] += sold
            s["total_sell_proceeds"] += sold * price
            if s["balance"] <= 1e-9:
                # Position is closed. Zero out the "remaining" trackers but
                # KEEP total_bought/total_sold/total_buy_cost/total_sell_proceeds
                # so the UI can still render the position's full lifetime.
                s["balance"] = 0
                s["total_cost"] = 0
                s["avg_cost"] = 0

        elif ttype == "swap":
            # Swap treated as a sell of the "from" at price_usd.
            # For simplicity, swap is logged as two transactions: one sell + one buy.
            # If a single "swap" row appears, treat as-is reducing balance.
            if amt > 0 and s["balance"] > 0:
                sold = min(amt, s["balance"])
                s["realized_pnl"] += (price - s["avg_cost"]) * sold
                s["total_cost"] -= s["avg_cost"] * sold
                s["balance"] -= sold
                s["total_sold"] += sold
                s["total_sell_proceeds"] += sold * price

    return dict(state)


def merge_holdings_with_cost_basis(holdings_by_symbol: list[dict],
                                   transactions: list[dict]) -> list[dict]:
    """Combine on-chain balances with cost basis math to produce full P&L rows.

    For each symbol we have live balance + price. We look up cost basis from txs
    and compute unrealized P&L on the *actual* on-chain balance (not the balance
    the txs imply — the on-chain number is the source of truth for holdings).
    """
    cost = compute_cost_basis(transactions)
    # Symbols that have holdings but no tx history will still appear
    # Symbols with tx history but no live holding (fully sold) also included
    out = []
    seen = set()

    for h in holdings_by_symbol:
        sym = h["symbol"].upper()
        seen.add(sym)
        cb = cost.get(sym, {})
        avg_cost = cb.get("avg_cost", 0)
        balance = float(h.get("balance") or 0)
        price = float(h.get("usd_price") or 0)
        current_value = float(h.get("usd_value") or 0)
        # Force stables to $1 flat — eliminates drift noise in holdings too
        if sym in STABLECOINS:
            price = 1.0
            current_value = balance
            avg_cost = 1.0 if balance > 0 else 0
        cost_basis_value = avg_cost * balance
        unrealized = current_value - cost_basis_value if avg_cost > 0 else 0
        unrealized_pct = (
            (unrealized / cost_basis_value * 100) if cost_basis_value > 0 else 0
        )
        out.append({
            "symbol": sym,
            "name": h.get("name") or sym,
            "chains": h.get("chains") or "",
            # "protocols" is a comma-separated string of all protocols
            # contributing to this aggregated row. "wallet" = plain wallet
            # holding; "aave" = deposited in Aave (earning yield). UI uses
            # this to render a protocol badge next to the symbol.
            "protocols": h.get("protocols") or "wallet",
            # Per-wallet contribution rows: list of {wallet_id, label,
            # balance, value, protocol, chain}. Used by the holdings table
            # to render wallet badges beside each symbol.
            "wallet_breakdown": h.get("wallet_breakdown") or [],
            "balance": balance,
            "usd_price": price,
            "current_value": current_value,
            "avg_cost": avg_cost,
            "cost_basis_value": cost_basis_value,
            "unrealized_pnl": unrealized,
            "unrealized_pnl_pct": unrealized_pct,
            "realized_pnl": cb.get("realized_pnl", 0),
            "price_24h_change": float(h.get("price_24h_change") or 0),
            "has_cost_basis": avg_cost > 0,
        })

    # Include fully-closed positions (for realized P&L history).
    # A closed position is one where the user bought AND sold the asset during
    # the imported history window, and no current on-chain balance remains.
    # We want to surface these so "I bought ETH, sold it, profited $500" isn't
    # invisible just because ETH isn't in the wallet anymore.
    #
    # Skip ghost positions: symbols with tx history but no current value AND
    # no meaningful realized P&L — these are almost always scam airdrops we
    # never sold (so there's no real cost basis and no realized pnl worth showing).
    for sym, cb in cost.items():
        if sym in seen:
            continue
        realized = cb.get("realized_pnl", 0)
        total_bought = cb.get("total_bought", 0)
        total_sold = cb.get("total_sold", 0)
        total_buy_cost = cb.get("total_buy_cost", 0)
        total_sell_proceeds = cb.get("total_sell_proceeds", 0)

        # Must have both a buy AND a sell to count as a "closed" trade.
        # Bought-but-never-sold positions are either held (and hidden by the
        # dust filter) or scam airdrops with $0 cost — neither deserves a row.
        if total_bought <= 0 or total_sold <= 0:
            continue
        # Require meaningful realized P&L. A USDC round-trip or any stablecoin
        # in/out will legitimately show $0 realized — not worth a row. This
        # also drops the case where a tiny scam airdrop was dumped for dust.
        if abs(realized) < 1.0:
            continue

        # Lifetime weighted average buy/sell prices. These are the most
        # meaningful numbers for a closed position — they tell the user
        # "you bought at an average of $X and sold at an average of $Y".
        avg_buy_price = (total_buy_cost / total_bought) if total_bought > 0 else 0
        avg_sell_price = (total_sell_proceeds / total_sold) if total_sold > 0 else 0

        out.append({
            "symbol": sym,
            "name": sym,
            "chains": "",
            "protocols": "wallet",
            "wallet_breakdown": [],
            "balance": 0,
            "usd_price": avg_sell_price,
            "current_value": 0,
            # avg_cost == lifetime avg buy price for closed rows (drives the
            # "Avg Cost" column without the template needing special casing).
            "avg_cost": avg_buy_price,
            "cost_basis_value": total_buy_cost,
            "unrealized_pnl": 0,
            "unrealized_pnl_pct": 0,
            "realized_pnl": realized,
            "price_24h_change": 0,
            "has_cost_basis": True,
            # Closed-position specific fields so the UI can render a lifetime
            # summary instead of the current-balance layout.
            "closed": True,
            "total_bought": total_bought,
            "total_sold": total_sold,
            "total_buy_cost": total_buy_cost,
            "total_sell_proceeds": total_sell_proceeds,
            "avg_buy_price": avg_buy_price,
            "avg_sell_price": avg_sell_price,
        })

    # Also flag open rows as not closed so the template can branch safely.
    for r in out:
        r.setdefault("closed", False)

    return out


def portfolio_summary(rows: list[dict]) -> dict:
    # Open rows drive total value / cost / unrealized. Closed rows only
    # contribute realized P&L — including their lifetime buy cost in
    # "total_cost_basis" would double-count money that's already been
    # recycled back into the portfolio and destroy the % calculation.
    open_rows = [r for r in rows if not r.get("closed")]
    total_value = sum(r["current_value"] for r in open_rows)
    total_cost = sum(r["cost_basis_value"] for r in open_rows if r["has_cost_basis"])
    unrealized = sum(r["unrealized_pnl"] for r in open_rows if r["has_cost_basis"])
    realized = sum(r["realized_pnl"] for r in rows)
    pnl_pct = (unrealized / total_cost * 100) if total_cost > 0 else 0
    return {
        "total_value": total_value,
        "total_cost_basis": total_cost,
        "unrealized_pnl": unrealized,
        "realized_pnl": realized,
        "total_pnl": unrealized + realized,
        "unrealized_pnl_pct": pnl_pct,
    }
