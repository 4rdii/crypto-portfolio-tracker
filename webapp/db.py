"""SQLite persistence layer for the portfolio tracker."""
import sqlite3
from pathlib import Path
from contextlib import contextmanager

from applog import log

DB_PATH = Path(__file__).resolve().parent / "portfolio.db"

# Hide positions worth less than this in aggregated views.
DUST_USD = 1.0

SCHEMA = """
-- Multi-tenant: every user is identified by the first wallet they sign in with.
-- primary_address is the canonical identifier; chain_type tells us which signing
-- protocol we verify against (EVM SIWE, Solana SIWS, etc.).
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    primary_address  TEXT NOT NULL UNIQUE,
    chain_type       TEXT NOT NULL,       -- 'evm' | 'solana' | 'bitcoin'
    display_name     TEXT,                -- optional ENS / SNS / manual label
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wallets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    address         TEXT NOT NULL,
    label           TEXT NOT NULL,
    chain           TEXT NOT NULL,
    scan_enabled    INTEGER NOT NULL DEFAULT 1,
    verified        INTEGER NOT NULL DEFAULT 0,   -- 1 = proved ownership via signature
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_scanned_at TIMESTAMP,
    UNIQUE(user_id, address)
);

CREATE TABLE IF NOT EXISTS holdings (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    wallet_id         INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    chain             TEXT NOT NULL,
    symbol            TEXT NOT NULL,        -- display symbol (aTokens show underlying, e.g. "WBTC")
    name              TEXT,                 -- raw token name from the scanner
    contract          TEXT,
    balance           REAL NOT NULL,
    usd_price         REAL NOT NULL,
    usd_value         REAL NOT NULL,
    price_24h_change  REAL DEFAULT 0,
    protocol          TEXT,                 -- 'aave' for deposited aTokens, NULL for plain wallet holdings
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(wallet_id, chain, contract)
);

CREATE TABLE IF NOT EXISTS transactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts         TIMESTAMP NOT NULL,
    tx_type    TEXT NOT NULL,            -- buy / sell / deposit / withdraw / swap
    symbol     TEXT NOT NULL,
    amount     REAL NOT NULL,
    price_usd  REAL NOT NULL DEFAULT 0,
    total_usd  REAL NOT NULL DEFAULT 0,
    wallet_id  INTEGER REFERENCES wallets(id) ON DELETE SET NULL,
    notes      TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS snapshots (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ts        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_usd REAL NOT NULL
);

-- Single-use nonces for SIWE/SIWS sign-in. Nonces expire 10 minutes after issue
-- and are consumed on successful verification so a signature can't be replayed.
CREATE TABLE IF NOT EXISTS auth_nonces (
    nonce        TEXT PRIMARY KEY,
    address      TEXT NOT NULL,
    chain_type   TEXT NOT NULL,
    issued_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    consumed     INTEGER NOT NULL DEFAULT 0
);

-- Credit ledger. Every user has a single dollar-denominated balance row
-- that's mutated in lockstep with credit_transactions rows (append-only
-- audit trail). Top-ups are detected by the treasury watcher polling the
-- treasury wallet address on every supported chain. Charges are deducted
-- synchronously whenever a paid operation runs (history import today,
-- more later). Refunds flow through the same ledger with a positive
-- delta and kind='refund' so net balance always equals SUM(delta).
CREATE TABLE IF NOT EXISTS user_credits (
    user_id      INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    balance_usd  REAL NOT NULL DEFAULT 0,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS credit_transactions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta_usd      REAL NOT NULL,                 -- + for topup/refund, - for charge
    kind           TEXT NOT NULL,                 -- 'topup' | 'charge' | 'refund' | 'grant'
    chain          TEXT,                          -- chain the deposit landed on (NULL for charges)
    tx_hash        TEXT,                          -- on-chain tx hash (NULL for charges)
    from_address   TEXT,                          -- sender (the wallet we credited against)
    token_symbol   TEXT,                          -- USDC/USDT/ETH/etc — NULL for charges
    token_amount   REAL,                          -- raw token units received
    notes          TEXT,                          -- human-readable description
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Idempotency guard for treasury-watcher deposits: each (chain, tx_hash)
-- combo can only credit once even if the watcher replays the same range.
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_tx_dedupe
    ON credit_transactions(chain, tx_hash) WHERE kind = 'topup';

-- Same idempotency guard for pay-per-import payments: each on-chain tx
-- hash can only be spent on a single import. Prevents a user from
-- replaying the same tx hash across multiple import calls.
CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_tx_import_dedupe
    ON credit_transactions(chain, tx_hash) WHERE kind = 'import_payment';

CREATE INDEX IF NOT EXISTS idx_credit_tx_user ON credit_transactions(user_id);

-- Unclaimed deposits: transfers to the treasury whose sender doesn't match
-- any linked wallet. We still log them so they're visible for manual
-- resolution ("I sent from cold wallet X, please credit me"). Moved to
-- user_credits when resolved.
CREATE TABLE IF NOT EXISTS unclaimed_deposits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chain          TEXT NOT NULL,
    tx_hash        TEXT NOT NULL,
    from_address   TEXT NOT NULL,
    token_symbol   TEXT NOT NULL,
    token_amount   REAL NOT NULL,
    usd_value      REAL NOT NULL,
    detected_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_user  INTEGER REFERENCES users(id),
    UNIQUE(chain, tx_hash)
);

-- History-import run log: every time a user runs an import against a
-- wallet we append a row here, regardless of success/failure. Powers the
-- 30-day-per-wallet import cap and gives ops visibility into churn.
CREATE TABLE IF NOT EXISTS wallet_imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    wallet_id   INTEGER NOT NULL REFERENCES wallets(id) ON DELETE CASCADE,
    ran_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    charged_usd REAL NOT NULL DEFAULT 0,
    row_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_wallet_imports_wallet_time
    ON wallet_imports(wallet_id, ran_at DESC);

CREATE INDEX IF NOT EXISTS idx_holdings_symbol ON holdings(symbol);
CREATE INDEX IF NOT EXISTS idx_holdings_user ON holdings(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_symbol ON transactions(symbol);
CREATE INDEX IF NOT EXISTS idx_transactions_ts ON transactions(ts);
CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_wallets_user ON wallets(user_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_user ON snapshots(user_id);
CREATE INDEX IF NOT EXISTS idx_nonces_address ON auth_nonces(address);

-- Spec-ID cost basis overrides. Only used when the user picks the SPEC
-- method and manually assigns which lot(s) to consume for a given sale.
-- Each row represents "for user U's sell tx S, take `amount` units from
-- lot L" (lot_id is the stable id compute_disposals assigns per symbol
-- in chronological order of acquisitions). If no overrides exist for a
-- sell tx the engine falls back to FIFO for that sale.
CREATE TABLE IF NOT EXISTS tax_lot_overrides (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    sell_tx_id   INTEGER NOT NULL,   -- transactions.id for the sale
    symbol       TEXT NOT NULL,      -- canonical symbol (BTC not WBTC)
    lot_id       INTEGER NOT NULL,   -- lot id within that symbol's ledger
    amount       REAL NOT NULL,      -- units to consume from this lot
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, sell_tx_id, lot_id)
);
CREATE INDEX IF NOT EXISTS idx_tax_overrides_user_tx
    ON tax_lot_overrides(user_id, sell_tx_id);

-- Tax report unlock. One-time $9.99 payment unlocks unlimited
-- tax reports forever (all years, jurisdictions, methods) for a single
-- user. The payment itself flows through the normal wallet-pay path;
-- this table only records the fact that the user has unlocked the
-- feature so subsequent report requests don't re-check billing.
CREATE TABLE IF NOT EXISTS tax_unlocks (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    unlocked_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    amount_paid_usd REAL NOT NULL,
    tx_hash         TEXT,    -- on-chain reference, NULL for admin grants
    chain           TEXT,    -- chain the payment landed on
    notes           TEXT
);

-- Portfolio rebalancing strategies. Both system pre-built strategies
-- (is_preset=1, user_id=NULL) and user custom strategies (is_preset=0).
-- Allocations is a JSON object mapping symbol → target percentage.
CREATE TABLE IF NOT EXISTS strategies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,  -- NULL for presets
    name        TEXT NOT NULL,
    allocations TEXT NOT NULL,           -- JSON: {"BTC": 40, "ETH": 30, "SOL": 20, "USDC": 10}
    is_preset   INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_strategies_user ON strategies(user_id);

-- Rebalancing execution history. Records every time a user executes
-- a rebalance, whether successful or failed. Swaps is a JSON array
-- of swap objects with from/to/amount/status.
CREATE TABLE IF NOT EXISTS rebalance_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    strategy_id  INTEGER REFERENCES strategies(id) ON DELETE SET NULL,
    swaps        TEXT NOT NULL,          -- JSON: [{"from":"USDC","to":"BTC","amount":100,"status":"success"}]
    tx_hashes    TEXT,                   -- JSON array of on-chain tx hashes
    status       TEXT NOT NULL,          -- 'pending' | 'success' | 'partial' | 'failed'
    error        TEXT,                   -- error message if failed
    total_cost_usd REAL,                 -- total gas + fees
    executed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_rebalance_user ON rebalance_history(user_id);
CREATE INDEX IF NOT EXISTS idx_rebalance_strategy ON rebalance_history(strategy_id);
"""


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        # WAL mode = multi-reader + single-writer concurrency; the default
        # rollback journal mode serializes everything and can starve the
        # treasury watcher thread while a long import holds the write lock.
        # Also set busy_timeout so the rare blocked reader retries instead
        # of raising OperationalError.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA foreign_keys = ON")
        # Lightweight idempotent migrations for columns added after initial release.
        # SQLite's CREATE TABLE IF NOT EXISTS won't add new columns to an existing
        # table, so any new column must be ALTER-added here defensively.
        _add_column_if_missing(conn, "holdings", "protocol", "TEXT")
        _add_column_if_missing(conn, "wallets", "enabled_chains", "TEXT")
        _add_column_if_missing(conn, "users", "is_free_tier", "INTEGER NOT NULL DEFAULT 0")
        conn.commit()


