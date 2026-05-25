"""Tests for the Solana transaction history importer and supporting modules.

Covers:
  1. solana_tokens — allowlist spot-checks and spam filter logic
  2. sol_history — transaction parser with mocked Helius responses
  3. birdeye_prices — cache hit/miss and rate-limit backoff

All network calls are mocked via unittest.mock — no live API keys required.
Run with:
    python3 webapp/tests/test_sol_history.py
Or via pytest:
    python3 -m pytest webapp/tests/test_sol_history.py -v
"""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Allow imports from the webapp directory regardless of CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))
_WEBAPP = os.path.dirname(_HERE)
sys.path.insert(0, _WEBAPP)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_helius_tx(sig: str, tx_type: str, timestamp: int, token_transfers=None,
                    native_transfers=None) -> dict:
    """Construct a minimal Helius Enhanced Transaction dict for testing."""
    return {
        "signature": sig,
        "type": tx_type,
        "timestamp": timestamp,
        "tokenTransfers": token_transfers or [],
        "nativeTransfers": native_transfers or [],
        "description": "",
        "source": "JUPITER",
    }


# ---------------------------------------------------------------------------
# Test 1: solana_tokens allowlist spot-checks
# ---------------------------------------------------------------------------

def test_solana_tokens_allowlist():
    """Five known mints must resolve to their correct symbols."""
    from tax.solana_tokens import LEGIT_MINTS, resolve_symbol, is_legit_mint

    # USDC
    assert is_legit_mint("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"), "USDC mint not in allowlist"
    assert resolve_symbol("EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v") == "USDC", "USDC symbol wrong"

    # BONK
    assert is_legit_mint("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"), "BONK mint not in allowlist"
    assert resolve_symbol("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263") == "BONK", "BONK symbol wrong"

    # JUP
    assert is_legit_mint("JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"), "JUP mint not in allowlist"
    assert resolve_symbol("JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN") == "JUP", "JUP symbol wrong"

    # mSOL
    assert is_legit_mint("mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So"), "mSOL mint not in allowlist"
    assert resolve_symbol("mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So") == "mSOL", "mSOL symbol wrong"

    # WIF
    assert is_legit_mint("EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm"), "WIF mint not in allowlist"
    assert resolve_symbol("EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm") == "WIF", "WIF symbol wrong"

    # Unknown mint should return None
    assert resolve_symbol("FakeMint111111111111111111111111111111111111") is None, \
        "Unknown mint should return None"

    print(f"  All {len(LEGIT_MINTS)} allowlist entries present; 5 spot-checks passed")


# ---------------------------------------------------------------------------
# Test 2: spam token filter
# ---------------------------------------------------------------------------

def test_spam_token_filter():
    """Unknown mint + UNKNOWN type → rejected. Unknown mint + SWAP → kept. Known mint → always kept."""
    from tax.solana_tokens import is_spam_token, LEGIT_MINTS

    UNKNOWN_MINT = "ScamMint1111111111111111111111111111111111111"
    KNOWN_MINT = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # BONK

    # Unknown mint + UNKNOWN type → spam (drop it)
    assert is_spam_token(UNKNOWN_MINT, "UNKNOWN") is True, \
        "Unknown mint with UNKNOWN type should be flagged as spam"

    # Unknown mint + AIRDROP type → spam
    assert is_spam_token(UNKNOWN_MINT, "AIRDROP") is True, \
        "Unknown mint with AIRDROP type should be flagged as spam"

    # Unknown mint + SWAP type → NOT spam (user voluntarily swapped it)
    assert is_spam_token(UNKNOWN_MINT, "SWAP") is False, \
        "Unknown mint with SWAP type should NOT be flagged as spam"

    # Unknown mint + TRANSFER type → NOT spam (user voluntarily sent/received it)
    assert is_spam_token(UNKNOWN_MINT, "TRANSFER") is False, \
        "Unknown mint with TRANSFER type should NOT be flagged as spam"

    # Known mint (BONK) → NEVER spam regardless of type
    assert is_spam_token(KNOWN_MINT, "UNKNOWN") is False, \
        "Known allowlist mint should never be flagged as spam"
    assert is_spam_token(KNOWN_MINT, "AIRDROP") is False, \
        "Known allowlist mint should never be flagged as spam even for AIRDROP"

    print("  Spam filter: unknown+UNKNOWN rejected, unknown+SWAP kept, known mint always kept")


