# Changelog

Dated log of notable changes. Prepend new entries at the top. Keep entries
short (bullet per change) with "why" commentary where the "what" isn't obvious.

## 2026-04-11 — Solana support + self-transfer fix + tax UI + stability

A multi-topic day: closed the x4rde 2025 phantom-loss bug, added row-level
self-transfer detection to the tax engine, built the Spec-ID lot picker UI
all the way through, shipped Solana history import behind a whitelist-based
spam filter, and self-hosted ethers to survive aggressive browser privacy
modes.

### Tax engine

- **Self-transfer detector** (`tax/self_transfers.py`) — wallet-to-wallet
  moves no longer get treated as disposals. The detector buckets
  withdraw/deposit pairs by canonical symbol and greedy-matches the closest
  timestamp pair inside a 1-hour window with 0.5% amount tolerance
  (headroom for gas burn on the outbound leg). The calculator now filters
  these out before the cost-basis walker sees them, and emits a caveat
  line telling the user how many pairs were collapsed.
  - **Regression context:** x4rde's 2025 report was showing -$377.64. Turned
    out $126 of that was a phantom loss from a 0.1 ETH withdraw + deposit
    34 seconds apart at identical prices, i.e. a self-transfer that the
    walker had been happily treating as a sale at a loss. A second USDC
    pair (~$1400) was also caught. Genuine ETH loss after filtering:
    ~$251.
  - Three new smoke tests in `test_tax_methods.py` lock the behaviour in:
    same-window match, cross-symbol negative (BTC withdraw ≠ ETH deposit),
    outside-window negative (2h-apart).
- **Dataclass ordering fix** in `calculator.TaxReport` — FX fields with
  defaults had been inserted before `disposal_count`/`asset_count` which
  is a `TypeError` under `@dataclass`. Moved the FX fields back behind
  the non-default fields.
- **Naive-datetime normalisation** — `methods._parse_ts` strips timezone
  info on return so the UK matcher (and anything else that `datetime`-
  compares) doesn't hit "can't compare offset-naive and offset-aware"
  when a mix of TZ-aware and naive rows arrive from different importers.

### Spec-ID lot picker UI

- **`tax/lots_view.build_sell_lot_snapshots`** — for every sell/withdraw in
  the filtered history, returns a snapshot of the open lots as of just
  before the disposal: `[{lot_id, acquired_ts, remaining, cost_per_unit,
  original_amount, source_tx_id}]`. Runs the FIFO walker internally but
  only consumes lots so subsequent snapshots see the correct remaining
  balances. Read-only audit view — doesn't produce disposals itself.
- **API endpoints:**
  - `GET /api/tax/lot-snapshots?year=&jurisdiction=` — returns the
    snapshot list scoped to the fiscal year window.
  - `GET /api/tax/overrides` — existing user-set Spec-ID picks.
  - `POST /api/tax/overrides` — persist a {sell_tx_id → {lot_id: units}}
    mapping.
  - `DELETE /api/tax/overrides/{sell_tx_id}` — clear overrides for one
    sale (falls back to FIFO for that sale only).
- **React `SpecIdPicker`** in `tax.tsx` — accordion per sale with
  BALANCED/FIFO FALLBACK status chips, per-lot number inputs that
  auto-save on blur (optimistic UI), and a "Reset to FIFO" button per
  sale. Loads snapshots on method=="SPEC" + year/jurisdiction change.
- **`app._load_spec_id_map(user_id, method)`** — central helper that
  returns the `{sell_tx_id: {lot_id: units}}` dict when method=="SPEC",
  wired into both `/api/tax/report` and `_build_report_or_400` so the
  same override map flows through report generation and row-level audit.

### Solana history import

- **`sol_history.py`** — new walker for Solana wallets using the Helius
  Enhanced Transactions API. Parses SWAP (2-row sell+buy), TRANSFER
  (deposit/withdraw), native-SOL (lamports), and UNKNOWN/AIRDROP via a
  spam filter. Price waterfall: stablecoin shortcut → Binance klines →
  Birdeye `/defi/history_price` → $0.00 with a loud warning. Rows match
  the exact schema `db.add_transaction` expects from the EVM walker, so
  no tax engine changes were needed.
- **`tax/solana_tokens.py`** — whitelist of 22 verified SPL mints (SOL,
  USDC/USDT, mSOL/jitoSOL/bSOL, JUP/RAY/ORCA/PYTH/JTO/JLP, BONK/WIF/
  POPCAT/WEN/PENGU/SAMO, W/HNT/SHDW/MNDE). Allowlist rationale: Solana
  token creation is essentially free (<$0.50) so a blacklist can never
  keep up with dust-airdrop campaigns; the curated list of tokens that
  generate real taxable events for 99% of wallets is small and stable.