def _add_column_if_missing(conn, table: str, column: str, decl: str):
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row):
    return {k: row[k] for k in row.keys()} if row else None


# ---------- Users ----------

def get_user_by_address(address: str):
    with get_conn() as c:
        r = c.execute(
            "SELECT * FROM users WHERE LOWER(primary_address) = LOWER(?)", (address,)
        ).fetchone()
        return row_to_dict(r)


def get_user(user_id: int):
    with get_conn() as c:
        r = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return row_to_dict(r)


def create_user(address: str, chain_type: str, display_name: str | None = None) -> int:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO users (primary_address, chain_type, display_name, last_login_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (address, chain_type, display_name),
        )
        return cur.lastrowid


def touch_user_login(user_id: int):
    with get_conn() as c:
        c.execute(
            "UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?",
            (user_id,),
        )


def is_free_tier(user_id: int) -> bool:
    """Return True if this user is on the admin-granted free tier."""
    with get_conn() as c:
        r = c.execute(
            "SELECT is_free_tier FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return bool(r and r["is_free_tier"])


def set_free_tier(user_id: int, enabled: bool = True):
    """Grant or revoke the free tier for a user."""
    with get_conn() as c:
        c.execute(
            "UPDATE users SET is_free_tier = ? WHERE id = ?",
            (1 if enabled else 0, user_id),
        )


# ---------- Wallets ----------

def list_wallets(user_id: int):
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM wallets WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def add_wallet(user_id: int, address: str, label: str, chain: str, verified: int = 0):
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO wallets (user_id, address, label, chain, verified) VALUES (?, ?, ?, ?, ?)",
            (user_id, address, label, chain, verified),
        )
        return cur.lastrowid