# ---------------------------------------------------------------------------
# Test 3: fetch_sol_history with mocked Helius responses
# ---------------------------------------------------------------------------

def test_fetch_sol_history_mocked():
    """Mock Helius API: 2 swaps + 1 transfer → correct row count and shape."""
    import sol_history

    WALLET = "F1iLXdL7rSUvjxomqxj8HcWTnFgSkTMGhj9G84XoAfKz"
    USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    SOL_MINT = "So11111111111111111111111111111111111111112"
    JUP_MINT = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"

    ts1 = int(datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc).timestamp())
    ts2 = int(datetime(2024, 6, 2, 10, 0, tzinfo=timezone.utc).timestamp())

    # Swap tx 1: sell 100 USDC, buy 5 JUP
    swap_tx_1 = _make_helius_tx(
        sig="sig_swap1_" + "a" * 50,
        tx_type="SWAP",
        timestamp=ts1,
        token_transfers=[
            {
                "fromUserAccount": WALLET,
                "toUserAccount": "JupiterPool1111111111111111111111111111111",
                "mint": USDC_MINT,
                "tokenAmount": 100.0,
            },
            {
                "fromUserAccount": "JupiterPool1111111111111111111111111111111",
                "toUserAccount": WALLET,
                "mint": JUP_MINT,
                "tokenAmount": 5.0,
            },
        ],
    )

    # Swap tx 2: sell 0.5 SOL (native), buy 50 USDC
    # SOL leg goes through tokenTransfers as WSOL on Helius
    swap_tx_2 = _make_helius_tx(
        sig="sig_swap2_" + "b" * 50,
        tx_type="SWAP",
        timestamp=ts2,
        token_transfers=[
            {
                "fromUserAccount": WALLET,
                "toUserAccount": "JupiterPool1111111111111111111111111111111",
                "mint": SOL_MINT,
                "tokenAmount": 0.5,
            },
            {
                "fromUserAccount": "JupiterPool1111111111111111111111111111111",
                "toUserAccount": WALLET,
                "mint": USDC_MINT,
                "tokenAmount": 50.0,
            },
        ],
    )

    # Transfer tx: receive 1 JUP from external wallet
    transfer_tx = _make_helius_tx(
        sig="sig_transfer1_" + "c" * 47,
        tx_type="TRANSFER",
        timestamp=ts2 + 3600,
        token_transfers=[
            {
                "fromUserAccount": "ExternalWallet1111111111111111111111111111",
                "toUserAccount": WALLET,
                "mint": JUP_MINT,
                "tokenAmount": 1.0,
            },
        ],
    )

    # Mock: first page returns all 3 txs, second page is empty (end of history)
    pages = [[swap_tx_1, swap_tx_2, transfer_tx], []]
    page_idx = [0]

    def mock_helius_page(address, before, limit=100):
        result = pages[page_idx[0]] if page_idx[0] < len(pages) else []
        page_idx[0] += 1
        return result

    # Mock _resolve_price to return flat $1 for stables, $0.5 for JUP, $100 for SOL
    def mock_resolve_price(symbol, mint, unix_ts):
        mapping = {"USDC": 1.0, "JUP": 0.5, "SOL": 100.0}
        return mapping.get(symbol, 0.0)

    with patch.object(sol_history, "_helius_page", side_effect=mock_helius_page), \
         patch.object(sol_history, "_resolve_price", side_effect=mock_resolve_price):
        rows = sol_history.fetch_sol_history(WALLET, max_txs=1000)

    # Swap 1: sell USDC + buy JUP = 2 rows
    # Swap 2: sell SOL + buy USDC = 2 rows
    # Transfer: deposit JUP = 1 row
    # Total expected: 5
    assert len(rows) == 5, f"Expected 5 rows, got {len(rows)}: {json.dumps(rows, indent=2)}"

    # Check row shape — every row must have all required keys
    required_keys = {"ts", "tx_type", "symbol", "amount", "price_usd", "notes"}
    for i, r in enumerate(rows):
        missing = required_keys - set(r.keys())
        assert not missing, f"Row {i} missing keys: {missing}"
        assert isinstance(r["ts"], str), f"Row {i} ts must be str, got {type(r['ts'])}"
        assert r["tx_type"] in {"buy", "sell", "deposit", "withdraw"}, \
            f"Row {i} tx_type invalid: {r['tx_type']}"
        assert float(r["amount"]) > 0, f"Row {i} amount must be positive"
        # price_usd can be 0 (unknown) but must not be NaN or negative
        price = float(r["price_usd"])
        assert price >= 0, f"Row {i} price_usd must be >= 0, got {price}"
        assert price == price, f"Row {i} price_usd is NaN"  # NaN != NaN

    # Verify specific tx types
    sell_rows = [r for r in rows if r["tx_type"] == "sell"]
    buy_rows = [r for r in rows if r["tx_type"] == "buy"]
    deposit_rows = [r for r in rows if r["tx_type"] == "deposit"]

    assert len(sell_rows) == 2, f"Expected 2 sell rows, got {len(sell_rows)}"
    assert len(buy_rows) == 2, f"Expected 2 buy rows, got {len(buy_rows)}"
    assert len(deposit_rows) == 1, f"Expected 1 deposit row, got {len(deposit_rows)}"

    # The JUP deposit should have tx_type=deposit and amount=1
    jup_deposit = next((r for r in deposit_rows if r["symbol"] == "JUP"), None)
    assert jup_deposit is not None, "Expected a JUP deposit row"
    assert abs(float(jup_deposit["amount"]) - 1.0) < 1e-6, \
        f"JUP deposit amount wrong: {jup_deposit['amount']}"

    print(f"  Parsed {len(rows)} rows: {len(sell_rows)} sell, {len(buy_rows)} buy, {len(deposit_rows)} deposit")


