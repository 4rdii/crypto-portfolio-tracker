# Security Model

Threat model, mitigations, and deliberate non-goals for the crypto tracker
webapp. Kept in sync with actual code; when you change a defense here, grep
for the mitigation label and update both sides.

## Trust boundaries

- **Trust**: the operator (anyone with SSH to the VPS)
- **Partial trust**: the user's wallet (we verify signatures; we don't inspect the wallet implementation)
- **Untrusted**: the user's browser session (can be tampered), all request bodies, every Moralis response field (we re-derive from contract+chain, never trust reported symbols), other users on the same deployment

## Primary attack surface

1. **Web routes** — FastAPI endpoints. Every mutating route is auth-gated + rate-limited + pydantic-validated.
2. **SIWE/SIWS verify** — signature forgery attempts, nonce replay, phishing-site domain spoofing.
3. **Treasury deposit crediting** — fake stablecoins, reverted txs, front-running another user's payment.
4. **XSS via user-controlled strings** — wallet labels, transaction notes, token symbols.
5. **Cross-tenant data leaks** — one user's wallet_id ending up in another user's query.

## Mitigations in place

### Auth — SIWE/SIWS

| Defense | Where | Notes |
|---|---|---|
| Nonce single-use + server-stored | `auth.issue_nonce`, `db.consume_nonce` | 10-min TTL, alphanumeric-only per EIP-4361 spec |
| Domain binding | `auth.verify_siwe` | Checks `siwe_msg.domain` against `SIWE_EXPECTED_DOMAINS` env or request Host header. Rejects phishing-signed messages naming attacker's domain. |
| Nonce in verify() | `auth.verify_siwe` | Nonce passed explicitly to `siwe_msg.verify(signature, nonce=nonce)` so the signed nonce is double-checked. |
| Check-before-consume order | `auth.verify_siwe` | Domain check runs BEFORE nonce consumption so a phishing attempt doesn't burn a nonce. |
| Ed25519 via pynacl | `auth.verify_siws` | `nacl.signing.VerifyKey.verify` throws on bad signature. |
| Nonce-in-message check | `auth.verify_siws` | Plaintext search for the issued nonce inside the signed message — prevents replaying a valid signature over a different message. |

### Credit ledger races

**Problem**: two concurrent imports on a user with $5 balance, each costing $4.
Naive read-then-write: both read $5, both debit → user charged $8 on a $5 balance.

**Mitigation**: `db.credit_charge` uses a single atomic UPDATE:
```sql
UPDATE user_credits
   SET balance_usd = balance_usd - ?
 WHERE user_id = ? AND balance_usd + eps >= ?
```
The `balance_usd >= ?` predicate runs inside the statement. The second concurrent
UPDATE sees the already-debited row and its WHERE clause fails → `cursor.rowcount = 0`
→ `credit_charge` returns False → the second import is rejected before touching Moralis.

**Verified**: 5-thread concurrent test with $10 balance and five $4 charges → exactly
2 succeed. Do not refactor this back to read-then-write.

### Fake-token deposits

**Problem**: attacker deploys an ERC-20 called "USDC" on Ethereum, sends 1000 of
them to the treasury. Moralis reports it as a USDC transfer. A naive watcher
prices it as $1 each and credits the attacker $1000.

**Mitigation**: `treasury_watcher.ACCEPTED_CONTRACTS` is an explicit allowlist
of `(chain, contract_lower) → canonical_symbol`. Every ERC-20 leg is looked up by
`(chain, er.address.lower())`. If the tuple isn't in the allowlist, the leg is
skipped — no credit, no unclaimed row. The Moralis-reported `token_symbol` is
NEVER used for validation, only for display.

**Rule**: when adding a new accepted token, add it to `STABLECOIN_CONTRACTS` or
`WRAPPED_TOKEN_CONTRACTS` in `treasury_watcher.py`, and also add its decimals
to `app.py` → `_TOKEN_DECIMALS` if you want it in the in-app pay dropdown.

### Top-up idempotency

**Problem**: the watcher runs every 90s; a tx could be seen in two consecutive
windows and credited twice. Or the in-app verify endpoint could be called twice.

**Mitigation**: unique partial index `idx_credit_topup_chain_tx` on
`credit_transactions(chain, tx_hash) WHERE kind='topup'`. `credit_topup` catches
`sqlite3.IntegrityError` as a no-op replay. `wallet_payments.verify_and_credit`
also pre-checks via `db.find_credit_topup` to give a nicer UX message, but the
DB-level unique index is the authoritative defense.

### Block-confirmation / reorg protection

