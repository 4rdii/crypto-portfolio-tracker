# Solana Build Review

**Reviewer:** Opus review agent (fresh context)
**Build agent:** Sonnet
**Reviewed at:** 2026-04-11T14:15:46+03:00

## Summary

The build is solid in the happy path — mocked tests all pass, row shape matches
EVM, stablecoin shortcut works, price waterfall order is correct, and all 12
existing tax tests still pass. However there are three correctness issues that
I'd classify as MAJOR (one is close to BLOCKER): the `is_spam_token` filter
defined in `solana_tokens.py` is never actually invoked by `sol_history.py`, so
spam/airdrop tokens that carry a Helius metadata symbol will slip through;
pagination walks newest-first but the import truncates at `max_txs` — which
means on a large wallet you lose the OLDEST history (the cost basis) rather
than the newest, opposite of the EVM walker's behavior; and any transient
Helius 429/5xx/network error silently terminates pagination at the failing
page, losing every older page. Counts: **0 blockers, 4 majors, 6 minors,
3 nits.**

## Test results (reproduced locally)

- `tests/test_sol_history.py`: **6 passed, 0 failed**
- `tests/test_tax_methods.py`: **12 passed, 0 failed**
- Smoke-test JSON `/tmp/sol_history_smoketest.json`: 10 rows, all parseable by
  `tax/methods.py:_parse_ts`, all `price_usd` positive floats, all symbols
  (SOL, USDT, USDC) in `LEGIT_MINTS`, all `tx_type` values in
  `{buy, sell, deposit, withdraw}`. No anomalies.

## Findings

### BLOCKER (0)

None.

### MAJOR (4)

#### M1: `is_spam_token()` is defined, documented, and tested — but never called

**File:** `sol_history.py:438-489` (dispatcher) and `sol_history.py:318-378`
(`_parse_transfer`)
**Description:** The module docstring at `sol_history.py:44` states "All
unknown (not-in-allowlist) mints go through `tax/solana_tokens.is_spam_token()`."
`is_spam_token` is defined at `tax/solana_tokens.py:263` and exercised by
`tests/test_sol_history.py:87-116`. But `grep is_spam_token webapp/sol_history.py`
returns only comments — the function is never imported or called. The only
filter actually applied in `_parse_transfer` (lines 354-360) is: if
`resolve_symbol(mint)` is None, fall back to `transfer.get("symbol")` metadata
from Helius; emit a row as long as that fallback symbol is non-empty and not
the literal "UNKNOWN". This is a much weaker filter than `is_spam_token`.
**Impact:** Spam tokens with a Helius-provided symbol will pass through for
all `tx_type` values — including `AIRDROP` (which doesn't match any
`skip_prefixes` at `sol_history.py:451-452`, so it falls into the `else`
branch at `sol_history.py:473` and gets emitted as a deposit). The entire
rationale of the solana_tokens allowlist is undercut on the one path the
allowlist is supposed to catch (unvetted mints on non-voluntary tx types).
**Suggested fix:** Call `is_spam_token(mint, tx_type)` inside
`_parse_transfer` and `_parse_tx`'s `else` branch before emitting any row
for an unlisted mint, and drop the row if it returns True.

#### M2: `max_txs` truncation discards the OLDEST history on Solana (opposite of EVM)

**File:** `sol_history.py:496-561`
**Description:** `fetch_sol_history` walks Helius in reverse-chronological
order (newest → oldest) via the `before=<lastSig>` cursor (line 553). The
loop terminates when `processed >= max_txs` (default 1000). On a wallet
with 2000 real txs and the default cap, the function returns the newest
1000 and silently drops the oldest 1000. Compare `history.py:198` which
explicitly sets `"order": "ASC"` with a comment at `history.py:186-189`
saying *"so the cost-basis engine sees buys before the sells that depend
on them — and if we ever do get truncated by the ceiling, we lose the MOST
RECENT history (less harmful) rather than the OLDEST (the buys we need for
cost basis)."*
**Impact:** A Solana import that trips the `max_txs` ceiling produces the
exact failure mode the EVM walker was deliberately designed to avoid: the
buys that provide cost basis for retained sells are discarded, and the
tax engine emits zero-basis disposals for every sell in the newest 1000
txs. On an active wallet this silently inflates reported gains.
**Suggested fix:** Either raise `max_txs` substantially, or walk Helius
completely and reverse-insert, or (best) walk oldest-first using a
`before` scan to the tail then replay forward. At minimum, log an
explicit `hit max_txs ceiling — oldest history may be missing, cost
basis may be incomplete` warning matching the EVM walker's warning at
`history.py:220-221`.

