#!/usr/bin/env python3
"""Family Fund Management System.

Share-based (mutual fund style) multi-person fund tracker.
Each person holds "shares" of a pooled portfolio. Deposits mint shares
at current NAV/share, withdrawals burn them.

Wallets are split into two types:
  - POOLED: shared wallets where ownership is split by shares
  - PERSONAL: wallets fully owned by one person (e.g. "mitra wallet")

Usage:
    python fund.py status                       # Show all shareholders & values
    python fund.py nav                          # Show current NAV & share price
    python fund.py deposit <person> <usd_amt>   # Record a deposit
    python fund.py withdraw <person> <usd_amt>  # Record a withdrawal
    python fund.py history [person]             # Show transaction history
    python fund.py add-person <name> [shares]   # Add a new shareholder
    python fund.py set-personal <wallet_id> <person>  # Mark wallet as personal
    python fund.py rebalance                    # Recalculate from current portfolio
    python fund.py init                         # Initialize from current state
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "portfolio.db"


# ─── Schema ────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS fund_shareholders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    shares REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, name)
);

CREATE TABLE IF NOT EXISTS fund_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shareholder_id INTEGER NOT NULL REFERENCES fund_shareholders(id),
    type TEXT NOT NULL CHECK(type IN ('deposit','withdrawal','init')),
    usd_amount REAL NOT NULL,
    shares_delta REAL NOT NULL,
    nav_per_share REAL NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fund_wallet_ownership (
    wallet_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    ownership_type TEXT NOT NULL CHECK(ownership_type IN ('pooled','personal')),
    owner_name TEXT,  -- only for personal wallets
    FOREIGN KEY (wallet_id) REFERENCES wallets(id)
);
"""

# Migration: add user_id columns if they don't exist yet (backward compat)
MIGRATIONS = [
    "ALTER TABLE fund_shareholders ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE fund_wallet_ownership ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1",
]


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    # Run migrations (safe to re-run — ALTER TABLE fails silently if col exists)
    for sql in MIGRATIONS:
        try:
            conn.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    # Drop the old unique constraint on name only (replaced by user_id+name)
    # SQLite doesn't support DROP CONSTRAINT, but the CREATE TABLE IF NOT EXISTS
    # with the new schema handles new installs. Existing DBs keep the old index
    # which is fine — the (user_id, name) UNIQUE in the schema is for new tables.
    return conn


# ─── Portfolio value helpers ───────────────────────────────────────

def get_pooled_value(conn, user_id=1):
    """Sum USD value of all pooled wallets for a user."""
    rows = conn.execute("""
        SELECT COALESCE(SUM(h.usd_value), 0) as total
        FROM holdings h
        JOIN fund_wallet_ownership fo ON h.wallet_id = fo.wallet_id
        WHERE fo.ownership_type = 'pooled' AND fo.user_id = ?
    """, (user_id,)).fetchone()
    return rows["total"] if rows else 0.0


def get_personal_value(conn, person_name, user_id=1):
    """Sum USD value of personal wallets owned by a person."""
    rows = conn.execute("""
        SELECT COALESCE(SUM(h.usd_value), 0) as total
        FROM holdings h
        JOIN fund_wallet_ownership fo ON h.wallet_id = fo.wallet_id
        WHERE fo.ownership_type = 'personal' AND LOWER(fo.owner_name) = LOWER(?)
          AND fo.user_id = ?
    """, (person_name, user_id)).fetchone()
    return rows["total"] if rows else 0.0


