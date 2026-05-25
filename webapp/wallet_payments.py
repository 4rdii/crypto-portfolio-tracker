"""In-app wallet-signed top-ups.

User flow:
  1. User clicks "Top up $N" on the billing page.
  2. Client builds an ERC-20 transfer (or native send) to TREASURY_ADDRESS
     for exactly the requested amount + token, and opens the wallet to sign.
  3. Wallet broadcasts the transaction and returns the tx hash to the page.
  4. Page POSTs {chain, tx_hash, expected_token, expected_amount} to
     /api/billing/pay/verify and we look that specific tx up on Moralis.
  5. If it's confirmed, matches the treasury recipient, matches the
     expected token+amount within tolerance, and matches a linked wallet
     as the sender → we credit the user on the spot.

This is purely additive: the background treasury_watcher stays as a
fallback for deposits that weren't initiated through the in-app flow
(unsolicited donations, users pasting our treasury address into another
tool, etc).

Why a dedicated endpoint instead of relying on the watcher?
  • The user expects instant feedback, not a 90-second poll.
  • We can surface specific errors (wrong chain, wrong amount, pending)
    at the moment of payment, not asynchronously.
  • Contract allowlist still applies: even a verified on-chain tx for a
    fake ERC-20 named "USDC" gets rejected because its contract isn't
    canonical.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException

import db
from applog import log
from scanner import _moralis_get
from treasury_watcher import (
    ACCEPTED_CONTRACTS,
    NATIVE_SYMBOL,
    STABLE_SYMBOLS,
    TREASURY_ADDRESS,
    MIN_AGE_SECONDS,
)


# How close to the expected amount the on-chain transfer has to land.
# Users may send $10.00 flat when prompted, but gas estimation or slippage
# (for swap-to-pay tools) can leave a few cents on the floor. 1% tolerance
# with a 1-cent floor keeps small top-ups from bouncing on rounding.
AMOUNT_TOLERANCE_PCT = 0.01   # 1%
AMOUNT_TOLERANCE_ABS = 0.01   # $0.01


def _fetch_tx(chain: str, tx_hash: str) -> dict | None:
    """Ask Moralis for a single tx by hash. Returns the parsed dict or None.

    Uses the v2.2 transaction endpoint which returns the same shape as
    the wallet-history rows (native_transfers + erc20_transfers + meta).
    Adding include=internal_transactions would be useful for swap-router
    paths but isn't required for the simple transfer case we care about.
    """
    try:
        r = _moralis_get(
            f"https://deep-index.moralis.io/api/v2.2/transaction/{tx_hash}",
            params={"chain": chain, "include": "internal_transactions"},
            timeout=30,
        )
        if r is None or r.status_code != 200:
            return None
        return r.json() or None
    except Exception as e:
        log.error("wallet_pay.fetch_tx_error chain=%s tx=%s err=%s",
                  chain, tx_hash[:12], e)
        return None


def _tx_age_seconds(tx: dict) -> float | None:
    ts_str = tx.get("block_timestamp") or ""
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds()


def _amount_within_tolerance(actual: float, expected: float) -> bool:
    if expected <= 0:
        return False
    diff = abs(actual - expected)
    pct = diff / expected
    return diff <= AMOUNT_TOLERANCE_ABS or pct <= AMOUNT_TOLERANCE_PCT


def _rejected(reason: str) -> dict:
    """Shorthand for a 'rejected'/'pending'-shape dict (fields all zeroed)."""
    return {
        "status": "rejected",
        "reason": reason,
        "matched_usd": 0.0,
        "matched_from": "",
        "matched_symbol": "",
        "matched_amount": 0.0,
    }


def verify_payment_for_import(
    user_id: int,
    chain: str,
    tx_hash: str,
    expected_token: str,
    expected_usd: float,
) -> dict:
    """Verify a user-signed payment tx for a pay-per-import run.

    Same on-chain checks as `verify_and_credit` (recipient = treasury,
    token on allowlist, amount matches, sender is a linked wallet,
    confirmation depth met), but DOES NOT touch user_credits — there is
    no prepaid balance in the pay-per-import model. The caller is
    responsible for logging the payment via `db.record_import_payment`
    and running the import only if this function returns status='ok'.

    Returns a dict:
        {"status": "ok"|"pending"|"rejected"|"already_used",
         "reason": "...",
         "matched_usd": float,        # on-chain USD value of the transfer
         "matched_from": str,         # sender address (lowercase)
         "matched_symbol": str,       # canonical token symbol
         "matched_amount": float}     # raw token amount
    """
    tx_hash = (tx_hash or "").strip().lower()
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise HTTPException(400, "Invalid tx hash format")
    chain = (chain or "").strip().lower()
    if chain not in {"eth", "arbitrum", "optimism", "base", "polygon", "bsc"}:
        raise HTTPException(400, "Unsupported chain")

    # Replay guard: a given on-chain tx can only pay for one import.
    if db.find_import_payment(chain, tx_hash) is not None:
        return {
            "status": "already_used",
            "reason": "This payment has already been used for an import.",
            "matched_usd": 0.0,
            "matched_from": "",
            "matched_symbol": "",
            "matched_amount": 0.0,
        }
    # Cross-bucket replay guard: also reject if this tx was already used
    # to unlock the tax feature for ANY user. Otherwise a user could get
    # both a tax unlock and a paid import from the same payment.
    if db.get_tax_unlock_by_tx(chain, tx_hash) is not None:
        return {
            "status": "already_used",
            "reason": "This payment has already been used to unlock tax reports.",
            "matched_usd": 0.0,
            "matched_from": "",
            "matched_symbol": "",
            "matched_amount": 0.0,
        }

    tx = _fetch_tx(chain, tx_hash)
    if tx is None:
        return {
            "status": "pending",
            "reason": "Transaction not yet indexed. Retry in a moment.",
            "matched_usd": 0.0, "matched_from": "",
            "matched_symbol": "", "matched_amount": 0.0,
        }

    age = _tx_age_seconds(tx)
    min_age = MIN_AGE_SECONDS.get(chain, 180)
    if age is None or age < min_age:
        return {
            "status": "pending",
            "reason": f"Waiting for confirmations ({int(age or 0)}s / {min_age}s).",
            "matched_usd": 0.0, "matched_from": "",
            "matched_symbol": "", "matched_amount": 0.0,
        }

    if tx.get("receipt_status") in ("0", 0):
        return {
            "status": "rejected",
            "reason": "Transaction reverted on-chain.",
            "matched_usd": 0.0, "matched_from": "",
            "matched_symbol": "", "matched_amount": 0.0,
        }

    treasury_low = TREASURY_ADDRESS.lower()
    expected_token_u = (expected_token or "").upper()
    matched_amount_token: float | None = None
    matched_symbol: str | None = None
    matched_from: str | None = None
    matched_usd: float | None = None
    block_number = str(tx.get("block_number") or "")

    # Native transfer leg
    for nt in (tx.get("native_transfers") or []):
        if (nt.get("to_address") or "").lower() != treasury_low:
            continue
        amt = float(nt.get("value_formatted") or 0)
        if amt <= 0:
            continue
        sym = NATIVE_SYMBOL.get(chain, "ETH")
        if expected_token_u and expected_token_u != sym:
            continue
        from treasury_watcher import _price_native
        price = _price_native(chain, sym, block_number)
        usd = amt * price
        matched_amount_token, matched_symbol = amt, sym
        matched_from = (nt.get("from_address") or "").lower()
        matched_usd = usd
        break

    # ERC-20 transfer leg (contract allowlist)
    if matched_amount_token is None:
        for er in (tx.get("erc20_transfers") or []):
            if (er.get("to_address") or "").lower() != treasury_low:
                continue
            contract = (er.get("address") or "").lower()
            canonical = ACCEPTED_CONTRACTS.get((chain, contract))
            if canonical is None:
                continue
            if expected_token_u and expected_token_u != canonical:
                continue
            amt = float(er.get("value_formatted") or 0)
            if amt <= 0:
                continue
            if canonical in STABLE_SYMBOLS:
                usd = amt * 1.0
            else:
                from history import moralis_historical_price
                price = float(moralis_historical_price(contract, chain, block_number) or 0)
                usd = amt * price
            matched_amount_token, matched_symbol = amt, canonical
            matched_from = (er.get("from_address") or "").lower()
            matched_usd = usd
            break

    if matched_amount_token is None or matched_usd is None:
        return {
            "status": "rejected",
            "reason": "No transfer to the treasury found in this transaction.",
            "matched_usd": 0.0, "matched_from": "",
            "matched_symbol": "", "matched_amount": 0.0,
        }

    if matched_usd <= 0:
        return {
            "status": "rejected",
            "reason": "Could not price this payment. Contact support.",
            "matched_usd": 0.0, "matched_from": "",
            "matched_symbol": "", "matched_amount": 0.0,
        }

    # The on-chain USD must be at least the expected amount (minus tolerance).
    # Overpayment is fine — we don't refund the excess.
    if expected_usd > 0 and matched_usd + AMOUNT_TOLERANCE_ABS < expected_usd \
            and matched_usd < expected_usd * (1 - AMOUNT_TOLERANCE_PCT):
        return {
            "status": "rejected",
            "reason": (f"Amount too low: sent ${matched_usd:.2f}, "
                       f"expected at least ${expected_usd:.2f}."),
            "matched_usd": matched_usd,
            "matched_from": matched_from or "",
            "matched_symbol": matched_symbol or "",
            "matched_amount": matched_amount_token,
        }

    # Sender must be a linked wallet of THIS user — prevents someone else's
    # treasury deposit from being claimed as an import payment.
    sender_user_id = db.find_user_by_wallet_address(matched_from or "")
    if sender_user_id != user_id:
        return {
            "status": "rejected",
            "reason": ("Sender wallet is not linked to your account. "
                       "Link the wallet first and retry."),
            "matched_usd": matched_usd,
            "matched_from": matched_from or "",
            "matched_symbol": matched_symbol or "",
            "matched_amount": matched_amount_token,
        }

    log.info("wallet_pay.import_verified user=%s amount=%.4f tx=%s",
             user_id, matched_usd, tx_hash[:12])
    return {
        "status": "ok",
        "reason": "Payment verified",
        "matched_usd": matched_usd,
        "matched_from": matched_from or "",
        "matched_symbol": matched_symbol or expected_token_u,
        "matched_amount": matched_amount_token,
    }


def verify_and_credit(
    user_id: int,
    chain: str,
    tx_hash: str,
    expected_token: str,
    expected_usd: float,
) -> dict:
    """Verify a user-submitted tx hash and credit them on success.

    Returns a dict shaped like:
        {"status": "credited"|"pending"|"rejected", "reason": "...",
         "amount_usd": float, "balance_usd": float}

    Raises HTTPException on client errors (400) or internal failures (500).
    Never raises for "tx exists but doesn't qualify" — those return a
    structured "rejected" status so the UI can show a friendly error.
    """
    tx_hash = (tx_hash or "").strip().lower()
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise HTTPException(400, "Invalid tx hash format")
    chain = (chain or "").strip().lower()
    if chain not in {"eth", "arbitrum", "optimism", "base", "polygon", "bsc"}:
        raise HTTPException(400, "Unsupported chain")

    # Idempotency: if we already credited this exact tx, short-circuit.
    # The credit ledger's (chain, tx_hash) unique index enforces this
    # invariant at the DB level too, but checking here gives the UI a
    # nicer message than a generic "already credited".
    existing = db.find_credit_topup(chain, tx_hash)
    if existing:
        log.info("wallet_pay.already_credited user=%s tx=%s", user_id, tx_hash[:12])
        return {
            "status": "credited",
            "reason": "Already credited (idempotent replay)",
            "amount_usd": float(existing["delta_usd"]),
            "balance_usd": db.get_credit_balance(user_id),
        }

    tx = _fetch_tx(chain, tx_hash)
    if tx is None:
        # Either Moralis doesn't know about this tx yet (too new to be
        # indexed) or the hash is wrong. Tell the client to retry in a
        # few seconds instead of giving up.
        return {
            "status": "pending",
            "reason": "Transaction not yet indexed. Retry in a moment.",
            "amount_usd": 0.0,
            "balance_usd": db.get_credit_balance(user_id),
        }

    # Confirmation / reorg protection — same delay the background watcher uses.
    age = _tx_age_seconds(tx)
    min_age = MIN_AGE_SECONDS.get(chain, 180)
    if age is None or age < min_age:
        return {
            "status": "pending",
            "reason": f"Waiting for confirmations ({int(age or 0)}s / {min_age}s).",
            "amount_usd": 0.0,
            "balance_usd": db.get_credit_balance(user_id),
        }

    # Moralis flags reverted txs — never credit one.
    if tx.get("receipt_status") in ("0", 0):
        return {
            "status": "rejected",
            "reason": "Transaction reverted on-chain.",
            "amount_usd": 0.0,
            "balance_usd": db.get_credit_balance(user_id),
        }

    # Find the transfer leg that lands on the treasury and get the sender
    # address for the matching-wallet check.
    treasury_low = TREASURY_ADDRESS.lower()
    expected_token_u = (expected_token or "").upper()
    matched_amount_token: float | None = None
    matched_symbol: str | None = None
    matched_from: str | None = None
    matched_usd: float | None = None
    block_number = str(tx.get("block_number") or "")

    # Case 1: native transfer (ETH/MATIC/BNB). Protocol-safe, no contract.
    for nt in (tx.get("native_transfers") or []):
        if (nt.get("to_address") or "").lower() != treasury_low:
            continue
        amt = float(nt.get("value_formatted") or 0)
        if amt <= 0:
            continue
        sym = NATIVE_SYMBOL.get(chain, "ETH")
        if expected_token_u and expected_token_u != sym:
            continue
        # Price natives via the same route the watcher uses
        from treasury_watcher import _price_native
        price = _price_native(chain, sym, block_number)
        usd = amt * price
        matched_amount_token, matched_symbol = amt, sym
        matched_from = (nt.get("from_address") or "").lower()
        matched_usd = usd
        break

    # Case 2: ERC-20 transfer against the canonical contract allowlist.
    # Same defense as the background watcher: the token_symbol field from
    # Moralis is NEVER trusted — only the (chain, contract) tuple is.
    if matched_amount_token is None:
        for er in (tx.get("erc20_transfers") or []):
            if (er.get("to_address") or "").lower() != treasury_low:
                continue
            contract = (er.get("address") or "").lower()
            canonical = ACCEPTED_CONTRACTS.get((chain, contract))
            if canonical is None:
                continue
            if expected_token_u and expected_token_u != canonical:
                continue
            amt = float(er.get("value_formatted") or 0)
            if amt <= 0:
                continue
            # Stables are $1 by definition; everything else prices via Moralis.
            if canonical in STABLE_SYMBOLS:
                usd = amt * 1.0
            else:
                from history import moralis_historical_price
                price = float(moralis_historical_price(contract, chain, block_number) or 0)
                usd = amt * price
            matched_amount_token, matched_symbol = amt, canonical
            matched_from = (er.get("from_address") or "").lower()
            matched_usd = usd
            break

    if matched_amount_token is None or matched_usd is None:
        return {
            "status": "rejected",
            "reason": "No transfer to the treasury found in this transaction.",
            "amount_usd": 0.0,
            "balance_usd": db.get_credit_balance(user_id),
        }

    if matched_usd <= 0:
        return {
            "status": "rejected",
            "reason": "Could not price this deposit. Contact support.",
            "amount_usd": 0.0,
            "balance_usd": db.get_credit_balance(user_id),
        }

    # Amount sanity check: the on-chain USD value must be close to what
    # the user told us they were paying. Blocks a scenario where a client
    # bug (or a curious user) submits a $1 tx hash and tries to claim a
    # $100 credit.
    if expected_usd > 0 and not _amount_within_tolerance(matched_usd, expected_usd):
        return {
            "status": "rejected",
            "reason": (f"Amount mismatch: sent ${matched_usd:.2f}, "
                       f"expected ${expected_usd:.2f}."),
            "amount_usd": matched_usd,
            "balance_usd": db.get_credit_balance(user_id),
        }

    # Sender must be one of this user's linked wallets. Otherwise anyone
    # could front-run another user's payment by submitting the hash first.
    sender_user_id = db.find_user_by_wallet_address(matched_from or "")
    if sender_user_id != user_id:
        return {
            "status": "rejected",
            "reason": ("Sender wallet is not linked to your account. "
                       "Link the wallet first (or the background watcher "
                       "will pick it up and log it as unclaimed)."),
            "amount_usd": matched_usd,
            "balance_usd": db.get_credit_balance(user_id),
        }

    # All checks passed — credit the user. The unique (chain, tx_hash)
    # index prevents double-credit if this endpoint is called twice in
    # quick succession; credit_topup returns False on the replay.
    did = db.credit_topup(
        user_id=user_id,
        delta_usd=matched_usd,
        chain=chain,
        tx_hash=tx_hash,
        from_address=matched_from or "",
        token_symbol=matched_symbol or expected_token_u,
        token_amount=matched_amount_token,
        notes=f"Wallet top-up · {matched_amount_token} {matched_symbol}",
    )
    if not did:
        # Someone (probably the background watcher) beat us to it between
        # the early idempotency check and the insert. Still a success.
        log.info("wallet_pay.raced user=%s tx=%s", user_id, tx_hash[:12])
        return {
            "status": "credited",
            "reason": "Already credited (background watcher)",
            "amount_usd": matched_usd,
            "balance_usd": db.get_credit_balance(user_id),
        }

    log.info("wallet_pay.credited user=%s amount=%.4f tx=%s",
             user_id, matched_usd, tx_hash[:12])
    return {
        "status": "credited",
        "reason": "Top-up successful",
        "amount_usd": matched_usd,
        "balance_usd": db.get_credit_balance(user_id),
    }


# ---------------------------------------------------------------------------
# Tax report unlock — one-time $19.99 USDC payment
# ---------------------------------------------------------------------------

TAX_UNLOCK_PRICE_USD = 9.99


def verify_payment_for_tax_unlock(
    user_id: int,
    chain: str,
    tx_hash: str,
    expected_token: str,
) -> dict:
    """Verify an on-chain payment intended to unlock the tax-report feature.

    Fires the same on-chain checks as `verify_payment_for_import` but the
    replay guard hits `tax_unlocks` instead of `credit_transactions`, and
    the expected amount is the fixed TAX_UNLOCK_PRICE_USD. On success the
    caller is responsible for calling `db.grant_tax_unlock`.

    We also cross-check against the import-payment ledger so a tx hash
    used to pay for an import can't be reused to unlock tax reports
    (which would otherwise give the user two paid things for one payment).
    """
    tx_hash = (tx_hash or "").strip().lower()
    if not tx_hash.startswith("0x") or len(tx_hash) != 66:
        raise HTTPException(400, "Invalid tx hash format")
    chain = (chain or "").strip().lower()
    if chain not in {"eth", "arbitrum", "optimism", "base", "polygon", "bsc"}:
        raise HTTPException(400, "Unsupported chain")

    # Short-circuit: already unlocked → treat as success.
    if db.has_tax_unlock(user_id):
        return {
            "status": "ok",
            "reason": "Tax reports already unlocked.",
            "matched_usd": 0.0,
            "matched_from": "",
            "matched_symbol": "",
            "matched_amount": 0.0,
        }

    # Replay guard: this tx hash can't already be a tax unlock for ANY user.
    if db.get_tax_unlock_by_tx(chain, tx_hash) is not None:
        return {
            "status": "already_used",
            "reason": "This payment has already been used to unlock tax reports.",
            "matched_usd": 0.0, "matched_from": "",
            "matched_symbol": "", "matched_amount": 0.0,
        }
    # Cross-bucket guard: also reject if this tx was already used for a
    # pay-per-import charge. One payment = one unlock OR one import, not both.
    if db.find_import_payment(chain, tx_hash) is not None:
        return {
            "status": "already_used",
            "reason": "This payment was already used for a history import.",
            "matched_usd": 0.0, "matched_from": "",
            "matched_symbol": "", "matched_amount": 0.0,
        }

    # Rest of the flow mirrors verify_payment_for_import exactly — we
    # fetch the tx, walk the transfers, require a linked sender. Rather
    # than copy-paste 100 lines, delegate to a minimal inner helper.
    result = _verify_treasury_transfer(
        user_id=user_id,
        chain=chain,
        tx_hash=tx_hash,
        expected_token=expected_token,
        expected_usd=TAX_UNLOCK_PRICE_USD,
    )
    if result["status"] == "ok":
        log.info(
            "wallet_pay.tax_unlock_verified user=%s amount=%.4f tx=%s",
            user_id, result["matched_usd"], tx_hash[:12],
        )
    return result


def _verify_treasury_transfer(
    user_id: int,
    chain: str,
    tx_hash: str,
    expected_token: str,
    expected_usd: float,
) -> dict:
    """Generic on-chain transfer verifier — no replay guard, no ledger write.

    Extracted from verify_payment_for_import so both the import flow and
    the tax-unlock flow can share the Moralis lookup + allowlist + amount
    + sender-is-linked checks. Pure function (returns a status dict, no
    side effects).
    """
    tx = _fetch_tx(chain, tx_hash)
    if tx is None:
        return {
            "status": "pending",
            "reason": "Transaction not yet indexed. Retry in a moment.",
            "matched_usd": 0.0, "matched_from": "",
            "matched_symbol": "", "matched_amount": 0.0,
        }

    age = _tx_age_seconds(tx)
    min_age = MIN_AGE_SECONDS.get(chain, 180)
    if age is None or age < min_age:
        return {
            "status": "pending",
            "reason": f"Waiting for confirmations ({int(age or 0)}s / {min_age}s).",
            "matched_usd": 0.0, "matched_from": "",
            "matched_symbol": "", "matched_amount": 0.0,
        }

    if tx.get("receipt_status") in ("0", 0):
        return _rejected("Transaction reverted on-chain.")

    treasury_low = TREASURY_ADDRESS.lower()
    expected_token_u = (expected_token or "").upper()
    matched_amount_token: float | None = None
    matched_symbol: str | None = None
    matched_from: str | None = None
    matched_usd: float | None = None
    block_number = str(tx.get("block_number") or "")

    # Native-leg match
    for nt in (tx.get("native_transfers") or []):
        if (nt.get("to_address") or "").lower() != treasury_low:
            continue
        amt = float(nt.get("value_formatted") or 0)
        if amt <= 0:
            continue
        sym = NATIVE_SYMBOL.get(chain, "ETH")
        if expected_token_u and expected_token_u != sym:
            continue
        from treasury_watcher import _price_native
        price = _price_native(chain, sym, block_number)
        usd = amt * price
        matched_amount_token, matched_symbol = amt, sym
        matched_from = (nt.get("from_address") or "").lower()
        matched_usd = usd
        break

    if matched_amount_token is None:
        for er in (tx.get("erc20_transfers") or []):
            if (er.get("to_address") or "").lower() != treasury_low:
                continue
            contract = (er.get("address") or "").lower()
            canonical = ACCEPTED_CONTRACTS.get((chain, contract))
            if canonical is None:
                continue
            if expected_token_u and expected_token_u != canonical:
                continue
            amt = float(er.get("value_formatted") or 0)
            if amt <= 0:
                continue
            if canonical in STABLE_SYMBOLS:
                usd = amt * 1.0
            else:
                from history import moralis_historical_price
                price = float(moralis_historical_price(contract, chain, block_number) or 0)
                usd = amt * price
            matched_amount_token, matched_symbol = amt, canonical
            matched_from = (er.get("from_address") or "").lower()
            matched_usd = usd
            break

    if matched_amount_token is None or matched_usd is None:
        return _rejected("No transfer to the treasury found in this transaction.")
    if matched_usd <= 0:
        return _rejected("Could not price this payment. Contact support.")

    if expected_usd > 0 and matched_usd + AMOUNT_TOLERANCE_ABS < expected_usd \
            and matched_usd < expected_usd * (1 - AMOUNT_TOLERANCE_PCT):
        return {
            "status": "rejected",
            "reason": (f"Amount too low: sent ${matched_usd:.2f}, "
                       f"expected at least ${expected_usd:.2f}."),
            "matched_usd": matched_usd,
            "matched_from": matched_from or "",
            "matched_symbol": matched_symbol or "",
            "matched_amount": matched_amount_token,
        }

    sender_user_id = db.find_user_by_wallet_address(matched_from or "")
    if sender_user_id != user_id:
        return {
            "status": "rejected",
            "reason": ("Sender wallet is not linked to your account. "
                       "Link the wallet first and retry."),
            "matched_usd": matched_usd,
            "matched_from": matched_from or "",
            "matched_symbol": matched_symbol or "",
            "matched_amount": matched_amount_token,
        }

    return {
        "status": "ok",
        "reason": "Payment verified",
        "matched_usd": matched_usd,
        "matched_from": matched_from or "",
        "matched_symbol": matched_symbol or expected_token_u,
        "matched_amount": matched_amount_token,
    }
