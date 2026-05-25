# Billing & Credit Ledger

## Overview

Paid operations (currently only history imports) are funded by a per-user
credit balance denominated in USD. Users top up by sending any accepted
token to a shared treasury address — either through the in-app wallet flow
(primary) or a manual transfer that the background watcher picks up (fallback).

## Data model

### `user_credits`
One row per user, materialized balance:
```sql
CREATE TABLE user_credits (
    user_id INTEGER PRIMARY KEY,
    balance_usd REAL NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### `credit_transactions`
Append-only ledger. Every balance change has a row here:
```sql
CREATE TABLE credit_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    delta_usd REAL NOT NULL,          -- positive for topup/refund, negative for charge
    kind TEXT NOT NULL,               -- 'topup' | 'charge' | 'refund'
    chain TEXT,                       -- only set for topups
    tx_hash TEXT,                     -- only set for topups
    from_address TEXT,                -- only set for topups
    token_symbol TEXT,                -- only set for topups
    token_amount REAL,                -- only set for topups
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX idx_credit_topup_chain_tx
    ON credit_transactions(chain, tx_hash) WHERE kind = 'topup';
```

The partial unique index on `(chain, tx_hash) WHERE kind='topup'` is what
makes top-ups idempotent — a replayed tx hash triggers `IntegrityError`
and becomes a no-op.

## Ledger operations

### `db.credit_topup(user_id, delta_usd, chain, tx_hash, from_address, token_symbol, token_amount, notes)`
- INSERT into `credit_transactions` with `kind='topup'`
- On `IntegrityError` (duplicate chain+tx_hash): return False (idempotent no-op)
- Otherwise call `_apply_credit_delta(c, user_id, delta_usd)` which upserts
  `user_credits` and bumps the balance
- Logs `credit.topup user=... amount=... chain=... tx=... token=...`

### `db.credit_charge(user_id, amount_usd, notes)`
- **Atomic conditional UPDATE**:
  ```sql
  UPDATE user_credits
     SET balance_usd = balance_usd - ?
   WHERE user_id = ? AND balance_usd + 1e-9 >= ?
  ```
- If `rowcount == 0`: insufficient balance, log warning, return False
- Otherwise INSERT the ledger row, log info, return True

This pattern is the defense against concurrent-charge races. See
`SECURITY.md` for the threat model.

### `db.credit_refund(user_id, amount_usd, notes)`
- INSERT positive-delta ledger row with `kind='refund'`
- `_apply_credit_delta` bumps the balance back up
- Called when a mid-flight import fails and we want to give the user their money back

### `db.find_credit_topup(chain, tx_hash)`
Lookup helper for the in-app verify endpoint. Returns the existing topup
row (if any) so the endpoint can short-circuit the Moralis round-trip
on an already-credited hash.

## Pricing

`pricing.quote_import(wallet)` returns the USD cost estimate for a full
history import, based on:
- `PRICE_PER_TX` (currently $0.001/tx)
- `MIN_CHARGE` (currently $0.10/import)
- `FREE_TX_ALLOWANCE` (currently 100 free txs per wallet on first import)
- `DELTA_MULTIPLIER` (re-imports charge 2× the new-tx delta)

Live operations (scanning current holdings, price refresh, P&L) are free.

## Payment flows

### Primary: in-app wallet pay

1. User visits `/billing`
2. Client fetches `/api/billing/pay/tokens` → list of chains + stablecoin
   contracts + decimals + EIP-155 chain IDs
3. User picks network + token + amount, clicks Pay
4. Client flow (in `billing.html`):
   ```
   ethers.BrowserProvider(window.ethereum)
   → eth_requestAccounts
   → wallet_switchEthereumChain(targetChainId)
   → new ethers.Contract(contract, ['function transfer(...)'], signer)
   → erc20.transfer(treasuryAddress, parseUnits(amountStr, decimals))
   → tx.wait(1)     // wait for 1 confirmation
   → POST /api/billing/pay/verify { chain, tx_hash, expected_token, expected_usd }
   ```
5. `wallet_payments.verify_and_credit`:
   - Validates tx_hash format + chain against supported list
   - Rate-limit: 30/hour per user
   - `db.find_credit_topup(chain, tx_hash)` → idempotency short-circuit
   - Fetches tx via Moralis `/transaction/{tx_hash}`
   - Maturity check: `tx age < MIN_AGE_SECONDS[chain]` → status "pending", client retries every 20s
   - Receipt status check: reverted → status "rejected"
   - Transfer leg matching: iterate `native_transfers` then `erc20_transfers`,
     find the one whose `to_address == treasury`. For ERC-20 legs, the
     `(chain, contract_lower)` tuple MUST be in `ACCEPTED_CONTRACTS` —
     otherwise the leg is skipped (contract allowlist defense)
   - Price: stables = $1; everything else = `moralis_historical_price` at
     the tx's block_number
   - Amount tolerance: the on-chain USD value must be within 1% or $0.01
     of the `expected_usd` the client submitted. Otherwise rejected.
   - Sender check: `db.find_user_by_wallet_address(from)` MUST equal the
     caller's `user_id`. Otherwise rejected (front-running defense).
   - Calls `db.credit_topup` — unique index is the authoritative idempotency
6. Client polls verify every 20s for up to 4 minutes while the status
   remains "pending", shows success/reject/reason in the UI

Why this is the PRIMARY path:
- Instant feedback — user doesn't wait ~90s for a watcher cycle
- Surfaces specific errors (wrong chain, amount mismatch) at the moment
  of payment, not asynchronously
- Works with any EIP-1193 wallet the user already connected for sign-in

### Fallback: treasury watcher

1. `treasury_watcher._loop` wakes every 90s (or earlier via `request_poll`)
2. For each EVM chain, fetches the treasury wallet's recent history via
   Moralis `/wallet/{addr}/history`
3. Same maturity / allowlist / amount / sender checks as the primary path
4. Deposits from a linked wallet → `credit_topup` with the matched user_id
5. Deposits from an unknown wallet → logged as unclaimed (operator resolves
   manually — admin view is in TODO.md)

The watcher is the only thing between a user who pasted the treasury
address into their own tool and a lost deposit. Keep it running.

## Operator knobs

- Change the treasury address: set `TREASURY_ADDRESS` env var and restart
- Add an accepted token: edit `STABLECOIN_CONTRACTS` / `WRAPPED_TOKEN_CONTRACTS`
  in `treasury_watcher.py`; if you want it in the in-app pay dropdown also
  add decimals to `app.py → _TOKEN_DECIMALS`
- Change tolerance: `AMOUNT_TOLERANCE_PCT` / `AMOUNT_TOLERANCE_ABS` in `wallet_payments.py`
- Change confirmation delay: `MIN_AGE_SECONDS` in `treasury_watcher.py`
- Change poll frequency: the sleep in `treasury_watcher._loop`
- Change pricing: `pricing.PRICE_PER_TX`, `MIN_CHARGE`, `FREE_TX_ALLOWANCE`, `DELTA_MULTIPLIER`

## Edge cases + how they're handled

| Edge case | Handling |
|---|---|
| User pays then closes tab before verify lands | Background watcher picks it up on the next cycle |
| User pays twice with the same tx hash | Unique index on `(chain, tx_hash)` → second call is a no-op; UI shows "already credited" |
| Tx reverts on-chain | `receipt_status == 0` → rejected, no credit |
| Tx exists but Moralis doesn't know yet | status "pending" → client retries |
| Fake stablecoin with "USDC" symbol | Contract allowlist lookup fails → leg skipped → rejected |
| User pays from an unlinked wallet | Watcher logs it as unclaimed; verify endpoint rejects |
| User's import racing with a `poll_now` nudge | Credit lands via whichever path wins; the loser sees `IntegrityError` and returns "already credited" |
| User tries to submit someone else's tx hash | Sender-is-linked check rejects; that tx credits the real owner on the background pass |

## Testing

See `tests/test_credit_ledger.py` (TODO) for:
- `test_credit_topup_idempotent` — same hash twice, balance only moves once
- `test_credit_charge_race` — 5 concurrent $4 charges on a $10 balance, exactly 2 succeed
- `test_credit_refund_after_failed_import` — import raises, refund lands, ledger balanced
- `test_find_credit_topup_case_insensitive` — tx hash lookup is case-insensitive
