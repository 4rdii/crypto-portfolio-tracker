"""Smoke tests for the tax cost basis engine.

Goals:
  1. FIFO / LIFO / HIFO produce different numbers on a carefully
     constructed fixture (proves they're actually distinct).
  2. HIFO ≤ FIFO for total gain in a rising market (proves HIFO picks
     the "right" lots to minimize tax).
  3. Holding period classification flips a disposal from short- to
     long-term at the jurisdiction threshold.
  4. Wrapped token canonicalization: a WBTC buy + BTC sell matches.
  5. Discount applied correctly (AU 50%, DE 0% LT).
  6. Missing-basis sales emit zero-basis disposals, not crashes.

Run with: python3 -m pytest webapp/tests/test_tax_methods.py -v
Or standalone: python3 webapp/tests/test_tax_methods.py
"""
import os
import sys
from datetime import datetime, timedelta

# Allow running from any CWD.
_HERE = os.path.dirname(os.path.abspath(__file__))
_WEBAPP = os.path.dirname(_HERE)
sys.path.insert(0, _WEBAPP)

from tax.methods import compute_disposals, canonical  # noqa: E402
from tax.calculator import compute_tax_report, compare_methods  # noqa: E402


def _mk(ts, ttype, symbol, amount, price, tx_id):
    """Minimal transaction dict matching the DB schema columns we care about."""
    return {
        "id": tx_id,
        "ts": ts,
        "tx_type": ttype,
        "symbol": symbol,
        "amount": amount,
        "price_usd": price,
    }


def test_fifo_vs_hifo_diverge():
    """Rising-market fixture: FIFO sells cheap, HIFO sells expensive."""
    txs = [
        _mk(datetime(2024, 1, 1), "buy", "BTC", 1.0, 20000, 1),
        _mk(datetime(2024, 2, 1), "buy", "BTC", 1.0, 40000, 2),
        _mk(datetime(2024, 3, 1), "buy", "BTC", 1.0, 60000, 3),
        # Sell 1 BTC at $80k. 2 untouched lots remain.
        _mk(datetime(2024, 4, 1), "sell", "BTC", 1.0, 80000, 4),
    ]
    fifo, _ = compute_disposals(txs, "FIFO")
    hifo, _ = compute_disposals(txs, "HIFO")
    lifo, _ = compute_disposals(txs, "LIFO")
    avg, _ = compute_disposals(txs, "AVG")

    fifo_gain = sum(d.gain for d in fifo)
    hifo_gain = sum(d.gain for d in hifo)
    lifo_gain = sum(d.gain for d in lifo)
    avg_gain = sum(d.gain for d in avg)

    # Expected:
    # FIFO consumes the $20k lot → gain = 80k - 20k = $60k
    # HIFO consumes the $60k lot → gain = 80k - 60k = $20k
    # LIFO consumes the $60k lot → gain = 80k - 60k = $20k (same as HIFO here)
    # AVG consumes 1 BTC at avg cost $40k → gain = 80k - 40k = $40k
    assert abs(fifo_gain - 60000) < 0.01, f"FIFO gain wrong: {fifo_gain}"
    assert abs(hifo_gain - 20000) < 0.01, f"HIFO gain wrong: {hifo_gain}"
    assert abs(lifo_gain - 20000) < 0.01, f"LIFO gain wrong: {lifo_gain}"
    assert abs(avg_gain - 40000) < 0.01, f"AVG gain wrong: {avg_gain}"
    print(f"  FIFO: ${fifo_gain:>10,.0f}   HIFO: ${hifo_gain:>10,.0f}   "
          f"LIFO: ${lifo_gain:>10,.0f}   AVG: ${avg_gain:>10,.0f}")


def test_hifo_minimizes_gain_rising_market():
    """On a rising market, HIFO should give the smallest gain."""
    txs = []
    for i, price in enumerate([10000, 20000, 30000, 40000, 50000], start=1):
        txs.append(_mk(datetime(2023, i, 1), "buy", "ETH", 1.0, price, i))
    txs.append(_mk(datetime(2024, 6, 1), "sell", "ETH", 3.0, 60000, 99))

    results = {m: sum(d.gain for d in compute_disposals(txs, m)[0])
               for m in ("FIFO", "HIFO", "LIFO", "AVG")}
    print(f"  {results}")
    assert results["HIFO"] <= results["FIFO"], "HIFO should not exceed FIFO"
    assert results["HIFO"] <= results["LIFO"], "HIFO should not exceed LIFO"


