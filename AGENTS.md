# AGENTS.md — Crypto Portfolio Tracker

Project context for AI coding agents (Claude Code, Cursor, Codex, Aider, etc).
Tool-agnostic. Claude Code users should also read `CLAUDE.md` which adds
Claude-specific instructions on top of this.

## What this project is

A self-hosted crypto portfolio tracker. Users sign in with their wallet (SIWE
for EVM, SIWS for Solana), link one or more wallets to their account, and the
app imports their on-chain history via Moralis + computes P&L, holdings, and
cost basis. Paid operations (history imports) are funded by a per-user credit
balance topped up by sending accepted tokens to a shared treasury address.

- Single-operator service (not a SaaS platform yet)
- Single FastAPI process + background thread for treasury polling
- SQLite for state (WAL mode), Moralis for chain data
- Jinja2 + vanilla JS frontend, no build pipeline
- Wallet signatures for auth, no passwords

Status: **alpha**. Security posture is hardened for single-operator trust, but
it has NOT been externally audited. Do not onboard third-party users without
an audit first.

## High-level architecture

```
        Browser (EVM/SVM wallet via ethers.js / @solana/web3.js)
           │
           ▼
   ┌──────────────────────────────────────────┐
   │  FastAPI app.py (port 8787)              │
   │  ├── Session cookies (Starlette)         │
   │  ├── Per-user rate limit (sliding window)│
   │  └── Route handlers                      │
   └──────┬────────────────────────────┬──────┘
          │                            │
          ▼                            ▼
   ┌──────────────┐         ┌────────────────────────┐
   │  SQLite WAL  │         │  Moralis API (v2.2)    │
   │  portfolio   │         │  - wallet history      │
   │  .db         │         │  - token prices        │
   │              │         │  - transaction lookup  │
   └──────────────┘         └────────────────────────┘
          ▲
          │ writes on maturity
          │
   ┌──────┴────────────────────────┐
   │  treasury_watcher (daemon     │
   │  thread, 90s poll)            │
   │  → credits user_credits on    │
   │    allowlisted ERC-20 / native│
   │    deposits to treasury       │
   └───────────────────────────────┘
```

## Key subsystems

### 1. Authentication — `auth.py`
EIP-4361 SIWE for EVM via `siwe-py`. SIWS for Solana via `pynacl`.
- Nonce issued server-side, stored in DB, single-use, 10 min TTL
- Domain binding: message `domain` field verified against env allowlist or request Host header — prevents phishing-site replay
- First sign-in auto-creates a user and auto-links the signing wallet as verified
- Additional wallets require a separate link-signature

### 2. Database — `db.py`
SQLite with `PRAGMA journal_mode=WAL; busy_timeout=5000`.
Tables:
- `users` — one per account (primary address + chain_type)
- `wallets` — n-to-1 to users, with verified flag
- `transactions` — imported tx history
- `user_credits` — current balance per user
- `credit_transactions` — append-only ledger (`kind` ∈ topup/charge/refund)
- `wallet_imports` — per-(user, wallet) import history for rate-capping
- `auth_nonces` — short-lived SIWE/SIWS nonces

Critical invariant: the partial index `(chain, tx_hash) WHERE kind='topup'`
is unique, which makes top-up credits idempotent at the DB level.

### 3. Credit ledger
- `credit_topup` — INSERT into `credit_transactions`, catch `IntegrityError` as idempotent replay, then `_apply_credit_delta`.
- `credit_charge` — **atomic** `UPDATE user_credits SET balance = balance - ? WHERE user_id = ? AND balance >= ?`. The WHERE clause is what makes two concurrent imports safe: the second's UPDATE sees the already-debited balance and fails with rowcount=0. Do not refactor this into a read-then-write.
- `credit_refund` — positive delta, used when an import fails mid-flight.

### 4. Treasury payment flow (dual)

**Primary (in-app, `wallet_payments.py`):**
1. User clicks Top-up $N on billing page
2. Frontend builds ERC-20 `transfer()` (or native send) to treasury, signs in wallet
3. Posts tx_hash → `/api/billing/pay/verify`
4. Server fetches tx from Moralis, validates maturity, allowlist, amount tolerance (±1%), sender-is-linked, then credits

**Fallback (background, `treasury_watcher.py`):**
- Daemon thread polls each EVM chain's wallet history for the treasury address every 90s
- Same validation pipeline as the primary path
- Catches unsolicited deposits and users who fell off the in-app flow
- Can be nudged early via `POST /api/billing/poll-now` (rate-limited 2/min/user)

### 5. Pricing — `pricing.py` + `treasury_watcher._price_token`
- Stablecoins: locked at $1.00 via a `STABLE_SYMBOLS` set
- Everything else: `moralis_historical_price(contract, chain, block_number)` at the tx's block

### 6. Rate limiting — `rate_limit.py`
Sliding-window, per-(bucket, user_id), in-memory. Fail-open on internal error.
Buckets in use:
- `import_history` (10/hour) — most expensive op
- `poll_now` (2/min) — treasury re-poll nudge
- `pay_verify` (30/hour) — in-app wallet top-up
- `scan_wallet`, `scan_all`, `estimate_import`, `chain_detect`, `add_tx` — cheaper buckets on bulk reads and cheap mutations

## Coding standards

1. **Defensive defaults**: every on-chain value is re-derived from the contract+chain tuple, never from Moralis-reported symbols. The allowlist is the ground truth.
2. **Input validation via pydantic**: no route takes a raw dict body. All inputs have length/range constraints.
3. **XSS**: all user-controlled values interpolated into innerHTML go through `esc()` in `app.js`.
4. **No side effects in helpers**: functions in `db.py` either pure-read or inside a single `with get_conn()` block. No hidden HTTP.
5. **Logging via `applog.log`**: info/warning/error only, structured strings, no stacktraces at info.
6. **No alert/confirm/prompt**: replaced with custom modals (in progress).

## Environment variables

```
MORALIS_API_KEY         primary Moralis key (pool of keys in moralis_keys.py)
TREASURY_ADDRESS        the wallet receiving top-ups
SIWE_EXPECTED_DOMAINS   comma-separated allowlist of domains SIWE messages may name
SESSION_SECRET          auto-generated on first run to ./session.key
```

## Security non-goals (explicit)

Things we are deliberately NOT defending against:
- Malicious operators with SSH/DB access (trust boundary is the server)
- Traffic analysis / TLS (expected to run behind a reverse proxy)
- Subnet-level DDoS (rely on CDN / host provider)
- Censorship-resistant operation (Moralis is a SPOF)
- Multi-signer treasury management (single EOA for now)

## Known open items

See `webapp/TODO.md` for the full list. Highlights:
- Transaction pagination (client-side today, chokes past ~10k tx)
- Browser prompt/confirm/alert → custom modals (in progress)
- Error monitoring (Sentry)
- CSRF tokens (currently rely on SameSite=Lax + JSON bodies)
- Solana payment flow (sign-in works, top-ups don't)
- BTC top-ups (EVM-only today)
- Full CI + E2E browser tests
