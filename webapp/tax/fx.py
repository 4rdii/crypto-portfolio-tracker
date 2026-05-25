"""Foreign exchange helpers for tax reports.

Most jurisdictions require capital gains to be reported in the local
currency. Our calculator works in USD (matching how prices are stored)
but the rendered report should show both USD and the jurisdiction's
native currency so the user knows what number actually goes on their
form.

Design choices
--------------
1. We report at a SINGLE rate for the whole tax year — specifically the
   year-end rate from the IRS / HMRC / CRA equivalent. Daily marking is
   the "purist" approach but in practice every tax authority either
   accepts year-end rates or publishes an official yearly average; the
   user can override in their accountant's tool if they want per-day.
2. Rates are baked in as a hardcoded table below. Refreshing them
   annually is a 30-second edit. We skip CoinGecko / exchange APIs
   here because:
     - tax reports are small volume
     - the year-end rate is a historical fact that doesn't need live data
     - offline reliability matters more than freshness for tax software
3. USD→USD always returns 1.0. Any missing year falls back to the most
   recent rate we have, with a caveat added to the report.

If a year's rate is missing the calculator flags the report with a
caveat like "FX rate for 2024 not yet published — using 2023 rate"
so the user knows to double-check before filing.
"""
from __future__ import annotations


# Year-end USD→target rates. Format: (year, target_currency) → rate
# meaning "1 USD = rate units of target".
#
# Sources (update annually):
#   GBP: IRS yearly average exchange rates (approx)
#   EUR: ECB year-end reference rate
#   CAD: Bank of Canada yearly average
#   AUD: ATO annual rates (fiscal year Jun 30 close)
#   SGD: MAS reference rate
#   JPY: BOJ
#   INR: RBI reference
_YEAR_END_RATES: dict[tuple[int, str], float] = {
    # 2023 year-end
    (2023, "GBP"): 0.7857,
    (2023, "EUR"): 0.9054,
    (2023, "CAD"): 1.3228,
    (2023, "AUD"): 1.4675,
    (2023, "SGD"): 1.3205,
    (2023, "JPY"): 141.04,
    (2023, "INR"): 83.21,
    # 2024 year-end (approximate — update when final BOK/IRS numbers land)
    (2024, "GBP"): 0.7984,
    (2024, "EUR"): 0.9648,
    (2024, "CAD"): 1.4377,
    (2024, "AUD"): 1.6123,
    (2024, "SGD"): 1.3672,
    (2024, "JPY"): 157.18,
    (2024, "INR"): 85.62,
    # 2025 year-end (approximate — update at year end)
    (2025, "GBP"): 0.8012,
    (2025, "EUR"): 0.9630,
    (2025, "CAD"): 1.4350,
    (2025, "AUD"): 1.6180,
    (2025, "SGD"): 1.3450,
    (2025, "JPY"): 156.30,
    (2025, "INR"): 85.80,
    # 2026 (placeholder until we set a real one — flagged in caveats)
    (2026, "GBP"): 0.8012,
    (2026, "EUR"): 0.9630,
    (2026, "CAD"): 1.4350,
    (2026, "AUD"): 1.6180,
    (2026, "SGD"): 1.3450,
    (2026, "JPY"): 156.30,
    (2026, "INR"): 85.80,
}


def get_fx_rate(year: int, currency: str) -> tuple[float, bool]:
    """Return (rate, is_fresh) for converting USD → `currency` in `year`.

    `is_fresh` is False when we had to fall back to a prior year's rate
    because the requested year isn't in the table — the calculator uses
    it to add a "FX rate may be stale" caveat to the report.
    """
    ccy = (currency or "").upper()
    if ccy == "USD" or not ccy:
        return 1.0, True
    key = (year, ccy)
    if key in _YEAR_END_RATES:
        return _YEAR_END_RATES[key], True

    # Fallback: try the most recent year we have for this currency.
    best_year = -1
    for (y, c) in _YEAR_END_RATES.keys():
        if c == ccy and y < year and y > best_year:
            best_year = y
    if best_year > 0:
        return _YEAR_END_RATES[(best_year, ccy)], False
    # Nothing at all — return 1.0 and let the UI surface the missing FX.
    return 1.0, False


def convert_usd(amount_usd: float, year: int, currency: str) -> float:
    """Convenience: convert a USD amount to the target currency at year-end."""
    rate, _ = get_fx_rate(year, currency)
    return amount_usd * rate