def delete_wallet(user_id: int, wallet_id: int):
    """Cascade-delete wallet + holdings + transactions associated with it.
    Scoped to user_id to prevent cross-tenant deletes."""
    with get_conn() as c:
        c.execute(
            "DELETE FROM transactions WHERE wallet_id = ? AND user_id = ?",
            (wallet_id, user_id),
        )
        c.execute(
            "DELETE FROM holdings WHERE wallet_id = ? AND user_id = ?",
            (wallet_id, user_id),
        )
        c.execute(
            "DELETE FROM wallets WHERE id = ? AND user_id = ?",
            (wallet_id, user_id),
        )


def toggle_wallet_scan(user_id: int, wallet_id: int, enabled: bool):
    with get_conn() as c:
        c.execute(
            "UPDATE wallets SET scan_enabled = ? WHERE id = ? AND user_id = ?",
            (1 if enabled else 0, wallet_id, user_id),
        )


def mark_wallet_scanned(user_id: int, wallet_id: int):
    with get_conn() as c:
        c.execute(
            "UPDATE wallets SET last_scanned_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
            (wallet_id, user_id),
        )


def get_wallet(user_id: int, wallet_id: int):
    with get_conn() as c:
        r = c.execute(
            "SELECT * FROM wallets WHERE id = ? AND user_id = ?",
            (wallet_id, user_id),
        ).fetchone()
        return row_to_dict(r)


def update_wallet_chains(user_id: int, wallet_id: int, chains: list[str]):
    """Update the enabled EVM chains for a wallet (comma-separated string)."""
    with get_conn() as c:
        c.execute(
            "UPDATE wallets SET enabled_chains = ? WHERE id = ? AND user_id = ?",
            (",".join(chains), wallet_id, user_id),
        )


# ---------- Holdings ----------