def get_total_shares(conn, user_id=1):
    """Sum of all shares across shareholders for a user."""
    row = conn.execute(
        "SELECT COALESCE(SUM(shares), 0) as total FROM fund_shareholders WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    return row["total"]


def get_nav_per_share(conn, user_id=1):
    """Current NAV per share = pooled_value / total_shares."""
    total_shares = get_total_shares(conn, user_id)
    if total_shares <= 0:
        return 1.0  # initial
    pooled = get_pooled_value(conn, user_id)
    return pooled / total_shares


# ─── Commands ──────────────────────────────────────────────────────

def cmd_init(args):
    """Initialize fund from current portfolio state with share distribution."""
    conn = get_db()

    # Check if already initialized (CLI always uses user_id=1)
    existing = conn.execute("SELECT COUNT(*) as c FROM fund_shareholders WHERE user_id = 1").fetchone()["c"]
    if existing > 0 and not args.force:
        print("Fund already initialized. Use --force to reinitialize.")
        return

    if args.force:
        conn.execute("DELETE FROM fund_transactions")
        conn.execute("DELETE FROM fund_shareholders")
        conn.execute("DELETE FROM fund_wallet_ownership")
        conn.commit()

    # Get all wallets for user_id=1
    wallets = conn.execute("""
        SELECT id, address, label FROM wallets WHERE user_id = 1
    """).fetchall()

    # Share distribution (percentages of pooled wallets).
    # Edit these to match your actual shareholders before running init.
    # Example structure — replace with real names and percentages (must sum to 100).
    shareholders = {
        "Person1": 85.14,
        "Person2":  0.45,
        "Person3":  1.74,
        "Person4":  2.04,
        "Person5":  1.24,
        "Expenses":  5.59,
        "Person6":  3.81,
    }

    # Personal wallets (fully owned, NOT part of the pool).
    # Map wallet label (lowercase) → shareholder name.
    personal_wallets = {
        # "my wallet": "Person1",
    }

    # Classify wallets
    for w in wallets:
        label_lower = w["label"].lower() if w["label"] else ""
        if label_lower in personal_wallets:
            owner = personal_wallets[label_lower]
            conn.execute(
                "INSERT OR REPLACE INTO fund_wallet_ownership (wallet_id, user_id, ownership_type, owner_name) VALUES (?, 1, 'personal', ?)",
                (w["id"], owner)
            )
            print(f"  Personal: {w['label']} → {owner}")
        else:
            conn.execute(
                "INSERT OR REPLACE INTO fund_wallet_ownership (wallet_id, user_id, ownership_type, owner_name) VALUES (?, 1, 'pooled', NULL)",
                (w["id"],)
            )
            print(f"  Pooled:   {w['label']}")

    conn.commit()

    # Get pooled value
    pooled_value = get_pooled_value(conn)
    print(f"\nPooled portfolio value: ${pooled_value:,.2f}")

    # Also add Mitra as shareholder (she has personal wallet only, 0 pool shares)
    all_people = {**shareholders, "Mitra": 0.0}

    # Create shareholders and record initial positions
    total_pct = sum(shareholders.values())
    for name, pct in all_people.items():
        # Normalize percentages so they sum to 100
        normalized_pct = (pct / total_pct * 100) if total_pct > 0 else 0
        shares = pct  # Use percentage points as share units
        usd_value = pooled_value * (pct / total_pct) if total_pct > 0 else 0

        conn.execute(
            "INSERT INTO fund_shareholders (user_id, name, shares) VALUES (1, ?, ?)",
            (name, shares)
        )
        sh_id = conn.execute(
            "SELECT id FROM fund_shareholders WHERE user_id = 1 AND name = ?", (name,)
        ).fetchone()["id"]

        if shares > 0:
            conn.execute("""
                INSERT INTO fund_transactions
                (shareholder_id, type, usd_amount, shares_delta, nav_per_share, note)
                VALUES (?, 'init', ?, ?, ?, 'Initial allocation from Google Sheet')
            """, (sh_id, usd_value, shares, pooled_value / total_pct if total_pct > 0 else 1.0))

        personal = get_personal_value(conn, name)
        total = usd_value + personal
        print(f"  {name}: {shares:.2f} shares = ${usd_value:,.2f} pooled"
              + (f" + ${personal:,.2f} personal" if personal > 0 else "")
              + f" = ${total:,.2f} total")

    conn.commit()
    print(f"\nFund initialized with {len(all_people)} shareholders.")
    print(f"NAV/share: ${pooled_value / total_pct:.2f}" if total_pct > 0 else "")


def cmd_status(args):
    """Show current status of all shareholders."""
    conn = get_db()
    pooled = get_pooled_value(conn, user_id=1)
    total_shares = get_total_shares(conn, user_id=1)
    nav = get_nav_per_share(conn, user_id=1)

    print(f"{'═' * 70}")
    print(f"  FAMILY FUND STATUS — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'═' * 70}")
    print(f"  Pooled Portfolio Value:  ${pooled:>12,.2f}")
    print(f"  Total Shares:           {total_shares:>12,.2f}")
    print(f"  NAV per Share:          ${nav:>12,.2f}")
    print(f"{'─' * 70}")
    print(f"  {'Name':<12} {'Shares':>10} {'Pool %':>8} {'Pool Value':>12} {'Personal':>12} {'Total':>12}")
    print(f"{'─' * 70}")

    shareholders = conn.execute(
        "SELECT * FROM fund_shareholders WHERE user_id = 1 ORDER BY shares DESC"
    ).fetchall()

    grand_total = 0
    for sh in shareholders:
        pool_val = sh["shares"] * nav if total_shares > 0 else 0
        pool_pct = (sh["shares"] / total_shares * 100) if total_shares > 0 else 0
        personal = get_personal_value(conn, sh["name"], user_id=1)
        total = pool_val + personal
        grand_total += total
        personal_str = f"${personal:>10,.2f}" if personal > 0 else f"{'—':>11}"
        print(f"  {sh['name']:<12} {sh['shares']:>10,.2f} {pool_pct:>7.1f}% ${pool_val:>10,.2f} {personal_str} ${total:>10,.2f}")

    print(f"{'─' * 70}")
    print(f"  {'TOTAL':<12} {total_shares:>10,.2f} {'100.0%':>8} ${pooled:>10,.2f} {'':>12} ${grand_total:>10,.2f}")
    print(f"{'═' * 70}")


def cmd_nav(args):
    """Show current NAV details."""
    conn = get_db()
    pooled = get_pooled_value(conn, user_id=1)
    total_shares = get_total_shares(conn, user_id=1)
    nav = get_nav_per_share(conn, user_id=1)

    print(f"Pooled Value: ${pooled:,.2f}")
    print(f"Total Shares: {total_shares:,.2f}")
    print(f"NAV/Share:    ${nav:,.4f}")

    # Show pooled wallets
    wallets = conn.execute("""
        SELECT w.label, w.address, COALESCE(SUM(h.usd_value), 0) as value
        FROM wallets w
        JOIN fund_wallet_ownership fo ON w.id = fo.wallet_id
        LEFT JOIN holdings h ON w.id = h.wallet_id
        WHERE fo.ownership_type = 'pooled' AND fo.user_id = 1
        GROUP BY w.id
        ORDER BY value DESC
    """).fetchall()

    print(f"\nPooled wallets:")
    for w in wallets:
        print(f"  {w['label']:<20} ${w['value']:>12,.2f}")


def cmd_deposit(args):
    """Record a deposit — mints new shares at current NAV."""
    conn = get_db()
    nav = get_nav_per_share(conn, user_id=1)
    amount = args.amount

    sh = conn.execute(
        "SELECT * FROM fund_shareholders WHERE user_id = 1 AND LOWER(name) = LOWER(?)",
        (args.person,)
    ).fetchone()

    if not sh:
        print(f"Error: '{args.person}' not found. Use add-person first.")
        return

    new_shares = amount / nav
    conn.execute(
        "UPDATE fund_shareholders SET shares = shares + ? WHERE id = ?",
        (new_shares, sh["id"])
    )
    conn.execute("""
        INSERT INTO fund_transactions
        (shareholder_id, type, usd_amount, shares_delta, nav_per_share, note)
        VALUES (?, 'deposit', ?, ?, ?, ?)
    """, (sh["id"], amount, new_shares, nav, args.note or ""))
    conn.commit()

    print(f"Deposit: {sh['name']} +${amount:,.2f} → +{new_shares:,.4f} shares @ ${nav:,.4f}/share")
    print(f"New total: {sh['shares'] + new_shares:,.4f} shares")


def cmd_withdraw(args):
    """Record a withdrawal — burns shares at current NAV."""
    conn = get_db()
    nav = get_nav_per_share(conn, user_id=1)
    amount = args.amount

    sh = conn.execute(
        "SELECT * FROM fund_shareholders WHERE user_id = 1 AND LOWER(name) = LOWER(?)",
        (args.person,)
    ).fetchone()

    if not sh:
        print(f"Error: '{args.person}' not found.")
        return

    shares_to_burn = amount / nav
    if shares_to_burn > sh["shares"]:
        print(f"Error: {sh['name']} only has {sh['shares']:,.4f} shares "
              f"(worth ${sh['shares'] * nav:,.2f}). Cannot withdraw ${amount:,.2f}.")
        return

    conn.execute(
        "UPDATE fund_shareholders SET shares = shares - ? WHERE id = ?",
        (shares_to_burn, sh["id"])
    )
    conn.execute("""
        INSERT INTO fund_transactions
        (shareholder_id, type, usd_amount, shares_delta, nav_per_share, note)
        VALUES (?, 'withdrawal', ?, ?, ?, ?)
    """, (sh["id"], amount, -shares_to_burn, nav, args.note or ""))
    conn.commit()

    print(f"Withdrawal: {sh['name']} -${amount:,.2f} → -{shares_to_burn:,.4f} shares @ ${nav:,.4f}/share")
    print(f"New total: {sh['shares'] - shares_to_burn:,.4f} shares")


def cmd_history(args):
    """Show transaction history."""
    conn = get_db()

    query = """
        SELECT ft.*, fs.name
        FROM fund_transactions ft
        JOIN fund_shareholders fs ON ft.shareholder_id = fs.id
        WHERE fs.user_id = 1
    """
    params = []
    if args.person:
        query += " AND LOWER(fs.name) = LOWER(?)"
        params.append(args.person)
    query += " ORDER BY ft.created_at DESC"

    txs = conn.execute(query, params).fetchall()

    if not txs:
        print("No transactions found.")
        return

    print(f"{'Date':<20} {'Person':<12} {'Type':<12} {'Amount':>12} {'Shares':>12} {'NAV':>10} {'Note'}")
    print("─" * 95)
    for tx in txs:
        sign = "+" if tx["shares_delta"] > 0 else ""
        print(f"{tx['created_at']:<20} {tx['name']:<12} {tx['type']:<12} "
              f"${tx['usd_amount']:>10,.2f} {sign}{tx['shares_delta']:>10,.4f} "
              f"${tx['nav_per_share']:>8,.2f} {tx['note'] or ''}")


def cmd_add_person(args):
    """Add a new shareholder."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO fund_shareholders (user_id, name, shares) VALUES (1, ?, ?)",
            (args.name, args.shares or 0)
        )
        conn.commit()
        print(f"Added: {args.name} with {args.shares or 0} initial shares.")
    except sqlite3.IntegrityError:
        print(f"Error: '{args.name}' already exists.")


def cmd_set_personal(args):
    """Mark a wallet as personal (fully owned by one person)."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO fund_wallet_ownership (wallet_id, user_id, ownership_type, owner_name) VALUES (?, 1, 'personal', ?)",
        (args.wallet_id, args.person)
    )
    conn.commit()
    print(f"Wallet {args.wallet_id} → personal ({args.person})")


