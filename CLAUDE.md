# CLAUDE.md — Crypto Portfolio Tracker

Project-level instructions for Claude Code. These take precedence over your
default behavior in this repository.

## Project at a glance

Multi-tenant crypto portfolio tracker.

- **Stack**: FastAPI + SQLite (WAL) + Jinja2 templates + vanilla JS (no build step)
- **Auth**: EIP-4361 SIWE for EVM wallets, SIWS for Solana — signature-only, no passwords
- **Data**: Moralis v2.2 for on-chain history + token prices
- **Billing**: Credit ledger funded by user wallet deposits to a shared treasury
- **Entry point**: `/root/crypto-tracker-project/webapp/app.py`
- **Run**: `cd webapp && python3 app.py`  (serves on `0.0.0.0:8787`)
- **Logs**: `/root/crypto-tracker-project/webapp/logs/webapp.log` (rotating 2MB × 10)

The legacy CLI scanner under `/root/crypto-tracker-project/scanner.py` is pre-webapp
tooling and is NOT what you should touch for new work — the webapp imports what it
needs from it. Always prefer editing files in `webapp/`.

## Repository layout

```
crypto-tracker-project/
├── CLAUDE.md                   ← this file
├── AGENTS.md                   ← project context for AI agents (same info, tool-agnostic)
├── scanner.py                  ← legacy CLI scanner (imported by webapp)
├── docs/                       ← legacy/setup docs
├── research/                   ← API specs
└── webapp/                     ← the actual app
    ├── app.py                  ← FastAPI entry — all routes live here
    ├── db.py                   ← SQLite layer + schema + credit ledger helpers
    ├── auth.py                 ← SIWE / SIWS verify + session management
    ├── scanner.py              ← Moralis wrapper + chain detection + EVM_CHAINS
    ├── history.py              ← Import wallet transaction history
    ├── pricing.py              ← Quote imports, price tokens
    ├── pnl.py                  ← Cost basis, holdings merge, portfolio summary
    ├── treasury_watcher.py     ← Background thread scanning the treasury for deposits
    ├── wallet_payments.py      ← In-app wallet-signed top-ups (primary pay path)
    ├── rate_limit.py           ← Per-user sliding window limiter
    ├── applog.py               ← Rotating file + stderr logger; export `log`
    ├── static/                 ← app.js, wallet-auth.js, css
    ├── templates/              ← base.html + one per page
    ├── docs/                   ← architecture, security, billing deep dives (read these!)
    └── TODO.md                 ← deferred work
```

## Conventions — follow these exactly

### Logging
- Import `from applog import log` in any module that needs to log — NOT stdlib `logging`.
- Log at info for credit operations, auth verdicts, treasury events, import start/stop.
- Log at warning for auth failures, insufficient balance, rate limits hit.
- Log at error for Moralis failures, DB errors, unexpected exceptions.
- **Do not** sprinkle print() across the app. Old print()s still exist in auth.py for SIWE parse debugging; leave those alone but don't add more.

### Input validation
- Every JSON body route uses a pydantic `BaseModel` subclass with `Field(...)` constraints — min/max length, numeric bounds. No free-form `dict` bodies.
- Per-route rate limiting is mandatory on anything that hits Moralis or mutates state. Call `rate_limit(bucket, user_id, limit, window_seconds)` at the top. See existing routes for the bucket naming convention.

### Credit ledger (critical)
- `credit_charge(user_id, amount)` is atomic via `UPDATE ... WHERE balance >= amount`. **Do not** revert this to a read-then-write pattern even if it looks cleaner — there's a comment above it explaining the race.
- `credit_topup` is idempotent on `(chain, tx_hash)` via a unique partial index. Always let the DB enforce it; never guard with an app-level "check then insert".
- Every ledger mutation MUST go through db.credit_topup / credit_charge / credit_refund — never UPDATE `user_credits` directly from a route.