#### M3: Transient Helius errors silently terminate pagination

**File:** `sol_history.py:201-223` (`_helius_page`) and `sol_history.py:531-555`
(pagination loop)
**Description:** `_helius_page` returns `[]` on every non-200 response —
network errors (line 203-205), 429 (207-210), any other status (212-214),
non-JSON (216-220). The caller loop at line 533 reads `if not batch:
break`. So a single 429 / 5xx / network blip on page 3 of a 10-page
import silently ends the walk, and pages 4-10 are lost. The 429 path
waits 5 seconds and then still returns `[]`, so the backoff doesn't
retry — it just delays the termination.
**Impact:** Realistic Helius behavior under load is an occasional 429 or
502; one transient failure will truncate an import. Unlike M2 this
affects even small wallets and cannot be mitigated by raising any
config knob. Combined with M2, a single blip on the middle page causes
the worst possible loss (newest N txs retained, older history
discarded). User has no way to tell the import was partial — the
function logs `"Done: processed N txs"` even when N is truncated by
error.
**Suggested fix:** Distinguish end-of-history (`[]` after 200 OK) from
error (`None` / raise) in `_helius_page`. In the caller, retry 429 /
5xx with backoff and re-raise on persistent failure instead of silently
breaking. Aborting the import with a clear error is strictly better
than a silent truncation that corrupts tax numbers.

#### M4: No test covers the self-transfer skip at `sol_history.py:347-348`

**File:** `tests/test_sol_history.py:123-246` (`test_fetch_sol_history_mocked`)
**Description:** The fixture contains two swaps and one external
transfer; none of them exercise the same-wallet-both-sides code path.
The skip at `sol_history.py:347-348` (`if from_acc == wallet_lower and
to_acc == wallet_lower: continue`) and the equivalent at
`sol_history.py:416-417` in `_parse_native_transfers` are therefore
untested. The build brief explicitly called this path out as "skip at
row-emission time, not at tax layer" — and the tax layer's
self-transfer collapser at `tax/methods.py` operates on EVM tx-hash
pairs, which Solana rows don't carry (no `tx_hash` key in emitted
rows), so the tax-layer fallback won't catch Solana self-transfers
either.
**Impact:** If a bug ever slips into the direction check (e.g. a
refactor that changes the `from_acc == wallet_lower and to_acc ==
wallet_lower` condition), nothing will flag it, and Solana consolidation
txs would leak into the cost basis engine as pairs of deposit+withdraw
rows that don't match.
**Suggested fix:** Add a fixture tx where both `fromUserAccount` and
`toUserAccount` equal WALLET, assert it contributes zero rows to the
return value. Test the same for `nativeTransfers`.

### MINOR (6)

#### m1: `_parse_tx` UNKNOWN branch lacks a dedicated test

**File:** `sol_history.py:473-487` vs `tests/test_sol_history.py`
**Description:** The `else` branch at `sol_history.py:473` (and its
fallback from `_parse_transfer` to `_parse_native_transfers`) has no
mocked fixture in `test_fetch_sol_history_mocked`. This is the branch
that's supposed to invoke the spam filter (see M1), so it's also the
branch most likely to regress.
**Impact:** Low in the short term; M1 is the primary issue here.
**Suggested fix:** Add a fixture with `tx_type="UNKNOWN"` + an
unlisted mint with a Helius-provided symbol + assert the row is
dropped once M1 is fixed.

#### m2: Solana address comparison lowercases both sides

