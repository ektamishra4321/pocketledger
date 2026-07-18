"""Bank/card statement CSV importer.

Handles the two common Indian bank CSV shapes:
  A) separate Debit / Credit (or Withdrawal / Deposit) columns
  B) single Amount column, with either sign or a DR/CR type column

Header detection is fuzzy (banks love creative column names). Every parsed row
is deduped via a content hash, so re-importing the same file is a no-op.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from categorizer import categorize, normalize_merchant
from db import Ledger, normalize_date, rupees_to_paise

DATE_HEADERS = ["date", "txn date", "transaction date", "value date", "tran date", "post date"]
DESC_HEADERS = ["description", "narration", "particulars", "details", "transaction details",
                "remarks", "merchant", "transaction remarks"]
DEBIT_HEADERS = ["debit", "withdrawal", "withdrawal amt", "withdrawal amount", "dr amount",
                 "debit amount", "paid out"]
CREDIT_HEADERS = ["credit", "deposit", "deposit amt", "deposit amount", "cr amount",
                  "credit amount", "paid in"]
AMOUNT_HEADERS = ["amount", "transaction amount", "amount (inr)", "amt"]
TYPE_HEADERS = ["type", "dr/cr", "cr/dr", "transaction type", "dr / cr"]


@dataclass
class ImportResult:
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def _match_header(headers: list[str], candidates: list[str]) -> str | None:
    lower = {h.lower().strip(): h for h in headers}
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    for cand in candidates:  # substring fallback
        for lh, orig in lower.items():
            if cand in lh:
                return orig
    return None


def _parse_amount_cell(cell: str) -> int | None:
    cell = (cell or "").strip()
    if not cell or cell in ("-", "--", "NA", "N/A", "0", "0.0", "0.00"):
        return None
    try:
        paise = rupees_to_paise(cell)
        return paise if paise != 0 else None
    except ValueError:
        return None


def import_csv_text(text: str, ledger: Ledger) -> ImportResult:
    result = ImportResult()
    # skip bank preamble lines before the real header row
    lines = text.splitlines()
    header_idx = 0
    for i, line in enumerate(lines[:30]):
        low = line.lower()
        if any(d in low for d in ("date",)) and any(
            d in low for d in DESC_HEADERS + DEBIT_HEADERS + AMOUNT_HEADERS
        ):
            header_idx = i
            break
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    headers = reader.fieldnames or []
    if not headers:
        result.errors.append("No header row found in CSV.")
        return result

    h_date = _match_header(headers, DATE_HEADERS)
    h_desc = _match_header(headers, DESC_HEADERS)
    h_debit = _match_header(headers, DEBIT_HEADERS)
    h_credit = _match_header(headers, CREDIT_HEADERS)
    h_amount = _match_header(headers, AMOUNT_HEADERS)
    h_type = _match_header(headers, TYPE_HEADERS)

    if not h_date or not h_desc or not (h_debit or h_amount):
        result.errors.append(
            f"Could not detect required columns. Found headers: {headers}. "
            f"Need a date column, a description/narration column, and a "
            f"debit/amount column."
        )
        return result

    for row in reader:
        try:
            raw_date = (row.get(h_date) or "").strip()
            raw_desc = (row.get(h_desc) or "").strip()
            if not raw_date or not raw_desc:
                result.skipped += 1
                continue
            txn_date = normalize_date(raw_date)

            amount_paise: int | None = None
            if h_debit:  # shape A
                d = _parse_amount_cell(row.get(h_debit, ""))
                c = _parse_amount_cell(row.get(h_credit, "")) if h_credit else None
                if d is not None:
                    amount_paise = abs(d)
                elif c is not None:
                    amount_paise = -abs(c)
            else:  # shape B
                a = _parse_amount_cell(row.get(h_amount, ""))
                if a is None:
                    result.skipped += 1
                    continue
                if h_type:
                    t = (row.get(h_type) or "").strip().lower()
                    amount_paise = -abs(a) if t.startswith("cr") or "credit" in t else abs(a)
                else:
                    amount_paise = a  # trust the sign

            if amount_paise is None:
                result.skipped += 1
                continue

            merchant = normalize_merchant(raw_desc)
            category = categorize(raw_desc) if amount_paise > 0 else "income"
            _, inserted = ledger.add_transaction(
                txn_date, amount_paise, merchant, note=raw_desc[:200],
                category=category, source="statement",
            )
            if inserted:
                result.imported += 1
            else:
                result.duplicates += 1
        except Exception as e:  # keep going; report at the end
            result.errors.append(f"Row error ({raw_date} {raw_desc[:40]}): {e}")
    return result


def import_csv_file(path: str, ledger: Ledger) -> ImportResult:
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        return import_csv_text(f.read(), ledger)
