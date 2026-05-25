# Crypto Tracker — Deferred Work

Items we know about but are not shipping right now. Ordered roughly by when
they'd move the needle. Update this file instead of letting ideas die in
chat history.

## Rebalance Feature

- [ ] **xStock contract addresses** — `rango_client.XSTOCKS_LIST` has placeholder Solana addresses. Replace with real mainnet addresses before enabling xStock swaps.
- [ ] **On-chain tx confirmation in /rebalance/confirm** — currently trusts user-reported status. Add Solana RPC polling to verify each submitted tx hash actually landed.
- [ ] **EVM wallet signing for rebalance** — execute flow uses `window.solana` only. Wire up EVM path for MetaMask/WalletConnect users.
- [ ] **Rango API key** — `rango_client.RANGO_API_KEY` is None; get and configure a key for higher rate limits and route quality.
- [ ] **Preset strategies seeding** — `strategies` table has `is_preset=1` rows but no seed script populates them. Add migration/seed for built-in presets (e.g. "60/40 BTC+ETH", "Equal Weight Top 5").

## Product / UX

- [ ] **Transaction pagination** — list_transactions returns up to 5000 rows today; client-side filter chokes past ~10k. Add server-side limit/offset + infinite scroll on the transactions page.
- [ ] **CSV export** — button on transactions page + holdings page. Plain text CSV, one row per tx / holding.
- [ ] **Per-chain picker UI on import** — backend already honors `chains` list; UI still sends null. Add checkboxes next to the detected active chains in the import modal.
- [ ] **Periodic auto-scan cron** — daily background sweep of every wallet's holdings so users don't have to click "Scan all". Already have `/api/scan-all`; just needs a scheduler.
- [ ] **Admin view for unclaimed deposits** — treasury_watcher already records sender→can't-match deposits; needs a page that lists them and lets an operator manually resolve (link to a user).
- [ ] **Deposit notifications** — when a credit lands, ping the user via Telegram/email.
- [ ] **BTC top-ups** — current payment flow is EVM-only. Add native BTC by generating a unique receiving address per user and watching it.
- [ ] **Solana payment flow** — signup works today but Solana users can't top up credits. Needs a Solana-side treasury + USDC-SPL support.
- [ ] **Solana treasury watcher** — current watcher is EVM-only.
- [ ] **Re-sync prices for existing txs** — button that re-runs enrich_with_prices for transactions with price=0.

## Reliability / Ops

- [ ] **Graceful shutdown of treasury watcher** — thread is daemonized so it dies on SIGTERM; explicit join on shutdown is cleaner.
- [ ] **Migrate @app.on_event → lifespan handler** — FastAPI deprecated the old API; currently throws DeprecationWarning on boot.
- [ ] **Error monitoring (Sentry / equivalent)** — today errors only land in stdout. Plug in Sentry or roll a lightweight error emailer.
- [ ] **Structured JSON logging** — current logging is plain text. Structure it so we can ship logs to a viewer later.
- [ ] **Off-site DB backup** — today we snapshot to Telegram. Add S3 / Hetzner Storage Box as a secondary destination.
- [ ] **Health-check deep mode** — `/api/health?deep=1` that pokes the DB, Moralis key pool, and treasury watcher liveness.

## Security (nice-to-have)

- [ ] **CSRF token on mutating routes** — today we rely on SameSite=Lax cookies + JSON-only bodies. Adding an explicit CSRF token would tighten this.
- [ ] **Per-IP rate limit (in addition to per-user)** — current limiter is per-user only; unauth'd endpoints like `/api/auth/nonce` aren't limited at all.
- [ ] **SIWE chain_id pinning** — we accept any chain_id today; pin to the mainnet(s) we actually deploy against.
- [ ] **Tighter Solana address validation** — current check is just base58-decodable.

## Testing / CI

- [ ] **Full test coverage** — initial tests cover pnl, credit ledger, pricing, treasury watcher, rate limit. Expand to auth, history, scanner, app routes.
- [ ] **CI pipeline** — run tests on every push. No CI today.
- [ ] **End-to-end browser test** — Playwright / similar for the sign-in → add-wallet → import → billing flow.

## Internal

- [ ] **Move constants out of modules** — TREASURY_ADDRESS, allowlists, pricing constants should live in a single `config.py` keyed off env.
- [ ] **Replace sqlite with Postgres** — once we have >100 users or need multi-worker deployment.
