# Architecture

## Process model

Single FastAPI process (`uvicorn` via `app.py`) serving on port 8787.
One daemon thread spawned at startup: `treasury_watcher._loop`.

No forking, no multi-worker. SQLite is the bottleneck; if we ever outgrow
single-process we migrate to Postgres before anything else (see TODO.md).

```
┌─────────────────────────────────────────────────┐
│  uvicorn main thread                            │
│   ├── FastAPI app (routes)                      │
│   ├── Starlette SessionMiddleware               │
│   └── StaticFiles("/static")                    │
│                                                 │
│  treasury_watcher daemon thread                 │
│   └── 90s poll loop, wakes early on Event       │
└─────────────────────────────────────────────────┘
             │                           │
             │ sqlite3 module            │ HTTPS
             ▼                           ▼
        portfolio.db              Moralis v2.2
       (WAL mode, one           (pool of API keys
        file, local)             in moralis_keys.py)
```

## Request lifecycle

1. Request arrives → Starlette session middleware attaches `request.session`
2. Route depends on `auth.require_user` which pulls `user_id` from the session cookie (401 if missing)
3. Most mutating routes call `rate_limit(bucket, user_id, ...)` as the first line of the handler
4. Pydantic model validates the body (length/range constraints enforced here)
5. Handler runs — DB work via `db.*`, external calls via `scanner._moralis_get` or helpers
6. Response serialized as JSON (or HTML for pages via `render()`)

## Data model

### Users + wallets
- `users` — one row per account; `primary_address` is the first wallet signed in
- `wallets` — n-to-1 to users; `verified=1` set only after a link-signature is accepted (or the login signature for the primary wallet)

### Transactions
- `transactions` — one row per on-chain leg AFTER normalization. Imported from Moralis via `history.import_wallet_history` for EVM chains and from Helius via `sol_history.fetch_sol_history` for Solana. Columns include `ts, wallet_id, chain, tx_hash, symbol, amount, price_usd, side, notes`. The `chain` column carries the importer's slug; `sol_history` rows land as `chain="solana"`.
- `wallet_imports` — bookkeeping: one row per completed import, used to rate-cap re-imports.

### Credits
- `user_credits` — current balance per user (one row). Ledger materialized view.
- `credit_transactions` — append-only. Every balance change has a row here. `kind ∈ {topup, charge, refund}`. Unique partial index on `(chain, tx_hash) WHERE kind='topup'` enforces idempotency.
- `auth_nonces` — short-lived SIWE/SIWS nonces. Consumed on verify.

### Schema migrations
Schema is a single string in `db.SCHEMA`. New columns are added via
`try: ALTER TABLE ... ADD COLUMN / except OperationalError: pass` in
`init_db()`. No proper migration tool; acceptable while we're pre-users.

## Frontend

### Base layout
`templates/base.html` holds the nav, the profile corner, the auth guard,
and loads `app.js` + `wallet-auth.js`. Every page extends it and fills
`{% block content %}` + `{% block scripts %}`.

### Static cache busting
All `<script src>` tags use `?v={{ static_version }}` where `static_version`
is the latest mtime across `static/` computed per-render. Any file change
invalidates the cache automatically on the next page load — no need for
users to hard-refresh after a deploy.

### Shared helpers — `static/app.js`
- `esc(s)` — HTML escape for innerHTML interpolation. **Use everywhere.**
- `fmtUSD(n)`, `fmtNum(n)` — display formatters
- `toast(msg)` — ephemeral bottom-right notification