# ---------------------------------------------------------------------------
# Test 3b: self-transfer skip (both token and native paths)
# ---------------------------------------------------------------------------

def test_fetch_sol_history_skips_self_transfers():
    """Wallet = from AND to on a leg must emit zero rows for that leg.

    Covers the regression path called out by the review (M4): the row-level
    self-transfer skip at sol_history.py's _parse_transfer and
    _parse_native_transfers. If someone later refactors either check we
    need a fixture that catches the break — the tax-layer self-transfer
    collapser doesn't fire on Solana because rows don't carry tx_hash.

    The fixture has three transactions:
      1. SWAP with BOTH legs from→to = wallet (self wrap/unwrap) → 0 rows
      2. TRANSFER token leg from→to = wallet (consolidating ATAs)    → 0 rows
      3. TRANSFER native SOL leg from→to = wallet (moving lamports)  → 0 rows
    A control real swap is also included to prove the parser still works
    alongside the skipped legs.
    """
    import sol_history

    WALLET = "F1iLXdL7rSUvjxomqxj8HcWTnFgSkTMGhj9G84XoAfKz"
    USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    SOL_MINT = "So11111111111111111111111111111111111111112"
    JUP_MINT = "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN"

    ts_base = int(datetime(2024, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp())

    # (1) A SWAP tx where both tokenTransfers are self→self — should be dropped
    self_swap = _make_helius_tx(
        sig="sig_selfswap_" + "a" * 48,
        tx_type="SWAP",
        timestamp=ts_base,
        token_transfers=[
            {
                "fromUserAccount": WALLET,
                "toUserAccount": WALLET,
                "mint": USDC_MINT,
                "tokenAmount": 50.0,
            },
            {
                "fromUserAccount": WALLET,
                "toUserAccount": WALLET,
                "mint": SOL_MINT,
                "tokenAmount": 0.25,
            },
        ],
    )

    # (2) A TRANSFER tx with a token self-leg and a native self-leg — both dropped
    self_transfer = _make_helius_tx(
        sig="sig_selftransfer_" + "b" * 44,
        tx_type="TRANSFER",
        timestamp=ts_base + 3600,
        token_transfers=[
            {
                "fromUserAccount": WALLET,
                "toUserAccount": WALLET,
                "mint": JUP_MINT,
                "tokenAmount": 10.0,
            },
        ],
        native_transfers=[
            {
                "fromUserAccount": WALLET,
                "toUserAccount": WALLET,
                "amount": 1_000_000_000,  # 1 SOL in lamports
            },
        ],
    )

    # (3) Control: a real outbound transfer so we know the parser is alive
    control_transfer = _make_helius_tx(
        sig="sig_control_" + "c" * 50,
        tx_type="TRANSFER",
        timestamp=ts_base + 7200,
        token_transfers=[
            {
                "fromUserAccount": WALLET,
                "toUserAccount": "ExternalWallet1111111111111111111111111111",
                "mint": USDC_MINT,
                "tokenAmount": 5.0,
            },
        ],
    )

    pages = [[self_swap, self_transfer, control_transfer], []]
    page_idx = [0]

    def mock_helius_page(address, before, limit=100):
        result = pages[page_idx[0]] if page_idx[0] < len(pages) else []
        page_idx[0] += 1
        return result

    def mock_resolve_price(symbol, mint, unix_ts):
        return {"USDC": 1.0, "JUP": 0.5, "SOL": 100.0}.get(symbol, 0.0)

    with patch.object(sol_history, "_helius_page", side_effect=mock_helius_page), \
         patch.object(sol_history, "_resolve_price", side_effect=mock_resolve_price):
        rows = sol_history.fetch_sol_history(WALLET, max_txs=1000)

    # Only the control transfer should survive
    assert len(rows) == 1, \
        f"Expected 1 row (control only), got {len(rows)}: {json.dumps(rows, indent=2)}"
    assert rows[0]["symbol"] == "USDC", f"Expected USDC, got {rows[0]['symbol']}"
    assert rows[0]["tx_type"] == "withdraw", \
        f"Expected withdraw, got {rows[0]['tx_type']}"
    assert abs(float(rows[0]["amount"]) - 5.0) < 1e-6

    print("  self-transfer legs (swap+token+native) dropped; 1 control row kept")


# ---------------------------------------------------------------------------
# Test 3c: spam filter — unlisted mint in AIRDROP/UNKNOWN context must drop
# ---------------------------------------------------------------------------

def test_fetch_sol_history_drops_spam_airdrops():
    """An AIRDROP tx with an unlisted mint + Helius metadata symbol must
    NOT leak into the output.

    Regression for review finding M1: ``is_spam_token`` was imported &
    tested but never actually called by the dispatcher, so spam with a
    Helius metadata symbol would sail through via the metadata fallback
    in ``_parse_transfer``. After the fix the dispatcher passes the
    original tx_type into ``_parse_transfer`` and the filter drops the
    row for non-voluntary tx types.
    """
    import sol_history

    WALLET = "F1iLXdL7rSUvjxomqxj8HcWTnFgSkTMGhj9G84XoAfKz"
    SPAM_MINT = "ScamMint111111111111111111111111111111111111"

    ts = int(datetime(2024, 8, 1, 12, 0, tzinfo=timezone.utc).timestamp())
    # AIRDROP tx type (not in skip_prefixes, so falls into else branch).
    # The transfer leg carries a Helius-provided symbol field, which in the
    # pre-fix code would have been taken at face value.
    airdrop = _make_helius_tx(
        sig="sig_airdrop_" + "d" * 50,
        tx_type="AIRDROP",
        timestamp=ts,
        token_transfers=[
            {
                "fromUserAccount": "SpamSource111111111111111111111111111111111",
                "toUserAccount": WALLET,
                "mint": SPAM_MINT,
                "tokenAmount": 1_000_000.0,
                "symbol": "FREEMONEY",  # Helius-reported symbol (spam)
            },
        ],
    )

    pages = [[airdrop], []]
    page_idx = [0]

    def mock_helius_page(address, before, limit=100):
        result = pages[page_idx[0]] if page_idx[0] < len(pages) else []
        page_idx[0] += 1
        return result

    def mock_resolve_price(symbol, mint, unix_ts):
        return 0.0  # unknown token, no price

    with patch.object(sol_history, "_helius_page", side_effect=mock_helius_page), \
         patch.object(sol_history, "_resolve_price", side_effect=mock_resolve_price):
        rows = sol_history.fetch_sol_history(WALLET, max_txs=1000)

    assert rows == [], \
        f"Spam airdrop should have been dropped, got: {json.dumps(rows, indent=2)}"
    print("  spam airdrop with Helius metadata symbol correctly dropped")


# ---------------------------------------------------------------------------
# Test 3d: _helius_page distinguishes error from end-of-history
# ---------------------------------------------------------------------------

def test_helius_page_retries_transient_and_returns_none():
    """Persistent 429s should trigger retries and return None (not []).

    The caller treats [] as end-of-history and None as "abort the walk",
    so conflating them would silently truncate a multi-page import on a
    single transient failure (review finding M3).
    """
    import sol_history

    with patch.dict(os.environ, {}, clear=False):
        # Ensure the module has an API key so it reaches the request path
        original_key = sol_history.HELIUS_API_KEY
        sol_history.HELIUS_API_KEY = "test-key"

        try:
            call_count = [0]
            sleep_calls = []

            def mock_get(*args, **kwargs):
                call_count[0] += 1
                resp = MagicMock()
                resp.status_code = 429
                return resp

            with patch.object(sol_history._SESSION, "get", side_effect=mock_get), \
                 patch.object(sol_history.time, "sleep",
                              side_effect=lambda s: sleep_calls.append(s)):
                result = sol_history._helius_page("SomeAddr", before=None, max_retries=3)

            assert result is None, \
                f"Persistent 429 should return None (not []), got {result!r}"
            assert call_count[0] == 3, \
                f"Expected 3 attempts, got {call_count[0]}"
            assert len(sleep_calls) >= 1, \
                f"Expected at least one backoff sleep, got {sleep_calls}"
            assert sleep_calls[0] == 2.0, \
                f"First backoff should be 2.0s, got {sleep_calls[0]}"
            print(f"  429 → {call_count[0]} retries, returned None ✓")
        finally:
            sol_history.HELIUS_API_KEY = original_key


def test_fetch_sol_history_raises_on_persistent_error():
    """fetch_sol_history must RAISE (not silently truncate) when
    _helius_page returns None. Silently returning a partial result would
    destroy cost basis for every older tx the user has.
    """
    import sol_history

    def always_none(*args, **kwargs):
        return None

    with patch.object(sol_history, "_helius_page", side_effect=always_none):
        try:
            sol_history.fetch_sol_history("SomeAddr", max_txs=1000)
        except RuntimeError as exc:
            assert "aborted" in str(exc).lower(), \
                f"Expected 'aborted' in error message, got: {exc}"
            print(f"  persistent error → RuntimeError raised ✓")
            return

    raise AssertionError("fetch_sol_history should have raised RuntimeError")


# ---------------------------------------------------------------------------
# Test 4: Birdeye price cache hit and miss
# ---------------------------------------------------------------------------

def test_birdeye_price_cache_hit_miss():
    """First call should hit Birdeye; second call for same mint+date should read from cache."""
    # Use a temp DB so we don't pollute the real portfolio.db
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_db = f.name

    try:
        import birdeye_prices

        # Patch the module-level DB path to our temp file
        original_db = birdeye_prices._CACHE_DB
        birdeye_prices._CACHE_DB = Path(tmp_db)
        birdeye_prices._KEY_INVALID = False  # reset any prior test state

        mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC
        unix_ts = int(datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc).timestamp())

        mock_birdeye_response = {
            "data": {
                "items": [
                    {"unixTime": unix_ts, "value": 1.0001},
                ]
            }
        }

        call_count = [0]

        def mock_get(*args, **kwargs):
            call_count[0] += 1
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = mock_birdeye_response
            return resp

        with patch.object(birdeye_prices._SESSION, "get", side_effect=mock_get):
            # First call — should hit the API
            price1 = birdeye_prices.get_sol_price_cached(mint, unix_ts)
            assert price1 is not None and price1 > 0, f"First call should return a price, got {price1}"
            assert call_count[0] == 1, f"Expected 1 API call, got {call_count[0]}"

            # Second call for the same (mint, date) — should read from cache, no API call
            price2 = birdeye_prices.get_sol_price_cached(mint, unix_ts)
            assert price2 == price1, f"Cache hit should return same price: {price1} vs {price2}"
            assert call_count[0] == 1, f"Cache hit should not call API; got {call_count[0]} calls"

        print(f"  Cache miss → API ({price1:.6f}), cache hit → no API call")

    finally:
        birdeye_prices._CACHE_DB = original_db
        os.unlink(tmp_db)