def replace_wallet_holdings(user_id: int, wallet_id: int, rows: list[dict]):
    """Atomically replace all holdings for a wallet with the latest scan."""
    with get_conn() as c:
        c.execute(
            "DELETE FROM holdings WHERE wallet_id = ? AND user_id = ?",
            (wallet_id, user_id),
        )
        for r in rows:
            c.execute(
                """INSERT INTO holdings
                    (user_id, wallet_id, chain, symbol, name, contract, balance,
                     usd_price, usd_value, price_24h_change, protocol)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    wallet_id,
                    r.get("chain"),
                    r.get("token_symbol") or r.get("symbol"),
                    r.get("token_name") or r.get("name"),
                    r.get("contract") or "native",
                    float(r.get("balance", 0)),
                    float(r.get("usd_price", 0)),
                    float(r.get("usd_value", 0)),
                    float(r.get("price_24h_change") or 0),
                    r.get("protocol"),
                ),
            )


def list_holdings_raw(user_id: int, dust: float = DUST_USD):
    """Per-wallet raw holdings rows (joined with wallet label). Hides dust."""
    with get_conn() as c:
        rows = c.execute(
            """SELECT h.*, w.label AS wallet_label, w.address AS wallet_address
               FROM holdings h
               LEFT JOIN wallets w ON w.id = h.wallet_id
               WHERE h.user_id = ? AND h.usd_value >= ?
               ORDER BY h.usd_value DESC""",
            (user_id, dust),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def list_holdings_by_symbol(user_id: int, dust: float = DUST_USD,
                            wallet_ids: list[int] | None = None):
    """Aggregate current holdings by symbol (across all wallets). Hides dust.

    aTokens are folded into their underlying (they report `symbol` as the
    underlying already, e.g. "WBTC" for aEthWBTC), so an Aave-deposited
    position aggregates with any wallet-held WBTC into a single row.
    `protocols` lists all protocols contributing to the row (NULL entries
    become "wallet"). Each row also embeds a `wallet_breakdown` list of
    `{wallet_id, label, balance, value, protocol, chain}` rows so the UI can
    render per-wallet badges and do client-side display filtering.

    When `wallet_ids` is supplied (not None), rows are filtered to only those
    wallets — used by the holdings page's wallet-picker filter.
    """
    params_filter = [user_id]
    wallet_clause = ""
    if wallet_ids is not None:
        if not wallet_ids:
            return []
        placeholders = ",".join("?" * len(wallet_ids))
        wallet_clause = f"AND h.wallet_id IN ({placeholders})"
        params_filter.extend(wallet_ids)

    with get_conn() as c:
        # Pull every holding row first (with wallet metadata attached) —
        # cheap since "holdings" tops out at a few hundred rows per user —
        # then aggregate in Python so we can also build the breakdown.
        raw = c.execute(
            f"""SELECT h.*, w.label AS wallet_label, w.address AS wallet_address
                FROM holdings h
                LEFT JOIN wallets w ON w.id = h.wallet_id
                WHERE h.user_id = ? {wallet_clause}""",
            params_filter,
        ).fetchall()

    # Group by symbol (case-insensitive to collapse e.g. "Cake" vs "CAKE").
    groups: dict[str, dict] = {}
    for r in raw:
        sym = (r["symbol"] or "").upper()
        if not sym:
            continue
        bal = float(r["balance"] or 0)
        val = float(r["usd_value"] or 0)
        price = float(r["usd_price"] or 0)
        g = groups.get(sym)
        if g is None:
            g = {
                "symbol": sym,
                "name": r["name"] or sym,
                "balance": 0.0,
                "usd_value": 0.0,
                "usd_price": price,
                "price_24h_change": float(r["price_24h_change"] or 0),
                "_chains": set(),
                "_protocols": set(),
                "wallet_breakdown": [],
            }
            groups[sym] = g
        g["balance"] += bal
        g["usd_value"] += val
        if price > 0:
            g["usd_price"] = price  # latest non-zero price wins
        if r["chain"]:
            g["_chains"].add(r["chain"])
        g["_protocols"].add(r["protocol"] or "wallet")
        g["wallet_breakdown"].append({
            "wallet_id": r["wallet_id"],
            "label": r["wallet_label"] or "Wallet",
            "address": r["wallet_address"] or "",
            "chain": r["chain"],
            "balance": bal,
            "value": val,
            "protocol": r["protocol"] or "wallet",
        })

    out = []
    for sym, g in groups.items():
        if g["usd_value"] < dust or g["balance"] <= 0:
            continue
        g["chains"] = ",".join(sorted(g.pop("_chains")))
        g["protocols"] = ",".join(sorted(g.pop("_protocols")))
        # Largest contributor first so the first badge the user sees is the
        # most meaningful one.
        g["wallet_breakdown"].sort(key=lambda w: -w["value"])
        out.append(g)
    out.sort(key=lambda g: -g["usd_value"])
    return out


# ---------- Transactions ----------

def list_transactions(user_id: int, symbol: str = None, limit: int = 500,
                      wallet_ids: list[int] | None = None):
    """List transactions for a user, optionally filtered by symbol and/or wallet ids.

    `wallet_ids` is used by the holdings page's wallet filter: when the user
    toggles off a wallet we want to recompute cost basis / realized P&L as if
    that wallet never existed — so we slice the underlying transaction set,
    not just the holdings. An empty list means "no wallets selected"; pass
    None (the default) to include everything.
    """
    clauses = ["t.user_id = ?"]
    params: list = [user_id]
    if symbol:
        clauses.append("t.symbol = ?")
        params.append(symbol)
    if wallet_ids is not None:
        if not wallet_ids:
            return []  # explicit empty filter → no transactions
        placeholders = ",".join("?" * len(wallet_ids))
        clauses.append(f"t.wallet_id IN ({placeholders})")
        params.extend(wallet_ids)
    params.append(limit)
    sql = f"""SELECT t.*, w.label AS wallet_label
              FROM transactions t
              LEFT JOIN wallets w ON w.id = t.wallet_id
              WHERE {' AND '.join(clauses)}
              ORDER BY t.ts DESC LIMIT ?"""
    with get_conn() as c:
        rows = c.execute(sql, params).fetchall()
        return [row_to_dict(r) for r in rows]


def add_transaction(
    user_id: int, ts, tx_type, symbol, amount, price_usd, wallet_id=None, notes=None
):
    total_usd = float(amount) * float(price_usd or 0)
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO transactions
                (user_id, ts, tx_type, symbol, amount, price_usd, total_usd, wallet_id, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, ts, tx_type, symbol.upper(), amount, price_usd or 0,
             total_usd, wallet_id, notes),
        )
        return cur.lastrowid


def delete_transaction(user_id: int, tx_id: int):
    with get_conn() as c:
        c.execute(
            "DELETE FROM transactions WHERE id = ? AND user_id = ?",
            (tx_id, user_id),
        )


def update_transaction_price(user_id: int, tx_id: int, price_usd: float) -> bool:
    """Overwrite a tx's price_usd and recalc total_usd.

    Used by the reprice-all endpoint to backfill historical prices from
    Binance 5m candles after the user imports. Scoped by user_id so you
    can't overwrite another tenant's rows. Returns True if a row was
    updated.
    """
    with get_conn() as c:
        cur = c.execute(
            """UPDATE transactions
               SET price_usd = ?,
                   total_usd = amount * ?
               WHERE id = ? AND user_id = ?""",
            (price_usd, price_usd, tx_id, user_id),
        )
        return cur.rowcount > 0


def list_all_transactions_for_tax(user_id: int) -> list[dict]:
    """Fetch every transaction a user has, oldest first, no limit.

    The tax engine needs the FULL history to build lots correctly — a
    2019 buy can still match against a 2024 sell. `list_transactions`
    caps at 500 rows by default which would silently corrupt tax math
    for any active trader, so we provide a dedicated no-limit helper.
    Ordered ascending so the cost-basis walker doesn't have to sort.
    """
    with get_conn() as c:
        rows = c.execute(
            """SELECT id, user_id, ts, tx_type, symbol, amount,
                      price_usd, total_usd, wallet_id
               FROM transactions
               WHERE user_id = ?
               ORDER BY ts ASC, id ASC""",
            (user_id,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


# ---------- Tax unlocks ----------

def has_tax_unlock(user_id: int) -> bool:
    """Is this user entitled to generate tax reports?"""
    with get_conn() as c:
        r = c.execute(
            "SELECT 1 FROM tax_unlocks WHERE user_id = ?", (user_id,)
        ).fetchone()
        return r is not None


def get_tax_unlock(user_id: int) -> dict | None:
    with get_conn() as c:
        r = c.execute(
            "SELECT * FROM tax_unlocks WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row_to_dict(r)


def list_tax_overrides(user_id: int) -> list[dict]:
    """Return every spec-ID lot override for a user, keyed by sell_tx_id.

    The tax API converts this to the dict-of-dicts format the engine
    expects (tx_id → {lot_id: amount}).
    """
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM tax_lot_overrides WHERE user_id = ? ORDER BY sell_tx_id",
            (user_id,),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def set_tax_override(user_id: int, sell_tx_id: int, symbol: str,
                     lot_id: int, amount: float) -> None:
    """Upsert a single lot-selection override. Amount <= 0 deletes the row."""
    with get_conn() as c:
        if amount <= 0:
            c.execute(
                "DELETE FROM tax_lot_overrides WHERE user_id = ? AND sell_tx_id = ? AND lot_id = ?",
                (user_id, sell_tx_id, lot_id),
            )
            return
        c.execute(
            """INSERT INTO tax_lot_overrides (user_id, sell_tx_id, symbol, lot_id, amount)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id, sell_tx_id, lot_id)
               DO UPDATE SET amount = excluded.amount, updated_at = CURRENT_TIMESTAMP""",
            (user_id, sell_tx_id, symbol.upper(), lot_id, amount),
        )


def clear_tax_overrides_for_tx(user_id: int, sell_tx_id: int) -> None:
    """Delete every override for a specific sell tx (used by 'reset' button)."""
    with get_conn() as c:
        c.execute(
            "DELETE FROM tax_lot_overrides WHERE user_id = ? AND sell_tx_id = ?",
            (user_id, sell_tx_id),
        )


def get_tax_unlock_by_tx(chain: str, tx_hash: str) -> dict | None:
    """Replay guard: has this on-chain tx already been spent on a tax unlock?

    Used by the payment verifier to prevent a user from reusing the same
    USDC transfer for multiple unlocks (own account or another user).
    """
    if not tx_hash:
        return None
    with get_conn() as c:
        r = c.execute(
            "SELECT * FROM tax_unlocks WHERE LOWER(tx_hash) = LOWER(?) AND chain = ?",
            (tx_hash, chain),
        ).fetchone()
        return row_to_dict(r)


def grant_tax_unlock(user_id: int, amount_paid_usd: float,
                     tx_hash: str | None = None, chain: str | None = None,
                     notes: str | None = None) -> None:
    """Insert-or-ignore a tax unlock row for this user.

    Idempotent: re-granting is a no-op so we can safely re-run the
    payment confirmation path on retries.
    """
    with get_conn() as c:
        c.execute(
            """INSERT OR IGNORE INTO tax_unlocks
               (user_id, amount_paid_usd, tx_hash, chain, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (user_id, amount_paid_usd, tx_hash, chain, notes),
        )


