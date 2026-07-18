"""Deterministic recurring-charge detection.

A merchant is 'recurring' if it has >= MIN_OCCURRENCES expense transactions where:
  - amounts are within AMOUNT_TOLERANCE of the group's median amount, and
  - the median gap between consecutive dates falls in a known cadence window.

Pure arithmetic on dates and paise — auditable, no ML.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import median

MIN_OCCURRENCES = 3
AMOUNT_TOLERANCE = 0.12  # +/-12% of median

CADENCES = [
    ("weekly", 5, 9),
    ("monthly", 26, 35),
    ("quarterly", 85, 97),
    ("yearly", 350, 380),
]


@dataclass
class RecurringCharge:
    merchant: str
    cadence: str
    typical_amount_paise: int
    occurrences: int
    last_date: str
    monthly_cost_paise: int  # normalized to per-month for comparison


def _monthly_equivalent(cadence: str, amount_paise: int) -> int:
    factor = {"weekly": 4.33, "monthly": 1.0, "quarterly": 1 / 3, "yearly": 1 / 12}[cadence]
    return round(amount_paise * factor)


def find_recurring(rows) -> list[RecurringCharge]:
    """rows: iterable of sqlite3.Row with txn_date, amount_paise, merchant (expenses only)."""
    groups: dict[str, list[tuple[str, int]]] = {}
    for r in rows:
        m = (r["merchant"] or "").strip()
        if not m:
            continue
        groups.setdefault(m, []).append((r["txn_date"], r["amount_paise"]))

    found: list[RecurringCharge] = []
    for merchant, txns in groups.items():
        if len(txns) < MIN_OCCURRENCES:
            continue
        txns.sort()
        amounts = [a for _, a in txns]
        med_amt = median(amounts)
        if med_amt <= 0:
            continue
        stable = [(d, a) for d, a in txns if abs(a - med_amt) <= med_amt * AMOUNT_TOLERANCE]
        if len(stable) < MIN_OCCURRENCES:
            continue
        dates = [date.fromisoformat(d) for d, _ in stable]
        gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        if not gaps:
            continue
        med_gap = median(gaps)
        for cadence, lo, hi in CADENCES:
            if lo <= med_gap <= hi:
                typical = round(median(a for _, a in stable))
                found.append(RecurringCharge(
                    merchant=merchant,
                    cadence=cadence,
                    typical_amount_paise=typical,
                    occurrences=len(stable),
                    last_date=stable[-1][0],
                    monthly_cost_paise=_monthly_equivalent(cadence, typical),
                ))
                break
    found.sort(key=lambda c: -c.monthly_cost_paise)
    return found