# ---------------------------------------------------------------------------
# Test 5: Birdeye rate-limit backoff
# ---------------------------------------------------------------------------

def test_birdeye_rate_limit_backoff():
    """Three consecutive 429s should trigger backoff and ultimately return None."""
    import birdeye_prices

    birdeye_prices._KEY_INVALID = False  # reset

    mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # BONK
    unix_ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())

    call_count = [0]
    sleep_calls = []

    def mock_429(*args, **kwargs):
        call_count[0] += 1
        resp = MagicMock()
        resp.status_code = 429
        return resp

    with patch.object(birdeye_prices._SESSION, "get", side_effect=mock_429), \
         patch("birdeye_prices.time") as mock_time:
        # Capture sleep calls so we can verify backoff happened
        mock_time.sleep = lambda s: sleep_calls.append(s)

        result = birdeye_prices.get_birdeye_historical_price(mint, unix_ts)

    # Should have tried 3 times (max_retries=3)
    assert call_count[0] == 3, f"Expected 3 API calls (all 429), got {call_count[0]}"

    # Should have slept between attempts — 2s, 4s pattern (3 retries → 2 sleeps)
    assert len(sleep_calls) >= 1, f"Expected at least 1 sleep call for backoff, got {sleep_calls}"
    assert sleep_calls[0] == 2.0, f"First backoff sleep should be 2.0s, got {sleep_calls[0]}"
    if len(sleep_calls) >= 2:
        assert sleep_calls[1] == 4.0, f"Second backoff sleep should be 4.0s, got {sleep_calls[1]}"

    # Final result should be None (all retries failed)
    assert result is None, f"Should return None after all retries exhausted, got {result}"

    print(f"  429 backoff: {call_count[0]} attempts, sleeps={sleep_calls}, result=None ✓")