### XSS
- Every `innerHTML = ...` with user-controlled values MUST wrap the value in `esc()` (defined in `static/app.js`). That includes symbols, labels, notes, chain names, addresses.
- Templates use `{% autoescape %}` — but the moment you drop into a `<script>` block with a template literal, you're responsible. When in doubt, escape.

### Treasury / payments
- The in-app wallet-pay flow (`wallet_payments.verify_and_credit`) is the PRIMARY payment path. The background `treasury_watcher` is the FALLBACK for unsolicited deposits. Don't refactor them back into a single code path.
- ERC-20 allowlist lives in `treasury_watcher.ACCEPTED_CONTRACTS` as `(chain, contract_lower) → canonical_symbol`. Moralis-reported token symbols are NEVER trusted — only the contract address. Adding a token? Add it here.
- Block-confirmation delay per chain lives in `MIN_AGE_SECONDS`. Both the watcher and wallet_payments use it. Keep them in sync.
- Pricing rules: stablecoins are locked at $1.00 (`STABLE_SYMBOLS`), everything else goes through `moralis_historical_price` at the block of the transfer. No CoinGecko fallback on the hot path.

### SIWE
- Domain is verified against either `SIWE_EXPECTED_DOMAINS` env var or the incoming request Host header. Don't accept arbitrary domains from the message itself.
- Nonce is consumed from DB BEFORE `siwe_msg.verify` runs — successful parse + domain check → burn nonce → verify signature. Failed verify still means the nonce is gone (intentional; prevents tamper-resign loops).
- Only supported chains for auto-link: EVM, Solana. Bitcoin is deliberately out of scope for sign-in (no universal standard).

### SQLite
- WAL mode + `busy_timeout = 5000` is set in `get_conn()`. Don't disable it.
- Long reads OUTSIDE a connection context manager can hold locks — always use `with get_conn() as c:`.
- Schema lives in `db.SCHEMA` as a single string; migrations are add-column-with-try-except in `init_db()`.

### Static assets
- `/static/app.js?v={{ static_version }}` — the version is computed from the latest mtime in the static dir on every render, so every redeploy auto-invalidates browser caches. Don't strip it.

## Security non-negotiables

- **No alert/confirm/prompt**. These are being replaced with custom modals. Don't add new uses of the browser ones in new code.
- **No sensitive data in logs**. Addresses are fine; signatures, session IDs, full tx hashes beyond first 12 chars are not.
- **Never bypass the per-user wallet ownership check**. Any route that acts on a wallet_id MUST call `db.get_wallet(user_id, wallet_id)` first and 404 on None. This is the primary cross-tenant leak defense.

## Commands

```bash
# Run the webapp (foreground)
cd /root/crypto-tracker-project/webapp && python3 app.py

# Run in background for dev
nohup python3 /root/crypto-tracker-project/webapp/app.py > /tmp/webapp.log 2>&1 &

# Tail logs
tail -f /root/crypto-tracker-project/webapp/logs/webapp.log

# Manual DB check
sqlite3 /root/crypto-tracker-project/webapp/portfolio.db ".tables"

# Import-test after edits
cd /root/crypto-tracker-project/webapp && python3 -c "import app; print('ok')"
```

## Where things live (quick jump table)

| Task | File |
|---|---|
| Add a route | `app.py` |
| Add a DB helper | `db.py` |
| Add a new accepted payment token | `treasury_watcher.py` → `STABLECOIN_CONTRACTS` or `WRAPPED_TOKEN_CONTRACTS`, then `app.py` → `_TOKEN_DECIMALS` |
| Change pricing formulas | `pricing.py` |
| Change a wallet-history import field | `history.py` |
| Sign-in flow bugs | `auth.py` + `static/wallet-auth.js` |
| Background treasury scan | `treasury_watcher.py` |
| In-app wallet pay verify | `wallet_payments.py` |
| Rate limit tuning | the `rate_limit(...)` call site in `app.py` |

## Rebalance Feature

Added in a multi-session build. Entry points:

