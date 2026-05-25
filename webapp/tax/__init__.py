"""Tax reporting package.

Converts a user's transaction history into per-jurisdiction tax reports
with multiple cost basis methods (FIFO / LIFO / HIFO / Average / Spec-ID).

Public surface:
    from tax import compute_tax_report, compare_methods, METHODS, JURISDICTIONS

Paid feature — unlocked per-user via a one-time USDC payment recorded in
the `tax_unlocks` table. The report computation itself is cheap (pure
Python walk of the user's own transaction list) so the "cost" is purely
the packaging + jurisdiction logic.
"""
from .methods import METHODS, compute_disposals, Disposal, Lot
from .jurisdictions import JURISDICTIONS, Jurisdiction
from .calculator import compute_tax_report, compare_methods, TaxReport
from .lots_view import build_sell_lot_snapshots

__all__ = [
    "METHODS",
    "compute_disposals",
    "Disposal",
    "Lot",
    "JURISDICTIONS",
    "Jurisdiction",
    "compute_tax_report",
    "compare_methods",
    "TaxReport",
    "build_sell_lot_snapshots",
]