**File:** `sol_history.py:267, 276-277, 334, 343-344, 398, 413-414`
**Description:** Solana base58 pubkeys are case-sensitive — unlike
EVM 0x addresses. Lowercasing before comparing (`wallet_lower =
wallet.lower()` and `from_acc = (...).lower()`) can in theory cause a
false positive match between two distinct addresses that differ only
in case. In practice this is astronomically unlikely for randomly
generated keypairs, and the code is almost certainly a case-insensitive
workaround for user-typed addresses stored in the DB with varying case.
**Impact:** Near-zero in practice but the pattern is wrong and
inconsistent with how other Solana tooling treats pubkeys.
**Suggested fix:** Normalize once on wallet registration (store the
canonical form Helius returns) and compare exactly. Failing that, add
a comment explaining why lowercasing is deliberate.

#### m3: Birdeye `_cached` cache key embeds date twice

**File:** `birdeye_prices.py:121-142`
**Description:** `_cache_key(mint, date)` returns `f"sol:{mint}:{date}"`,
then the SELECT at line 138-141 queries `WHERE symbol = ? AND date = ?`
with `(key, date)`. The date appears inside the symbol value AND as a
separate column — functionally equivalent to querying by just the
symbol column since two rows can never share the same `sol:MINT:DATE`
across different `date` values. Not incorrect, but redundant and
obscures intent.
**Impact:** None — works correctly; slightly wasteful on storage.
**Suggested fix:** Either drop the date from the key (keep it only in
the date column) or drop the date column from the predicate. Pick one.

#### m4: Swap leg using native SOL emits `withdraw`, not `sell`

**File:** `sol_history.py:421, 458-465`
**Description:** When a Jupiter swap routes through native SOL and
Helius only puts the SOL movement in `nativeTransfers` (not
`tokenTransfers`), `_parse_tx` calls `_parse_native_transfers` which
emits the SOL leg as `tx_type="withdraw"` rather than `"sell"`.
Mathematically both are in `SELL_TYPES` so the tax engine processes
them identically, but semantically a withdraw-during-swap is
misleading in the UI and in notes (`notes` is `"sol-transfer · ..."`
instead of `"swap · ..."`).
**Impact:** Cosmetic — tax numbers are correct but the UI will show
a "withdraw" for what the user remembers as a swap.
**Suggested fix:** When `_parse_native_transfers` is called from the
SWAP branch, pass a flag that rewrites `tx_type` to `sell`/`buy` and
`notes` to `"swap · ..."`.

#### m5: Birdeye cache stores no record of failed lookups → repeat attempts on every import

**File:** `birdeye_prices.py:145-156`
**Description:** `_store` only writes positive prices. A `None` / `0`
result from Birdeye (dead token, thin candles) is left uncached. The
comment at line 146-148 acknowledges this as deliberate but it means
re-importing the same wallet re-queries Birdeye for every permanently-
unpriced row on every import. Combined with a wallet that has many
unprice-able mints, this burns quota.
**Impact:** Quota burn on bulk re-imports. Not a correctness bug.
**Suggested fix:** Negative-cache with a shorter TTL (e.g. 24h) so
transient thin-book misses can recover but permanent-dead mints stop
hammering Birdeye.

#### m6: No Solana address validation before URL construction

**File:** `sol_history.py:196`
**Description:** `url = f"{_HELIUS_BASE}/{address}/transactions/"`
f-strings a user-supplied address directly into the URL path. No
base58 / length / character-class check. A malicious wallet-create
request could attempt path traversal (`../transactions/`), though
the attack surface is limited because Helius would reject anything
non-pubkey-shaped and the request still targets api.helius.xyz.
**Impact:** Low — no SSRF, limited path games.
**Suggested fix:** Reject addresses that aren't 32-44 base58 chars
(`^[1-9A-HJ-NP-Za-km-z]{32,44}$`) at the top of `fetch_sol_history`.

### NIT (3)

#### n1: `_import_price_modules` is called inside every `_resolve_price` invocation

**File:** `sol_history.py:121, 140`
**Description:** Python caches imports after the first call, so this
is ~free, but it's still three `import` lookups per price resolution.
**Suggested fix:** Cache the tuple at module level on first call.