**Problem**: credit a tx before it has enough confirmations → chain reorgs,
tx disappears, user keeps the credit.

**Mitigation**: `MIN_AGE_SECONDS` dict per chain (eth/arb/op/base/polygon=180,
bsc=60). Both the background watcher and `wallet_payments.verify_and_credit`
reject txs whose `block_timestamp` is less than this threshold old — the UI
re-polls and the credit lands on the next pass.

### Front-running another user's top-up

**Problem**: Alice pays $100 from her wallet to the treasury. Bob sees the tx
in the mempool and submits Alice's tx_hash to `/api/billing/pay/verify` from
his own session, hoping to claim her credit.

**Mitigation**: `verify_and_credit` checks that the tx sender (from the matching
transfer leg) is a wallet linked to the requesting user. If the sender isn't
linked to the caller's user_id, the verify returns `rejected`. The background
watcher applies the same logic.

### Cross-tenant wallet_id leaks

**Problem**: any route that takes `wallet_id` as a path parameter can be called
with someone else's wallet_id.

**Mitigation**: every such route starts with `db.get_wallet(user_id, wallet_id)`
which JOINs on user_id. Returns None → 404. Never exposes the existence of
another user's wallet.

### TOCTOU on import quoting

**Problem**: user clicks "Import" on a $3 quote. Between the quote and the
charge, their wallet adds 5000 new txs. The import's actual cost is now $50 —
we'd silently debit the extra.

**Mitigation**: `ImportHistoryIn.max_cost_usd` is passed in the request body.
The quote is re-computed server-side; if it exceeds `max_cost_usd`, the route
returns 409 with the new quote instead of charging. The user sees the new
number and decides again.

### Rate limiting

Sliding window per `(bucket, user_id)`:
- `import_history` — 10/hour (the most expensive op)
- `poll_now` — 2/min (prevents Moralis quota drain via repoll spam)
- `pay_verify` — 30/hour (brute force of random tx hashes)
- `scan_wallet`, `scan_all`, `estimate_import`, `chain_detect`, `add_tx` — cheaper buckets

Fails open on internal error — logged but not blocking. Does not survive
restart — acceptable.

### Input validation

Every JSON route body is a pydantic `BaseModel` subclass with explicit
`Field(..., ge, le, min_length, max_length)` constraints. No raw dict bodies.
Length caps on user strings:
- Wallet label: 64 chars
- Transaction symbol: 32 chars
- Transaction notes: 500 chars
- Wallets per user: 50
- Total transactions per user: 100,000
- Wallet imports per wallet in 30 days: 10

### XSS

- Server-side: Jinja2 autoescape on HTML templates
- Client-side: every `innerHTML = \`...${x}...\`` interpolation of user-controlled
  `x` goes through `esc()` in `static/app.js`. That includes symbols (which can
  be anything an attacker deploys as an ERC-20), notes, labels, addresses.

### Session security

- `SessionMiddleware` with `secret_key` auto-generated on first boot to
  `session.key` (mode 600)
- `same_site="lax"` (blocks CSRF on top-level cross-origin POSTs)
- `https_only=False` — flip to True if fronted by HTTPS proxy
- 30-day max_age

## Static asset cache busting

Every `/static/*` script tag is suffixed with `?v={{ static_version }}` where
`static_version` is the latest mtime across `static/`. Prevents the scenario
where a user's browser holds a stale `app.js` (e.g., missing `esc()` after a
deploy) and pages that depend on it crash silently.

## Non-goals — explicit

Things we are NOT defending against:

- **Operator compromise** — anyone with SSH can read/modify the DB. The trust
  boundary is the VPS. Use a hardened host.
- **TLS / traffic analysis** — expected to run behind a reverse proxy with TLS.
- **DDoS** — relies on the host provider / CDN.
- **Moralis outage / censorship** — Moralis is a SPOF. We retry keys in the pool
  but don't have a fallback provider.
- **Multi-sig treasury** — single EOA treasury for now. Rotating keys is manual.
- **CSRF on cookie-only sessions** — currently rely on SameSite=Lax + JSON-only
  bodies. Token-based CSRF is in TODO.md.

## Changelog of security hardening

See `CHANGELOG.md` for the dated list of security fixes applied in this session.
Highlights:
- XSS fixes across all templates (esc() helper + wrap user values)
- Atomic credit_charge (race condition fix)
- SIWE domain binding
- Treasury contract allowlist (fake-token defense)
- Block confirmation delay (MIN_AGE_SECONDS)
- Rate limiting across 7 buckets
- Cross-tenant wallet_id ownership check
- In-app wallet payment front-running defense (sender-is-linked check)
