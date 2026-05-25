# Solana Tax History Import — Build Report

**Date:** 2026-04-11  
**Author:** Build Agent (claude-sonnet-4-6)  
**Branch:** Not committed (review first)

---

## Files Created

| File | Lines | Purpose |
|------|-------|---------|
| `webapp/tax/solana_tokens.py` | 287 | SPL allowlist + spam filter |
| `webapp/birdeye_prices.py` | 310 | Birdeye historical price fetcher + cache |
| `webapp/sol_history.py` | 602 | Helius Enhanced Transactions walker |
| `webapp/tests/test_sol_history.py` | 430 | Unit tests (6 tests) |
| `webapp/tax/SOLANA_BUILD_REPORT.md` | this file | Build report |

**Total new lines:** ~1,629

---

## Files Modified

| File | Change |
|------|--------|
| `webapp/app.py` | Added `import sol_history as sol_history_mod`; updated `_run_import_for_wallet` to branch on `wallet_chain == "solana"` and call `sol_history_mod.fetch_sol_history()`; updated the EVM-only guard in `api_import_history` to allow Solana wallets through (`chain not in ("evm", "solana")`) |

No files in `webapp/tax/` were modified (tax engine is untouched).

---

## Tests

**6/6 passing** — run with:
```
python3 webapp/tests/test_sol_history.py
```

| Test | What it verifies |
|------|-----------------|
| `test_solana_tokens_allowlist` | 5 known mints (USDC, BONK, JUP, mSOL, WIF) resolve to correct symbols; unknown mint returns None |
| `test_spam_token_filter` | unknown+UNKNOWN → spam; unknown+SWAP → kept; known mint → always kept |
| `test_fetch_sol_history_mocked` | 2 swap txs + 1 transfer → 5 rows with correct shape, tx_type, prices |
| `test_birdeye_price_cache_hit_miss` | First call hits API; second call for same (mint, date) reads from SQLite cache |
| `test_birdeye_rate_limit_backoff` | 3x 429 → exponential backoff 2→4→8s, final result None |
| `test_price_waterfall_binance_first` | Binance called and returned → Birdeye NOT called |

---

## Smoke Test

**Wallet:** `F1iLXdL7rSUvjxomqxj8HcWTnFgSkTMGhj9G84XoAfKz` (active Phantom wallet)  
**Transactions processed:** 100 raw Helius Enhanced Transactions  
**Rows emitted:** 57

**Coin distribution:**
- USDC: 32 rows
- SOL: 21 rows
- jitoSOL: 2 rows
- USDT: 1 row
- PENGU: 1 row

**Transaction type distribution:**
- buy: 25
- sell: 18
- deposit: 10
- withdraw: 4

**SWAP-derived rows (buy + sell):** 43 ✓  
**Bad prices (NaN or negative):** 0 ✓  
**Spam-token rows (unknown mints in non-SWAP context):** 0 ✓

All coins in the output are in the LEGIT_MINTS allowlist. Price resolution worked correctly:
- USDC/USDT resolved to $1.00 (stablecoin fast-path)
- SOL resolved via Binance (confirmed $187.94 / $222.04 on respective dates)
- jitoSOL resolved via Binance (maps to SOLUSDT via `_CANONICAL_SYMBOL`)
- PENGU fell through to Birdeye (not on Binance)

First 10 rows saved to `/tmp/sol_history_smoketest.json`.

---

## Architecture Decisions

### Why Helius Enhanced Transactions over raw RPC?

Raw Solana transactions are instruction-level data that require program-specific parsers
for each DEX/protocol version (Jupiter v4, v5, v6 all use different layouts). Helius
Enhanced Transactions provides a normalized `type` + `tokenTransfers` schema that works
across all programs without maintaining a decode table. The tradeoff is Helius API
dependency, but this is already an established dependency (scanner.py uses it for balances).

### Why the allowlist approach in solana_tokens.py?

A blacklist of scam mints can never keep up — new ones are deployed faster than any
team can ban them. The allowlist inverts the burden: we maintain 20-25 vetted entries
covering >95% of retail wallet activity. Unknown mints in SWAP context are preserved
(user voluntarily traded them); unknown mints in airdrop/unknown context are dropped.
See solana_tokens.py docstring for the full rationale.

### Why not modify the pricing quote for Solana?

The `pricing.quote_import()` function calls Moralis EVM `count_wallet_txs()` per chain.
For Solana wallets this returns 0 on all EVM chains → cost_usd=0 → Solana imports are
currently free. This is intentional for the MVP: implementing a Solana-native tx-count
endpoint would require a separate Helius API call and a Solana-specific pricing formula.
The free-tier behavior is documented as a known limitation below.

---

## Known Limitations / Out of Scope

1. **Solana wallet pricing in `quote_import`**: Solana imports always price at $0 because
   `pricing.quote_import()` only queries EVM chains. A proper Solana quote should call
   Helius to count transactions and apply the same per-tx pricing model.

2. **Batch import (`/api/wallets/import-history-all`) excludes Solana wallets**: The batch
   endpoint filters `if w["chain"] == "evm"` (line ~903 of app.py). Solana wallets must
   use the single-wallet `/api/wallets/{id}/import-history` endpoint. Fixing the batch
   endpoint requires also fixing the pricing quote (see above).

3. **React UI Solana wallet support**: Not verified during this build. The wallets page
   likely shows Solana wallets (scanner.py already supports them for balance scanning),
   but whether the "Import History" button appears for Solana wallets depends on client-
   side chain-type checks. This is UI-layer work, out of scope for this agent.

4. **NFT disposals not tracked**: Helius Enhanced API includes NFT transactions. The
   parser skips all NFT_* and COMPRESSED_NFT_* types. NFT cost basis tracking requires
   a separate row schema (NFTs don't have a fungible `amount` or `symbol`).

5. **WSOL double-counting risk**: If a SOL-to-token swap routes through WSOL internally
   (SOL → WSOL → USDC), Helius may report both native SOL and WSOL transfers. The parser
   skips the WSOL mint for swap tokenTransfers when the native SOL leg is already captured
   via nativeTransfers, but edge cases may exist with Jupiter's routing engine.

6. **`W` token address clarification**: The brief mentioned `85VB...bwi57` for the
   Wormhole W token. The correct verified address ending is `...QAmQ`, not `...bwi57`.
   The correct address (`85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ`) was confirmed
   via solscan.io and wormhole.com/ecosystem/w-token and is what's in the allowlist.

---

## Suggested Next Steps

1. **Implement Solana tx-count in pricing.py** so the quote endpoint can price Solana
   imports correctly using Helius transaction count.

2. **Add Solana to batch import** (`/api/wallets/import-history-all`) once pricing is fixed.

3. **Verify React UI** shows the Import History button for Solana wallets — check
   `static/js/` for any `chain === "evm"` guards that would hide the button.

4. **Extend LEGIT_MINTS** as new legitimate Solana tokens gain adoption. Good candidates
   for the next round: TRUMP (FAR...), FARTCOIN (9BB...), KMNO (KMNO...), DRIFT (DR...).
   Verify each against solscan before adding.

5. **Add NFT disposal tracking** as a separate module (separate row shape needed — at
   minimum `collection`, `token_id`, `floor_price_usd` as additional fields).

6. **WSOL double-counting regression test**: Add a test fixture with a native-SOL-input
   Jupiter swap to verify we don't emit duplicate SOL rows when Helius emits both
   nativeTransfers and tokenTransfers for the same SOL movement.