# ---------- Snapshots ----------

def record_snapshot(user_id: int, total_usd: float):
    with get_conn() as c:
        c.execute(
            "INSERT INTO snapshots (user_id, total_usd) VALUES (?, ?)",
            (user_id, total_usd),
        )


def list_snapshots(user_id: int, limit: int = 90):
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM snapshots WHERE user_id = ? ORDER BY ts DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return list(reversed([row_to_dict(r) for r in rows]))


# ---------- Auth nonces ----------

def store_nonce(nonce: str, address: str, chain_type: str):
    with get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO auth_nonces (nonce, address, chain_type) VALUES (?, ?, ?)",
            (nonce, address.lower(), chain_type),
        )


def consume_nonce(nonce: str, address: str) -> bool:
    """Mark a nonce as used. Returns True if it was valid + unconsumed + fresh."""
    with get_conn() as c:
        r = c.execute(
            """SELECT nonce FROM auth_nonces
               WHERE nonce = ? AND LOWER(address) = LOWER(?)
                 AND consumed = 0
                 AND issued_at > datetime('now', '-10 minutes')""",
            (nonce, address),
        ).fetchone()
        if not r:
            return False
        c.execute("UPDATE auth_nonces SET consumed = 1 WHERE nonce = ?", (nonce,))
        return True