### Wallet signing — `static/wallet-auth.js`
- Connects injected EIP-1193 providers (MetaMask / Rabby / Coinbase / etc)
- Constructs a SIWE message, gets signature, posts to `/api/auth/verify-siwe`
- Sibling path for Solana (Phantom / Solflare / Backpack) via `window.solana`
  signMessage → `/api/auth/verify/solana`. Requires **HTTPS or localhost
  origin** — Phantom silently drops `connect()` on plain HTTP as an
  anti-phishing measure, so any non-localhost HTTP deployment blocks the
  Solana wallet link flow (the Helius history import is unaffected,
  that's purely server-side).
- **Dependency load order:** `ui/src/app/lib/auth.ts#loadWalletAuth`
  first injects `/static/ethers.umd.min.js` (self-hosted, 505 KB) then
  `/static/wallet-auth.js`. Self-hosting is deliberate — Edge's Tracking
  Prevention and various aggressive browser privacy modes block
  cross-origin CDN storage access, which used to leave the "Add wallet"
  modal stuck on "Waiting for signature…" because `wallet-auth.js`
  never loaded. Same-origin load always works.

### Per-page templates
- `landing.html` — sign-in entry
- `dashboard.html` — P&L summary, allocation pie, chain split toggle
- `holdings.html` — by-symbol holdings with cost basis
- `transactions.html` — paginated tx history with filters
- `wallets.html` — linked wallet management + import history button
- `billing.html` — credit balance, in-app wallet pay flow, credit history
- `profile.html` — account settings

## Threading + concurrency

### Treasury watcher
Runs in a daemon thread spawned at startup. Loop body:
1. Check `_WAKE_EVENT.wait(timeout=1)` — lets `poll_now` API wake it early
2. If last run was <90s ago and no wake event, sleep
3. Otherwise fetch treasury history for each EVM chain, filter to mature txs (age ≥ MIN_AGE_SECONDS[chain]), check contract allowlist, try to match sender to a linked user wallet
4. If match: `db.credit_topup` (idempotent). If no match: log as unclaimed and move on.

### SQLite concurrency
- WAL mode lets multiple readers run alongside one writer
- `busy_timeout=5000` forces SQLite to retry internally on lock conflict
- All writes go through `with get_conn() as c:` which commits on exit
- `credit_charge` uses conditional UPDATE (balance predicate in WHERE) so two concurrent imports can't both decrement a balance that only supports one

### Rate limiting
In-memory `dict[(bucket, user_id), deque[timestamp]]` guarded by a
`threading.Lock`. Sliding window. Fails open on internal error (logged).
Does NOT survive a restart — acceptable since the most expensive buckets
(import_history) have server-side cost caps via the credit ledger anyway.

## External integrations

### Moralis
- v2.2 wallet history endpoint for import
- v2.2 single-tx endpoint for `wallet_payments.verify_and_credit`
- v2.2 historical price endpoint for non-stable token pricing
- API key pool in `moralis_keys.py` — rotates on 429 / 401 via `_moralis_get`

### Helius — Solana transaction importer
- **Endpoint:** `GET /v0/addresses/{address}/transactions` (Enhanced
  Transactions API). Helius parses raw Solana transactions into
  structured SWAP/TRANSFER/UNKNOWN events so `sol_history.py` doesn't
  have to touch borsh layouts. Walks newest-first with a `before=<sig>`
  cursor.
- **Wrapper:** `sol_history._helius_page` retries 429/5xx/network with
  exponential 2→4→8s backoff over 3 attempts, returns `None` on
  persistent failure (distinct from `[]` end-of-history). The caller
  `fetch_sol_history` raises `RuntimeError` on `None` rather than
  silently truncating — a partial Solana history is strictly worse than
  no history, because missing early buys turn retained sells into
  phantom zero-basis disposals.
- **Parsing:** `_parse_swap` (sell input + buy output rows),
  `_parse_transfer` (deposit/withdraw), `_parse_native_transfers`
  (native SOL, 0.001 SOL noise floor). All three check for
  self-transfers at the row level (`fromUserAccount ==
  toUserAccount == wallet`) because Solana rows don't carry a
  `tx_hash` the tax layer's EVM self-transfer collapser can use.
- **Spam filter:** `tax/solana_tokens.is_spam_token(mint, tx_type)`.
  Whitelist of 22 verified SPL mints; unlisted mints are kept only if
  `tx_type` is SWAP or TRANSFER (voluntary user engagement) and
  dropped otherwise (AIRDROP, UNKNOWN, CONTRACT_INTERACTION …). The
  dispatcher in `_parse_tx` passes the original tx type into
  `_parse_transfer` so the filter sees the real context.
- **Truncation ceiling:** `max_txs` default 5000 (vs 1000 on the EVM
  walker) because Helius has no `after=` cursor — we can't walk
  oldest-first. When the ceiling is hit, `fetch_sol_history` logs a
  loud WARNING matching `history.py`'s style and returns what it has.

### Birdeye — SPL historical prices
- `birdeye_prices.py` — thin client over `/defi/history_price`. Called
  only as the third fallback in the Solana price waterfall, after the
  stablecoin short-circuit and Binance klines. Exponential backoff on
  429, session-disable on 401/403 (bad/expired key) so a misconfigured
  key doesn't fan out into quota-burning retries.
- SQLite cache keyed by `(mint, date)` reuses the same `price_cache.db`
  file as `binance_prices`, under a `sol:` prefix so the namespaces
  don't collide.

### siwe-py
EIP-4361 parsing and verification. Strict about:
- Alphanumeric-only nonces (no URL-safe base64)
- No milliseconds in the `Issued At` field
- Exact domain/URI match with what `from_message` parses

### pynacl
Ed25519 signature verification for Solana SIWS.

## Observability

- Rotating file logger at `logs/webapp.log` (2MB × 10 files)
- StreamHandler duplicates to stderr so `journalctl` / `tail /tmp/webapp.log` work during dev
- No metrics, no traces, no Sentry yet (see TODO.md)

## Deployment assumptions

Runs on a single VPS. No reverse proxy required for correctness, but
recommended for HTTPS. Assumptions baked into the code:
- `https_only=False` on the session cookie (flip if fronted by TLS)
- `SIWE_EXPECTED_DOMAINS` env var is the source of truth for phishing defense
  — set this explicitly in production, don't rely on the Host-header fallback
- Moralis keys are hot (no key-management service)
- SQLite file is local; backup strategy is ad-hoc (see TODO.md for S3/etc)