- **`birdeye_prices.py`** — Birdeye historical price client with SQLite
  cache (same pattern as `binance_prices`), exponential backoff on 429
  (2s→4s→8s, 3 retries), 401/403 session disable so a bad key doesn't
  fan out into quota-burning retries.
- **`app.py`** — `_run_import_for_wallet` routes to `sol_history` when
  `chain == "solana"`; `api_import_history` accepts Solana wallets.
- **Test coverage** (`test_sol_history.py`): 10 tests, all mocked (no
  live API keys needed). Covers the allowlist, spam filter,
  self-transfer skip (token + native paths), spam-airdrop drop, retry
  logic, persistent-error raise, and the full happy path.
- **Review cycle:** build done by a Sonnet agent, reviewed by a separate
  Opus agent. Opus found 4 majors (`is_spam_token` defined but never
  called, `max_txs` truncation direction wrong, pagination swallowing
  transient errors, missing self-transfer test). All four fixed before
  merge — the review report is preserved at
  `tax/SOLANA_REVIEW_REPORT.md`. Spam filter now runs on every unlisted
  mint via a `spam_filter_type` param on `_parse_transfer`; default
  `max_txs` raised from 1000 to 5000 with an explicit ceiling warning
  matching the EVM walker's style; `_helius_page` now returns `None`
  on persistent error (retries 429/5xx with 2→4→8s backoff) and the
  pagination loop raises `RuntimeError` instead of silently truncating.

### Frontend resilience

- **Self-hosted `ethers.umd.min.js`** — moved from `cdn.jsdelivr.net` to
  `/static/ethers.umd.min.js` (505 KB). Edge's Tracking Prevention and
  various strict browser privacy modes block cross-origin CDN storage
  access, which silently killed `wallet-auth.js` loading (it's chained
  after ethers), which left `linkSolanaWallet` / `linkEvmWallet` /
  `signInWith*` all undefined, which left the "Add wallet" modal
  forever stuck on "Waiting for signature…". Same-origin load always
  works. Patched in `ui/src/app/lib/auth.ts` and rebuilt the SPA.

### Known gap (not a bug, tracked)

- **Phantom refuses to `connect()` over plain HTTP** — the VPS is
  currently served as `http://45.93.136.6:8787`. Phantom (and most
  Solana wallet extensions) silently reject connect calls on non-HTTPS
  origins as an anti-phishing measure. The promise returned by
  `provider.connect()` stays in `pending` forever, no error, no popup.
  Localhost is the only HTTP exception. Workaround for dev: SSH tunnel
  to `localhost:8787`. Proper fix: domain + Cloudflare proxy + SSL.
  Tracked in `Tasks/2026-04-11.md` in the obsidian vault. EVM wallet
  linking is unaffected because MetaMask/Rabby etc. don't enforce the
  HTTPS constraint.

## 2026-04-10 — Security pass + in-app wallet pay

A single long session combining a full code review and the first phase of
fixes + a product pivot on the payment flow.

### Security fixes (critical/high)

- **Atomic `credit_charge`** — rewrote from read-then-write to
  `UPDATE ... WHERE balance >= amount`. Two concurrent imports on the same
  user can no longer both debit a balance that only supports one. Verified
  with a 5-thread concurrency test (exactly 2 of 5 $4 charges succeed against
  a $10 balance).
- **Fake-token contract allowlist** — `treasury_watcher.ACCEPTED_CONTRACTS`
  is now the ground truth for every ERC-20 deposit. Moralis-reported token
  symbols are no longer consulted for credit decisions. Added 20 stablecoin
  contracts + 12 wrapped-token contracts across 6 chains.
- **SIWE domain binding** — `verify_siwe` now checks `siwe_msg.domain`
  against either the `SIWE_EXPECTED_DOMAINS` env var or the request's Host
  header. A phishing site that tricked a wallet into signing a message
  naming its own domain can no longer replay that signature at us. Domain
  check runs BEFORE nonce consumption.
- **Nonce double-check** — `siwe_msg.verify(signature, nonce=nonce)` passes
  the expected nonce explicitly so siwe-py double-checks it matches the
  signed nonce.
- **Block confirmation delay** — `MIN_AGE_SECONDS` dict per chain (60-180s);
  the watcher and in-app verify both reject txs younger than this. Protects
  against reorgs.
- **poll-now DoS fix** — `/api/billing/poll-now` used to run a full
  synchronous Moralis scan per call, blocking the worker and burning the
  API quota. Now it just `Event.set()`s the background thread and returns.
  Rate-limited 2/min/user.