# ---------- Credits ----------

def get_credit_balance(user_id: int) -> float:
    """Return the user's current credit balance in USD. Zero if no row yet."""
    with get_conn() as c:
        r = c.execute(
            "SELECT balance_usd FROM user_credits WHERE user_id = ?", (user_id,)
        ).fetchone()
        return float(r["balance_usd"]) if r else 0.0


def _apply_credit_delta(c, user_id: int, delta: float):
    """Internal: mutate user_credits row atomically. Caller holds the conn."""
    row = c.execute(
        "SELECT balance_usd FROM user_credits WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        c.execute(
            "INSERT INTO user_credits (user_id, balance_usd) VALUES (?, ?)",
            (user_id, max(0.0, delta)),
        )
    else:
        c.execute(
            "UPDATE user_credits SET balance_usd = balance_usd + ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (delta, user_id),
        )


def credit_topup(
    user_id: int,
    delta_usd: float,
    chain: str,
    tx_hash: str,
    from_address: str,
    token_symbol: str,
    token_amount: float,
    notes: str = "",
) -> bool:
    """Record an incoming treasury deposit as a credit top-up.

    Idempotent on (chain, tx_hash): the unique index guarantees a replayed
    watcher range can't double-credit. Returns True if we actually credited,
    False if this tx was already on file.
    """
    with get_conn() as c:
        try:
            c.execute(
                """INSERT INTO credit_transactions
                    (user_id, delta_usd, kind, chain, tx_hash, from_address,
                     token_symbol, token_amount, notes)
                   VALUES (?, ?, 'topup', ?, ?, ?, ?, ?, ?)""",
                (user_id, delta_usd, chain, tx_hash, from_address.lower(),
                 token_symbol, token_amount, notes),
            )
        except sqlite3.IntegrityError:
            # (chain, tx_hash) already credited — idempotent no-op
            return False
        _apply_credit_delta(c, user_id, delta_usd)
        log.info("credit.topup user=%s amount=%.4f chain=%s tx=%s token=%s",
                 user_id, delta_usd, chain, tx_hash[:12], token_symbol)
        return True