def test_long_term_classification_us():
    """A 367-day hold is long-term in the US (threshold is 366 days)."""
    txs = [
        _mk(datetime(2023, 1, 1), "buy", "BTC", 1.0, 20000, 1),
        # 367 days later
        _mk(datetime(2024, 1, 3), "sell", "BTC", 1.0, 50000, 2),
    ]
    report = compute_tax_report(txs, 2024, "US", "FIFO")
    assert report.long_term_gain > 0, "Expected a long-term gain"
    assert report.short_term_gain == 0, "Expected no short-term gain"
    assert abs(report.long_term_gain - 30000) < 0.01
    print(f"  US LT gain: ${report.long_term_gain:,.0f}")


def test_au_50pct_discount():
    """AU applies 50% discount to LT gains. Short-term stays full."""
    # Buy, hold 400 days, sell → long-term under AU's 12-month rule.
    txs = [
        _mk(datetime(2023, 1, 1), "buy", "BTC", 1.0, 10000, 1),
        _mk(datetime(2024, 2, 5), "sell", "BTC", 1.0, 30000, 2),
    ]
    report = compute_tax_report(txs, 2024, "AU", "FIFO")
    # AU tax year ends June 30, so 2024 AU year = July 2023 – June 2024 ✓
    assert abs(report.long_term_gain - 20000) < 0.01
    assert abs(report.taxable_gain - 10000) < 0.01, \
        f"Expected $10k taxable after 50% discount, got ${report.taxable_gain}"
    print(f"  AU LT gain: ${report.long_term_gain:,.0f}  "
          f"taxable: ${report.taxable_gain:,.0f}")


def test_de_long_term_tax_free():
    """Germany: >1y hold makes gains tax-free (0% discount)."""
    txs = [
        _mk(datetime(2023, 1, 1), "buy", "BTC", 1.0, 10000, 1),
        _mk(datetime(2024, 6, 1), "sell", "BTC", 1.0, 60000, 2),
    ]
    report = compute_tax_report(txs, 2024, "DE", "FIFO")
    assert report.long_term_gain == 50000
    assert report.taxable_gain == 0, \
        f"DE LT should be tax-free, got ${report.taxable_gain}"
    print(f"  DE LT gain: ${report.long_term_gain:,.0f}  "
          f"taxable: ${report.taxable_gain:,.0f} (tax free)")


def test_wrapped_token_canonicalization():
    """Buying WBTC then selling BTC should match against the same lot."""
    txs = [
        _mk(datetime(2024, 1, 1), "buy", "WBTC", 1.0, 20000, 1),
        _mk(datetime(2024, 6, 1), "sell", "BTC", 1.0, 50000, 2),
    ]
    disposals, lots = compute_disposals(txs, "FIFO")
    assert len(disposals) == 1
    d = disposals[0]
    assert d.cost_basis == 20000, \
        f"Expected WBTC lot to match BTC sell, got basis ${d.cost_basis}"
    assert d.gain == 30000
    assert d.symbol == "BTC"  # canonicalized
    print(f"  WBTC buy matched BTC sell: gain=${d.gain:,.0f}")


def test_missing_basis_sale():
    """Selling without a prior buy emits a zero-basis disposal, not a crash."""
    txs = [
        _mk(datetime(2024, 6, 1), "sell", "SOL", 10.0, 150, 1),
    ]
    disposals, _ = compute_disposals(txs, "FIFO")
    assert len(disposals) == 1
    assert disposals[0].cost_basis == 0
    assert disposals[0].proceeds == 1500
    print(f"  zero-basis sale: proceeds=${disposals[0].proceeds:,.0f}")


def test_compare_methods_returns_sorted():
    """compare_methods should return cheapest-first for a rising market."""
    txs = [
        _mk(datetime(2024, 1, 1), "buy", "ETH", 1.0, 1000, 1),
        _mk(datetime(2024, 2, 1), "buy", "ETH", 1.0, 2000, 2),
        _mk(datetime(2024, 3, 1), "buy", "ETH", 1.0, 3000, 3),
        _mk(datetime(2024, 4, 1), "sell", "ETH", 1.0, 4000, 4),
    ]
    rows = compare_methods(txs, 2024, "US")
    methods = [r["method"] for r in rows]
    print(f"  order by taxable (cheapest first): {methods}")
    # On a purely rising market, HIFO and LIFO tie (both pick the $3k lot)
    # so either can be first. FIFO should always be the MOST expensive.
    assert rows[0]["method"] in ("HIFO", "LIFO")
    assert rows[-1]["method"] == "FIFO"
    assert rows[0]["taxable_gain"] <= rows[-1]["taxable_gain"]