#### n2: CLI entrypoint's docstring example uses a non-placeholder address

**File:** `sol_history.py:575`
**Description:** The example `F1iLXdL7rSUvjxomqxj8HcWTnFgSkTMGhj9G84XoAfKz`
is a real test wallet (matches the one in the test file). Harmless
but a placeholder like `<address>` would be clearer documentation.

#### n3: `tests/test_sol_history.py` imports `unittest` but never uses it

**File:** `tests/test_sol_history.py:20`
**Description:** `import unittest` is unused — the test file uses
plain functions + `__main__` runner matching `test_tax_methods.py`'s
style (which is correct). Just dead import.
**Suggested fix:** Delete the line.

## Positive observations

- Row shape is a clean match with EVM rows (`ts`, `tx_type`, `symbol`,
  `amount`, `price_usd`, `notes`, plus nullable `wallet_id`/`user_id`
  the caller fills). Verified against `history.py:268-279, 299-316` and
  the `db.add_transaction` signature at `db.py:509-521`. The `ts` format
  matches EVM (`datetime.isoformat()` on a UTC-aware datetime) and
  parses cleanly through `tax/methods.py:_parse_ts`.
- Price waterfall order is correct: stablecoin shortcut first
  (`sol_history.py:146-147`), then Binance
  (`sol_history.py:150-155`), then Birdeye (`sol_history.py:158-163`),
  then 0.0 with a warning. The shortcut avoids burning Birdeye quota on
  known stables. `test_price_waterfall_binance_first` verifies Birdeye
  is not called when Binance hits.
- Tax engine compatibility: `sell`/`buy`/`deposit`/`withdraw` are all in
  `BUY_TYPES`/`SELL_TYPES` at `tax/methods.py:41-42`, so the Solana
  rows walk correctly through the cost basis engine without any walker
  changes.
- `binance_historical_price(symbol, date)` at `binance_prices.py:501`
  exists with the exact signature `sol_history.py:151` calls — no
  AttributeError at runtime.
- Security: no API keys are logged. Helius URL key is passed via
  `params` dict (not f-stringed), and log lines only emit status code +
  address, never `r.url`/`r.text`. Birdeye error paths log only status
  codes. All SQLite operations use parameterized queries.
- No new third-party dependencies — `sol_history.py` and
  `birdeye_prices.py` use only stdlib + `requests` (already a
  transitive dep). `grep '^import\|^from'` confirms this.
- Env-loading pattern in `birdeye_prices.py:54-73` and
  `sol_history.py:66-85` mirrors `scanner.py:19-38` exactly.
- All 12 existing tax tests still pass — no regression in the walker.

## Mint verification spot-checks

All three brief-specified canonical mints match exactly:

- **SOL** (Wrapped SOL, `sol_history.py` + `solana_tokens.py:61`):
  `So11111111111111111111111111111111111111112` — verified against the
  canonical Wrapped SOL mint universally used by SPL tooling.
- **USDC** (`solana_tokens.py:69`):
  `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` — Circle's USDC on
  Solana; matches Solscan / Phantom registry.
- **USDT** (`solana_tokens.py:75`):
  `Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB` — Tether on Solana;
  matches Solscan / Phantom registry.

Additional spot-checks from domain knowledge:

- **BONK** `DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263` — correct.
- **WIF** `EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm` — correct.
- **PYTH** `HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3` — correct.
- **JTO** `jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL` — correct.
- **mSOL** `mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So` — correct.

I cannot perform a live web-fetch verification for all 22 entries;
anything not listed above was not independently re-verified in this
review and should be re-checked before mainnet mass-import use.
The addresses look structurally correct (base58, start with the
vanity prefixes expected for each token) and the inline comments
cite the correct solscan.io URLs for each.

## Merge recommendation

**CONDITIONAL** — fix M1 (spam filter never called), M2 (max_txs
truncation direction), and M3 (pagination swallows transient errors)
before merge. Those three are the real bugs. M4 (missing self-transfer
test) and the minors can land as a follow-up, though I'd strongly
prefer M4 to go in with the same PR.