# ---------------------------------------------------------------------------
# Test 6: Price waterfall — Binance called first, Birdeye NOT called when Binance hits
# ---------------------------------------------------------------------------

def test_price_waterfall_binance_first():
    """When Binance returns a valid price, Birdeye should NOT be called."""
    import sol_history
    import binance_prices
    import birdeye_prices

    symbol = "SOL"
    mint = "So11111111111111111111111111111111111111112"
    unix_ts = int(datetime(2024, 3, 15, 12, 0, tzinfo=timezone.utc).timestamp())
    expected_price = 150.0

    binance_call_count = [0]
    birdeye_call_count = [0]

    def mock_binance_price(sym, date):
        binance_call_count[0] += 1
        return expected_price  # Binance has the price

    def mock_birdeye_price(m, ts):
        birdeye_call_count[0] += 1
        return 999.0  # Should never be called

    # Mock the stable import to avoid needing a real DB
    mock_st = MagicMock()
    mock_st.STABLECOIN_MINTS = set()  # SOL is not a stable

    mock_bp = MagicMock()
    mock_bp.binance_historical_price = mock_binance_price

    mock_bep = MagicMock()
    mock_bep.get_sol_price_cached = mock_birdeye_price

    def mock_import_modules():
        return mock_bp, mock_bep, mock_st

    with patch.object(sol_history, "_import_price_modules", side_effect=mock_import_modules):
        price = sol_history._resolve_price(symbol, mint, unix_ts)

    assert abs(price - expected_price) < 0.01, \
        f"Price should come from Binance ({expected_price}), got {price}"
    assert binance_call_count[0] == 1, f"Binance should be called once, got {binance_call_count[0]}"
    assert birdeye_call_count[0] == 0, \
        f"Birdeye should NOT be called when Binance hits, got {birdeye_call_count[0]} calls"

    print(f"  Price waterfall: Binance called ({price:.2f}), Birdeye skipped ✓")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_solana_tokens_allowlist,
        test_spam_token_filter,
        test_fetch_sol_history_mocked,
        test_fetch_sol_history_skips_self_transfers,
        test_fetch_sol_history_drops_spam_airdrops,
        test_helius_page_retries_transient_and_returns_none,
        test_fetch_sol_history_raises_on_persistent_error,
        test_birdeye_price_cache_hit_miss,
        test_birdeye_rate_limit_backoff,
        test_price_waterfall_binance_first,
    ]

    passed = failed = 0
    for t in tests:
        try:
            print(f"• {t.__name__}")
            t()
            passed += 1
        except AssertionError as e:
            print(f"  FAIL: {e}")
            failed += 1
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"  ERROR: {type(e).__name__}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