def cmd_rebalance(args):
    """Show current state vs share allocations — no changes made."""
    conn = get_db()
    cmd_status(args)


# ─── CLI ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Family Fund Management")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Initialize fund from current state")
    p.add_argument("--force", action="store_true", help="Reinitialize (clears existing data)")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="Show all shareholders & values")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("nav", help="Show current NAV & share price")
    p.set_defaults(func=cmd_nav)

    p = sub.add_parser("deposit", help="Record a deposit")
    p.add_argument("person", help="Shareholder name")
    p.add_argument("amount", type=float, help="USD amount")
    p.add_argument("--note", help="Optional note")
    p.set_defaults(func=cmd_deposit)

    p = sub.add_parser("withdraw", help="Record a withdrawal")
    p.add_argument("person", help="Shareholder name")
    p.add_argument("amount", type=float, help="USD amount")
    p.add_argument("--note", help="Optional note")
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("history", help="Show transaction history")
    p.add_argument("person", nargs="?", help="Filter by person")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("add-person", help="Add a new shareholder")
    p.add_argument("name", help="Person's name")
    p.add_argument("shares", type=float, nargs="?", default=0, help="Initial shares")
    p.set_defaults(func=cmd_add_person)

    p = sub.add_parser("set-personal", help="Mark wallet as personal")
    p.add_argument("wallet_id", type=int)
    p.add_argument("person", help="Owner name")
    p.set_defaults(func=cmd_set_personal)

    p = sub.add_parser("rebalance", help="Show current allocations")
    p.set_defaults(func=cmd_rebalance)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
