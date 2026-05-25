"""Per-country tax rules for the report.

Crypto tax law is a moving target and this is NOT legal advice — every
number here is a sensible default that matches the public guidance of
the relevant tax authority as of the time this file was written. The
calculator uses these rules to classify disposals (short vs long term),
filter allowed methods, and apply jurisdiction-specific flags in the
rendered report.

When a user picks a jurisdiction in the UI we:
  1. Validate their chosen cost basis method is in `allowed_methods`.
  2. Compute holding period and apply `long_term_days` to flag each
     disposal long- or short-term.
  3. Apply `long_term_discount_pct` when summing capital gains (AU uses
     50%; DE drops long-term gains to 0% after the 1-year hold, etc.).
  4. Surface any `caveats` verbatim in the PDF header so the user sees
     the fine print before filing.

No jurisdiction's full tax code fits in a data class, so we keep the
per-country logic narrow (classification + discount) and put the actual
nuance in the caveat strings. The user reads them; our job is accurate
math against clear rules, not pretending to be a CPA.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Jurisdiction:
    code: str                       # ISO-ish short code (US, GB, SG, ...)
    name: str                       # human label
    currency: str                   # the tax authority's currency (for the UI label)
    long_term_days: int | None      # held ≥ this many days = long term. None = no LT concept.
    long_term_discount_pct: float   # how much of the LT gain is taxable (1.0 = full, 0.5 = 50% CGT discount)
    default_method: str             # method to pre-select in the picker
    allowed_methods: list[str]      # methods accepted by this jurisdiction's tax office
    loss_offset_allowed: bool       # can losses offset gains? (India: no)
    has_capital_gains: bool         # some places tax crypto as income not CG
    tax_year_end: tuple[int, int]   # (month, day) — when the tax year closes
    caveats: list[str] = field(default_factory=list)  # legal disclaimers


# The list below is deliberately conservative. Each entry has a short
# docstring in the caveats explaining the nuance the calculator can't
# fully model, so the rendered PDF always tells the user what to check.

US = Jurisdiction(
    code="US",
    name="United States",
    currency="USD",
    long_term_days=366,  # IRS: "more than one year"
    long_term_discount_pct=1.0,  # long-term still taxed, just at lower rate
    default_method="FIFO",
    allowed_methods=["FIFO", "LIFO", "HIFO", "SPEC", "AVG"],
    loss_offset_allowed=True,
    has_capital_gains=True,
    tax_year_end=(12, 31),
    caveats=[
        "Rev. Proc. 2024-28 requires per-wallet cost basis tracking "
        "from Jan 1, 2025 onward. This report aggregates across wallets "
        "by default — for post-2025 filings you may need to re-run "
        "per-wallet.",
        "Long-term holding requires >1 year (366+ days). Short-term "
        "gains are taxed as ordinary income.",
        "Form 8949 + Schedule D is the standard filing. Use the included "
        "Form 8949 CSV export when preparing your return.",
    ],
)

UK = Jurisdiction(
    code="GB",
    name="United Kingdom",
    currency="GBP",
    long_term_days=None,  # no short/long distinction in UK CGT
    long_term_discount_pct=1.0,
    default_method="AVG",  # Section 104 pool = weighted average
    allowed_methods=["AVG"],  # UK basically mandates Section 104 pooling
    loss_offset_allowed=True,
    has_capital_gains=True,
    tax_year_end=(4, 5),  # UK tax year ends April 5
    caveats=[
        "UK HMRC requires Section 104 pooling (weighted average cost) "
        "for crypto. Other methods are not accepted — the picker locks "
        "to AVG for UK.",
        "Same-day rule: acquisitions and disposals on the SAME day are "
        "matched first. Bed-and-breakfasting rule: disposals matched "
        "against acquisitions within the next 30 days take priority "
        "over the s104 pool. This report does NOT yet model those "
        "matching rules — for high-frequency trading, consult an "
        "accountant.",
        "Annual CGT allowance (£3,000 for 2024/25) is NOT subtracted — "
        "the totals shown are GROSS gains before any allowance.",
        "Tax year ends April 5, not December 31.",
    ],
)

SG = Jurisdiction(
    code="SG",
    name="Singapore",
    currency="SGD",
    long_term_days=None,
    long_term_discount_pct=0.0,  # no CGT on crypto for individuals
    default_method="FIFO",
    allowed_methods=["FIFO", "LIFO", "HIFO", "AVG"],
    loss_offset_allowed=False,
    has_capital_gains=False,  # Singapore has no capital gains tax
    tax_year_end=(12, 31),
    caveats=[
        "Singapore has NO capital gains tax for individual investors. "
        "If you're holding crypto as an investment (not as a trading "
        "business) the gains shown here are for your records only.",
        "If IRAS treats you as trading in a revenue nature (frequent, "
        "systematic, with profit intent) the gains become TAXABLE as "
        "ordinary income — this report still gives you the per-lot "
        "breakdown you'd need either way.",
        "GST on crypto was removed in 2020 — no indirect tax concern.",
    ],
)

DE = Jurisdiction(
    code="DE",
    name="Germany",
    currency="EUR",
    long_term_days=366,  # >1 year holding = tax free
    long_term_discount_pct=0.0,  # 0% tax rate on long-term
    default_method="FIFO",
    allowed_methods=["FIFO"],  # §23 EStG is FIFO-only
    loss_offset_allowed=True,
    has_capital_gains=True,
    tax_year_end=(12, 31),
    caveats=[
        "Germany treats crypto held >1 year as tax-free (§23 EStG). "
        "Short-term gains are added to your income tax bracket — the "
        "calculator reports the gross amount, your personal rate "
        "applies on top.",
        "The €1,000 annual Freigrenze (allowance) is NOT subtracted.",
        "Staking / lending RESETS the 1-year clock on the deposited "
        "coins in most interpretations. This report does not yet model "
        "staking resets — manually review staked positions.",
    ],
)

CA = Jurisdiction(
    code="CA",
    name="Canada",
    currency="CAD",
    long_term_days=None,
    long_term_discount_pct=1.0,
    default_method="AVG",  # CRA requires ACB (adjusted cost basis)
    allowed_methods=["AVG"],
    loss_offset_allowed=True,
    has_capital_gains=True,
    tax_year_end=(12, 31),
    caveats=[
        "Canada CRA mandates the Adjusted Cost Basis (ACB) method — "
        "a weighted average across all your identical holdings. The "
        "picker locks to AVG for Canada.",
        "Only 50% of capital gains are taxable in Canada for amounts "
        "up to $250k/year; this report shows the FULL gain. Multiply "
        "by 0.5 (or 0.667 for the portion above $250k post-June 2024) "
        "when filing.",
        "Superficial loss rule: losses on assets re-acquired within "
        "30 days before/after the sale are denied. Not yet modelled.",
    ],
)

AU = Jurisdiction(
    code="AU",
    name="Australia",
    currency="AUD",
    long_term_days=366,  # 12-month CGT discount
    long_term_discount_pct=0.5,  # 50% discount for long-term holders
    default_method="FIFO",
    allowed_methods=["FIFO", "LIFO", "HIFO", "SPEC"],
    loss_offset_allowed=True,
    has_capital_gains=True,
    tax_year_end=(6, 30),  # Australian tax year: July 1 – June 30
    caveats=[
        "Australia applies a 50% CGT discount for assets held >12 "
        "months. This report already applies the discount to long-term "
        "gains shown in the summary — the per-disposal rows show the "
        "undiscounted numbers for auditability.",
        "Tax year ends June 30.",
        "ATO does accept specific identification but requires records "
        "at time of sale — retroactive spec-ID may be challenged.",
    ],
)

JP = Jurisdiction(
    code="JP",
    name="Japan",
    currency="JPY",
    long_term_days=None,
    long_term_discount_pct=1.0,
    default_method="AVG",  # 総平均法 (total average) is NTA default
    allowed_methods=["AVG", "FIFO"],  # 移動平均法 is also allowed
    loss_offset_allowed=False,  # crypto losses can't offset other income
    has_capital_gains=True,
    tax_year_end=(12, 31),
    caveats=[
        "Japan taxes crypto as MISCELLANEOUS INCOME, not capital gains. "
        "The rate is progressive up to 55% — much higher than for "
        "stocks. The calculator shows the gain; your marginal rate "
        "applies on top.",
        "Losses on crypto CANNOT be offset against other income or "
        "carried forward. Only gains are reported.",
        "NTA default is 総平均法 (total average method). 移動平均法 "
        "(moving average / FIFO-like) is also permitted if elected.",
    ],
)

IN = Jurisdiction(
    code="IN",
    name="India",
    currency="INR",
    long_term_days=None,
    long_term_discount_pct=1.0,
    default_method="FIFO",
    allowed_methods=["FIFO"],
    loss_offset_allowed=False,  # famously: no loss offset allowed
    has_capital_gains=True,
    tax_year_end=(3, 31),  # Indian fiscal year: April 1 – March 31
    caveats=[
        "India taxes crypto (Virtual Digital Assets) at a FLAT 30% "
        "regardless of holding period. No indexation, no slab rates.",
        "LOSSES on crypto CANNOT be offset against any other income, "
        "not even against other crypto gains — every gain is taxed at "
        "30%, losses are ignored. The report still shows losses for "
        "your records.",
        "A 1% TDS (tax deducted at source) applies to every sale over "
        "₹10,000. Not modelled here — check your exchange statements.",
        "Indian tax year ends March 31.",
    ],
)

# Registry the rest of the code imports.
JURISDICTIONS: dict[str, Jurisdiction] = {
    j.code: j for j in [US, UK, SG, DE, CA, AU, JP, IN]
}