- **Cross-tenant wallet_id leak fix** — every route taking `wallet_id`
  path param now does `db.get_wallet(user_id, wallet_id)` → 404 on None.
- **TOCTOU on import pricing** — `ImportHistoryIn.max_cost_usd` in request
  body; server re-quotes and 409s if the real cost has drifted above the
  user's authorized cap.
- **XSS pass** — `esc()` helper added to `static/app.js`; every `innerHTML`
  interpolation in wallets.html, transactions.html, holdings.html, billing.html
  that touches a user-controlled value now escapes it. IDs forced through
  `Number()`.
- **Static asset cache busting** — `/static/*` script tags get
  `?v={{ static_version }}` where version is the latest mtime across
  `static/`. Prevents stale-cache bugs like the "esc is not defined"
  crash users saw immediately after the XSS fix shipped.
- **Input validation** — every JSON body route now uses a pydantic
  `BaseModel` with explicit `Field(...)` constraints on lengths and ranges.
  No raw dict bodies anywhere.
- **Rate limiting** — sliding-window per-(bucket, user_id). Buckets on
  `import_history`, `poll_now`, `pay_verify`, `scan_wallet`, `scan_all`,
  `estimate_import`, `chain_detect`, `add_tx`.
- **Caps** — 50 wallets per user, 100k transactions per user, 10 history
  imports per wallet per rolling 30 days.
- **Treasury address via env** — `TREASURY_ADDRESS` now read from env (or
  `.env` file) with a hardcoded fallback for dev.

### Infrastructure

- **`applog.py`** — rotating file + stderr logger (2MB × 10). All modules
  now import `from applog import log`. Critical paths log at info/warning/error.
- **`rate_limit.py`** — 60-line in-memory sliding-window limiter with
  `threading.Lock`. Fails open on internal error.
- **WAL mode** — `PRAGMA journal_mode=WAL; busy_timeout=5000` set in
  `get_conn()` and `init_db()`. Multiple readers + one writer, lower lock
  contention.
- **`wallet_imports` table** — bookkeeping for the per-wallet import rate cap.

### Product — in-app wallet pay (replaces treasury-watcher-as-primary)

The user pushed back on making the treasury watcher the primary pay flow.
Rewrote as:

- **`wallet_payments.py`** — new module. `verify_and_credit` fetches a
  user-submitted tx hash via Moralis `/transaction/{tx_hash}`, runs the
  full validation pipeline (maturity, allowlist, amount tolerance ±1%,
  sender-is-linked), then credits via `db.credit_topup` (idempotent on
  the DB unique index).
- **`POST /api/billing/pay/verify`** — new endpoint. Rate-limited 30/hr.
  Returns structured `{status: credited|pending|rejected, reason, amount_usd, balance_usd}`.
- **`GET /api/billing/pay/tokens`** — per-chain stablecoin config derived
  from the treasury watcher allowlist. Frontend uses it to populate the
  pay dropdowns without ever drifting from the backend's source of truth.
- **`db.find_credit_topup`** — idempotency lookup by `(chain, tx_hash)`.
- **`billing.html`** — new "Top up with connected wallet" card: network +
  token dropdowns, amount input, quick-pick buttons, ethers v6 pay flow
  (`BrowserProvider → wallet_switchEthereumChain → ERC-20 transfer() → tx.wait(1)`),
  then polls verify every 20s for up to 4 minutes. Manual transfer block
  moved to a collapsed "Advanced · fallback" `<details>`.
- **Scope note**: in-app pay only offers stablecoins (USDC/USDT/DAI) —
  volatile tokens (WETH/WBTC) require live-price slippage math we haven't
  built. The watcher still credits them on manual transfers.

### Product — deferred (TODO.md)

Items moved to `TODO.md` with explicit priorities:
- Transaction pagination (client-side today, chokes past ~10k txs)
- Custom modals to replace alert/confirm/prompt (in progress)
- Dashboard stale-data warning (>24h)
- DB backup + Telegram delivery cron
- Unit tests for major subsystems
- Sentry / error monitoring
- CSRF tokens, per-IP rate limiting
- Solana payment flow
- BTC top-ups
- Admin view for unclaimed deposits
- Graceful shutdown + lifespan handler migration

### Docs

- `CLAUDE.md` and `AGENTS.md` added at project root
- `webapp/docs/` populated with ARCHITECTURE, SECURITY, BILLING, CHANGELOG
- KB research notes written to `/root/obsidian-vault/KB/` (see its INDEX)