def credit_charge(user_id: int, amount_usd: float, notes: str = "") -> bool:
    """Deduct a charge from the user's balance. Returns False if insufficient.

    Atomic via a conditional UPDATE: the `balance_usd >= ?` predicate is
    evaluated inside the single statement, so two concurrent imports can't
    both read a $5 balance and both deduct $4 (the second UPDATE sees the
    already-debited row and its WHERE clause fails → rowcount=0 → False).
    Previously this was a read-then-write in separate statements which was
    race-prone even inside a BEGIN DEFERRED transaction.
    """
    amount_usd = float(amount_usd)
    if amount_usd <= 0:
        return True
    # Tolerance to sidestep float-comparison edge cases ($4.99999 vs $5.0).
    eps = 1e-9
    with get_conn() as c:
        # Ensure a row exists to UPDATE against — first-ever charge for a
        # brand-new user with no topups gets handled consistently.
        c.execute(
            "INSERT OR IGNORE INTO user_credits (user_id, balance_usd) VALUES (?, 0)",
            (user_id,),
        )
        cur = c.execute(
            "UPDATE user_credits SET balance_usd = balance_usd - ?, "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE user_id = ? AND balance_usd + ? >= ?",
            (amount_usd, user_id, eps, amount_usd),
        )
        if cur.rowcount == 0:
            log.warning("credit.charge_insufficient user=%s amount=%.4f notes=%s",
                        user_id, amount_usd, notes[:80])
            return False
        c.execute(
            """INSERT INTO credit_transactions
                (user_id, delta_usd, kind, notes)
               VALUES (?, ?, 'charge', ?)""",
            (user_id, -amount_usd, notes),
        )
        log.info("credit.charge user=%s amount=%.4f notes=%s",
                 user_id, amount_usd, notes[:80])
        return True


def credit_refund(user_id: int, amount_usd: float, notes: str = ""):
    """Issue a refund (positive delta) back to the user's balance."""
    if amount_usd <= 0:
        return
    with get_conn() as c:
        c.execute(
            """INSERT INTO credit_transactions
                (user_id, delta_usd, kind, notes)
               VALUES (?, ?, 'refund', ?)""",
            (user_id, amount_usd, notes),
        )
        _apply_credit_delta(c, user_id, amount_usd)
    log.info("credit.refund user=%s amount=%.4f notes=%s",
             user_id, amount_usd, notes[:80])


def list_credit_transactions(user_id: int, limit: int = 100):
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM credit_transactions WHERE user_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [row_to_dict(r) for r in rows]


def record_import_payment(
    user_id: int,
    cost_usd: float,
    chain: str,
    tx_hash: str,
    from_address: str,
    token_symbol: str,
    token_amount: float,
    notes: str = "",
) -> bool:
    """Log a pay-per-import payment to the credit_transactions ledger.

    Pay-per-import does NOT touch user_credits — there is no balance to
    maintain. The ledger entry is purely for audit / history display, and
    the (chain, tx_hash) unique index on kind='import_payment' guarantees
    a given on-chain tx can only pay for one import (no replay).

    Returns True on success, False if the tx_hash was already spent.
    """
    with get_conn() as c:
        try:
            c.execute(
                """INSERT INTO credit_transactions
                    (user_id, delta_usd, kind, chain, tx_hash, from_address,
                     token_symbol, token_amount, notes)
                   VALUES (?, ?, 'import_payment', ?, ?, ?, ?, ?, ?)""",
                (user_id, -float(cost_usd), chain, tx_hash,
                 (from_address or "").lower(), token_symbol,
                 token_amount, notes),
            )
        except sqlite3.IntegrityError:
            return False
        log.info("credit.import_payment user=%s cost=%.4f chain=%s tx=%s",
                 user_id, cost_usd, chain, tx_hash[:12])
        return True


def find_import_payment(chain: str, tx_hash: str):
    """Look up an existing pay-per-import row by (chain, tx_hash).

    Used by the pay-and-import endpoint to reject replay attempts where
    the same on-chain payment is submitted twice.
    """
    if not chain or not tx_hash:
        return None
    with get_conn() as c:
        r = c.execute(
            "SELECT * FROM credit_transactions "
            "WHERE kind = 'import_payment' AND chain = ? "
            "AND lower(tx_hash) = lower(?) LIMIT 1",
            (chain, tx_hash),
        ).fetchone()
        return row_to_dict(r) if r else None


def find_credit_topup(chain: str, tx_hash: str):
    """Look up an existing top-up row by (chain, tx_hash).

    Used by the in-app wallet-payment verify endpoint to short-circuit the
    Moralis round-trip when a tx hash has already been credited (either by
    a previous call or by the background treasury watcher). The unique
    partial index on (chain, tx_hash) WHERE kind='topup' guarantees at
    most one hit.
    """
    if not chain or not tx_hash:
        return None
    with get_conn() as c:
        r = c.execute(
            "SELECT * FROM credit_transactions "
            "WHERE kind = 'topup' AND chain = ? AND lower(tx_hash) = lower(?) "
            "LIMIT 1",
            (chain, tx_hash),
        ).fetchone()
        return row_to_dict(r) if r else None