### Endpoints (all under `/api/rebalance/`)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/strategies` | List preset + user custom strategies |
| POST | `/strategies` | Create a new custom strategy (rate limited 20/hr) |
| PUT | `/strategies/{id}` | Update a custom strategy |
| DELETE | `/strategies/{id}` | Delete a custom strategy |
| POST | `/calculate` | Compute required swaps for a target allocation (rate limited 60/min) |
| POST | `/quote` | Get Rango swap quotes for a list of swap pairs (rate limited 30/min) |
| POST | `/execute` | Build unsigned Solana txs via Rango for wallet signing (rate limited 10/hr) |
| POST | `/confirm` | Persist tx hashes + final status after the user signs |
| GET | `/history` | Paginated rebalance execution history |

### DB tables added

- **`strategies`** — Both preset (is_preset=1, user_id=NULL) and user-owned (is_preset=0) allocation templates. `allocations` is a JSON object `{symbol: percentage}`.
- **`rebalance_history`** — One row per execution attempt. `swaps` and `tx_hashes` are JSON arrays. `status` is one of `pending | success | partial | failed`.

### Rango integration

`webapp/rango_client.py` wraps the Rango Exchange API for swap routing on Solana.
- `quote_swap(from, to, amount, slippage)` — calls `/basic/best-route`, returns `{output_amount, price_impact, fee_usd, route_id}`.
- `build_swap_transaction(route_id, user_address)` — calls `/tx/create`, returns base64-encoded Solana transaction for wallet signing.
- No API key is required for public Rango endpoints (key slot is wired up for future use).
- **Limitation**: Rango's free-tier routing does not guarantee route availability. If `quote_swap` fails, the quote item carries an `error` key and is skipped in execute.

### React component

`webapp/ui/src/app/components/rebalance.tsx` — full rebalancer UI.
- Uses inline `Banner`, `ConfirmDialog`, and `AddAssetDialog` components instead of `alert()`/`confirm()`/`prompt()` (project convention).
- All fetch calls check `res.ok` before consuming the body.
- Holdings value key: `/api/holdings` returns `current_value` (set by `pnl.merge_holdings_with_cost_basis`); `usd_value` is the raw DB key used by `/api/rebalance/calculate` which calls `db.list_holdings_by_symbol` directly. The component reads `h.current_value ?? h.usd_value` to handle both paths.

### Known limitations / deferred work

- Execute flow is Solana-only (uses `window.solana`). EVM wallet signing not yet wired.
- Rango contract addresses in `XSTOCKS_LIST` are placeholders — need to be replaced with real mainnet addresses before enabling xStock swaps.
- No on-chain verification of submitted tx hashes in `/confirm` — the status is user-reported. Add Solana RPC confirmation polling as a follow-up.
- Strategy allocations must sum to exactly 100% (±1%) — enforced at both API and UI level.

### Bug fixed during audit (2026-05-01)

- `db.get_user_by_id()` was called in `/api/rebalance/execute` but does not exist in `db.py`. Fixed to use `db.get_user(user_id)`.
- `/api/rebalance/calculate` was missing a `rate_limit()` call. Added at 60 req/min.
- `RebalanceQuote.swaps` and `RebalanceExecute.quotes` used bare `list` type — replaced with typed Pydantic sub-models.
- All `alert()`/`confirm()`/`prompt()` calls in `rebalance.tsx` replaced with custom modal components.
- Missing `res.ok` check in `getQuotes()` fetch call added.

## Reference docs

For deeper context on specific subsystems, see:
- `webapp/docs/ARCHITECTURE.md` — full system overview, data flows, threading model
- `webapp/docs/SECURITY.md` — threat model, fixes applied, non-goals
- `webapp/docs/BILLING.md` — credit ledger math, in-app pay flow, treasury watcher fallback
- `webapp/docs/CHANGELOG.md` — dated session log of what changed and why
- `webapp/TODO.md` — deferred work, ordered by priority