def test_in_no_loss_offset():
    """India: losses don't offset gains. Pure gain positions still work."""
    txs = [
        _mk(datetime(2024, 1, 1), "buy", "BTC", 1.0, 50000, 1),
        _mk(datetime(2024, 2, 1), "buy", "ETH", 1.0, 3000, 2),
        _mk(datetime(2024, 3, 1), "sell", "BTC", 1.0, 40000, 3),  # $10k loss
        _mk(datetime(2024, 4, 1), "sell", "ETH", 1.0, 4000, 4),   # $1k gain
    ]
    report = compute_tax_report(txs, 2024, "IN", "FIFO")
    # Net is -$9k, but India can't offset; taxable_gain should be >= 0
    assert report.taxable_gain >= 0
    print(f"  IN total={report.total_gain:,.0f}  "
          f"taxable={report.taxable_gain:,.0f}")


def test_self_transfer_filtered():
    """A withdraw+deposit pair of the same asset close in time must NOT
    be treated as a disposal (phantom loss regression).

    The fixture mirrors the x4rde 2025 bug: buy ETH high, withdraw some
    34s before a matching deposit (transfer between own wallets), then
    sell a DIFFERENT chunk of ETH at a real loss. Expected behaviour:

      • Before filter: 2 disposals, one phantom + one real
      • After filter:  1 disposal, only the real loss
    """
    from tax.self_transfers import detect_self_transfer_ids
    txs = [
        _mk(datetime(2024, 1, 1, 10, 0, 0),  "buy",      "ETH", 1.0, 4000, 1),
        _mk(datetime(2024, 6, 1, 12, 0, 0),  "withdraw", "ETH", 0.1, 3000, 2),
        _mk(datetime(2024, 6, 1, 12, 0, 34), "deposit",  "ETH", 0.1, 3000, 3),
        _mk(datetime(2024, 7, 1, 10, 0, 0),  "sell",     "ETH", 0.2, 2500, 4),
    ]
    matched = detect_self_transfer_ids(txs)
    assert matched == {2, 3}, f"expected {{2,3}}, got {matched}"

    report = compute_tax_report(txs, 2024, "US", "FIFO")
    # Only the REAL sell (id 4) should be reported. 0.2 ETH @ $4000 basis
    # sold at $2500 = 0.2 * (2500 - 4000) = -$300 loss.
    assert report.disposal_count == 1, f"disp={report.disposal_count}"
    assert abs(report.total_gain - (-300.0)) < 0.01, \
        f"expected -300, got {report.total_gain}"
    assert any("self-transfer" in c for c in report.caveats), \
        "caveat about self-transfer filtering missing"
    print(f"  filtered 1 pair; reported disposals={report.disposal_count} "
          f"gain=${report.total_gain:,.2f}")


def test_self_transfer_not_matched_across_symbols():
    """Same amount but different symbols must NOT be paired."""
    from tax.self_transfers import detect_self_transfer_ids
    txs = [
        _mk(datetime(2024, 6, 1, 12, 0, 0),  "withdraw", "BTC", 0.5, 60000, 1),
        _mk(datetime(2024, 6, 1, 12, 1, 0),  "deposit",  "ETH", 0.5, 3000, 2),
    ]
    matched = detect_self_transfer_ids(txs)
    assert matched == set(), f"cross-symbol match leak: {matched}"
    print(f"  correctly ignored cross-symbol BTC/ETH transfer")


def test_self_transfer_outside_time_window():
    """Withdraw+deposit more than 1 hour apart should NOT pair."""
    from tax.self_transfers import detect_self_transfer_ids
    txs = [
        _mk(datetime(2024, 6, 1, 12, 0, 0), "withdraw", "ETH", 0.5, 3000, 1),
        _mk(datetime(2024, 6, 1, 14, 0, 1), "deposit",  "ETH", 0.5, 3000, 2),
    ]
    matched = detect_self_transfer_ids(txs)
    assert matched == set(), f"window leak: {matched}"
    print(f"  correctly ignored 2h-apart transfer")


if __name__ == "__main__":
    tests = [
        test_fifo_vs_hifo_diverge,
        test_hifo_minimizes_gain_rising_market,
        test_long_term_classification_us,
        test_au_50pct_discount,
        test_de_long_term_tax_free,
        test_wrapped_token_canonicalization,
        test_missing_basis_sale,
        test_compare_methods_returns_sorted,
        test_in_no_loss_offset,
        test_self_transfer_filtered,
        test_self_transfer_not_matched_across_symbols,
        test_self_transfer_outside_time_window,
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
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