def record_unclaimed_deposit(
    chain: str, tx_hash: str, from_address: str,
    token_symbol: str, token_amount: float, usd_value: float,
):
    """Log a treasury deposit we couldn't match to any linked wallet."""
    with get_conn() as c:
        try:
            c.execute(
                """INSERT INTO unclaimed_deposits
                    (chain, tx_hash, from_address, token_symbol, token_amount, usd_value)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (chain, tx_hash, from_address.lower(), token_symbol,
                 token_amount, usd_value),
            )
        except sqlite3.IntegrityError:
            pass  # already logged


def count_user_transactions(user_id: int) -> int:
    """Fast row-count for the lifetime transaction cap."""
    with get_conn() as c:
        r = c.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return int(r["n"]) if r else 0


def record_wallet_import(user_id: int, wallet_id: int,
                         charged_usd: float, row_count: int):
    """Append an import-run row for the 30-day cap."""
    with get_conn() as c:
        c.execute(
            """INSERT INTO wallet_imports
                (user_id, wallet_id, charged_usd, row_count)
               VALUES (?, ?, ?, ?)""",
            (user_id, wallet_id, charged_usd, row_count),
        )


def count_recent_wallet_imports(user_id: int, wallet_id: int, days: int = 30) -> int:
    """Count imports of this wallet in the rolling N-day window."""
    with get_conn() as c:
        r = c.execute(
            f"""SELECT COUNT(*) AS n FROM wallet_imports
                WHERE user_id = ? AND wallet_id = ?
                  AND ran_at > datetime('now', '-{int(days)} days')""",
            (user_id, wallet_id),
        ).fetchone()
        return int(r["n"]) if r else 0


def find_user_by_wallet_address(address: str) -> int | None:
    """Reverse-lookup: which user owns this wallet address? Returns user_id or None."""
    with get_conn() as c:
        r = c.execute(
            "SELECT user_id FROM wallets WHERE LOWER(address) = LOWER(?) LIMIT 1",
            (address,),
        ).fetchone()
        return int(r["user_id"]) if r else None


# ────────────────────────────────────────────────────────────────────
# Portfolio Rebalancing Strategies
# ────────────────────────────────────────────────────────────────────

def get_preset_strategies():
    """Return all system-defined preset strategies."""
    with get_conn() as c:
        return c.execute(
            """SELECT id, name, allocations, created_at
               FROM strategies
               WHERE is_preset = 1
               ORDER BY id""",
        ).fetchall()


def get_user_strategies(user_id: int):
    """Return all custom strategies for this user."""
    with get_conn() as c:
        return c.execute(
            """SELECT id, name, allocations, created_at, updated_at
               FROM strategies
               WHERE user_id = ? AND is_preset = 0
               ORDER BY updated_at DESC""",
            (user_id,),
        ).fetchall()


def get_strategy(strategy_id: int, user_id: int | None = None):
    """Get a single strategy. If user_id provided, verify ownership (unless preset)."""
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not row:
            return None
        # If user_id provided and strategy is not a preset, verify ownership
        if user_id is not None and row["is_preset"] == 0 and row["user_id"] != user_id:
            return None
        return row


def create_strategy(user_id: int, name: str, allocations: str):
    """Create a new custom strategy for this user. Returns strategy_id."""
    with get_conn() as c:
        cursor = c.execute(
            """INSERT INTO strategies (user_id, name, allocations, is_preset)
               VALUES (?, ?, ?, 0)""",
            (user_id, name, allocations),
        )
        return cursor.lastrowid


def update_strategy(strategy_id: int, user_id: int, name: str, allocations: str):
    """Update an existing user strategy. Returns True if updated, False if not found/owned."""
    with get_conn() as c:
        cursor = c.execute(
            """UPDATE strategies
               SET name = ?, allocations = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND user_id = ? AND is_preset = 0""",
            (name, allocations, strategy_id, user_id),
        )
        return cursor.rowcount > 0


def delete_strategy(strategy_id: int, user_id: int):
    """Delete a user strategy. Returns True if deleted, False if not found/owned."""
    with get_conn() as c:
        cursor = c.execute(
            """DELETE FROM strategies
               WHERE id = ? AND user_id = ? AND is_preset = 0""",
            (strategy_id, user_id),
        )
        return cursor.rowcount > 0


def record_rebalance(user_id: int, strategy_id: int | None, swaps: str,
                     status: str, tx_hashes: str | None = None,
                     error: str | None = None, total_cost_usd: float | None = None):
    """Record a rebalance execution attempt. Returns rebalance_id."""
    with get_conn() as c:
        cursor = c.execute(
            """INSERT INTO rebalance_history
                (user_id, strategy_id, swaps, tx_hashes, status, error, total_cost_usd)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, strategy_id, swaps, tx_hashes, status, error, total_cost_usd),
        )
        return cursor.lastrowid


def get_rebalance_history(user_id: int, limit: int = 50):
    """Get recent rebalance execution history for this user."""
    with get_conn() as c:
        return c.execute(
            """SELECT r.*, s.name AS strategy_name
               FROM rebalance_history r
               LEFT JOIN strategies s ON r.strategy_id = s.id
               WHERE r.user_id = ?
               ORDER BY r.executed_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
