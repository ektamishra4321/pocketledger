"""PocketLedger deterministic test suite. Run: python -m pytest"""

import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from categorizer import categorize, normalize_merchant
from db import Ledger, make_dedupe_hash, normalize_date, paise_to_rupees_str, rupees_to_paise
from importer import import_csv_text
from recurring import find_recurring


@pytest.fixture()
def ledger(tmp_path):
    lg = Ledger(db_path=str(tmp_path / "test.db"))
    yield lg
    lg.close()


# ---------------- money math ----------------

def test_rupees_to_paise_basic():
    assert rupees_to_paise(450) == 45000
    assert rupees_to_paise("129.50") == 12950
    assert rupees_to_paise("1,299.99") == 129999
    assert rupees_to_paise("\u20b9500") == 50000

def test_rupees_to_paise_negative_and_accounting():
    assert rupees_to_paise("-250") == -25000
    assert rupees_to_paise("(250.00)") == -25000

def test_rupees_to_paise_rejects_garbage():
    with pytest.raises(ValueError):
        rupees_to_paise("abc")

def test_paise_to_rupees_str():
    assert paise_to_rupees_str(45000) == "\u20b9450.00"
    assert paise_to_rupees_str(-12950) == "-\u20b9129.50"
    assert paise_to_rupees_str(5) == "\u20b90.05"


# ---------------- dates ----------------

def test_normalize_date_formats():
    assert normalize_date("2026-07-18") == "2026-07-18"
    assert normalize_date("18-07-2026") == "2026-07-18"
    assert normalize_date("18/07/2026") == "2026-07-18"
    assert normalize_date("18-Jul-2026") == "2026-07-18"

def test_normalize_date_relative():
    assert normalize_date("today") == date.today().isoformat()
    assert normalize_date("yesterday") == (date.today() - timedelta(days=1)).isoformat()

def test_normalize_date_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_date("sometime last week")


# ---------------- categorizer ----------------

def test_categorize_common_merchants():
    assert categorize("Swiggy") == "food"
    assert categorize("UPI-ZEPTO-12345@ybl") == "groceries"
    assert categorize("Uber rides") == "transport"
    assert categorize("NETFLIX.COM") == "entertainment"
    assert categorize("AMAZON PAY INDIA") == "shopping"

def test_categorize_fallback():
    assert categorize("Random Unknown Shop 77") == "uncategorized"

def test_normalize_merchant_strips_upi_noise():
    assert normalize_merchant("UPI-SWIGGY-987654321@ybl") == "swiggy"
    assert "blinkit" in normalize_merchant("POS BLINKIT COMMERCE 000012345678")


# ---------------- ledger + dedupe ----------------

def test_add_and_query(ledger):
    _, ok = ledger.add_transaction("2026-07-01", 45000, "swiggy", category="food")
    assert ok
    rows = ledger.query(category="food")
    assert len(rows) == 1
    assert rows[0]["amount_paise"] == 45000

def test_duplicate_rejected(ledger):
    ledger.add_transaction("2026-07-01", 45000, "swiggy", note="lunch")
    _, ok = ledger.add_transaction("2026-07-01", 45000, "swiggy", note="lunch")
    assert not ok

def test_dedupe_hash_stability():
    assert make_dedupe_hash("2026-07-01", 45000, "Swiggy") == make_dedupe_hash("2026-07-01", 45000, "swiggy ")

def test_monthly_summary_math(ledger):
    ledger.add_transaction("2026-07-01", 45000, "swiggy", category="food")
    ledger.add_transaction("2026-07-05", 30000, "zomato", category="food")
    ledger.add_transaction("2026-07-10", 20000, "uber", category="transport")
    ledger.add_transaction("2026-07-15", -500000, "salary", category="income")  # excluded
    assert ledger.monthly_total("2026-07") == 95000
    cats = ledger.monthly_by_category("2026-07")
    assert cats[0]["category"] == "food" and cats[0]["total_paise"] == 75000

def test_month_bounds_december(ledger):
    assert ledger.month_bounds("2026-12") == ("2026-12-01", "2026-12-31")


# ---------------- budgets ----------------

def test_budget_upsert(ledger):
    ledger.set_budget("food", 600000)
    ledger.set_budget("Food", 500000)  # update, case-insensitive
    budgets = ledger.budgets()
    assert len(budgets) == 1
    assert budgets[0]["monthly_limit_paise"] == 500000


# ---------------- importer ----------------

CSV_DEBIT_CREDIT = """Account Statement for XXXX1234
Date,Narration,Debit,Credit,Balance
01-07-2026,UPI-SWIGGY-123456@ybl,450.00,,10000.00
02-07-2026,NEFT-SALARY JULY,,50000.00,60000.00
03-07-2026,POS AMAZON PAY INDIA,1299.00,,58701.00
"""

CSV_SINGLE_AMOUNT = """Transaction Date,Description,Amount,Type
2026-07-04,NETFLIX.COM,649.00,DR
2026-07-05,REFUND FLIPKART,300.00,CR
"""

def test_import_debit_credit_shape(ledger):
    res = import_csv_text(CSV_DEBIT_CREDIT, ledger)
    assert res.imported == 3 and res.errors == []
    rows = ledger.query(limit=10)
    swiggy = [r for r in rows if r["merchant"] == "swiggy"][0]
    assert swiggy["amount_paise"] == 45000 and swiggy["category"] == "food"
    salary = [r for r in rows if "salary" in r["note"].lower()][0]
    assert salary["amount_paise"] == -5000000 and salary["category"] == "income"

def test_import_single_amount_shape(ledger):
    res = import_csv_text(CSV_SINGLE_AMOUNT, ledger)
    assert res.imported == 2
    netflix = ledger.query(merchant_like="netflix")[0]
    assert netflix["amount_paise"] == 64900 and netflix["category"] == "entertainment"
    refund = ledger.query(merchant_like="flipkart")[0]
    assert refund["amount_paise"] == -30000

def test_reimport_is_noop(ledger):
    import_csv_text(CSV_DEBIT_CREDIT, ledger)
    res = import_csv_text(CSV_DEBIT_CREDIT, ledger)
    assert res.imported == 0 and res.duplicates == 3

def test_import_bad_csv_reports_error(ledger):
    res = import_csv_text("just,some,random\n1,2,3\n", ledger)
    assert res.imported == 0 and res.errors


# ---------------- recurring ----------------

class FakeRow(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k)

def _rows(entries):
    return [FakeRow(txn_date=d, amount_paise=a, merchant=m) for d, a, m in entries]

def test_recurring_monthly_detected():
    rows = _rows([("2026-04-05", 64900, "netflix"),
                  ("2026-05-05", 64900, "netflix"),
                  ("2026-06-04", 64900, "netflix"),
                  ("2026-07-05", 64900, "netflix")])
    found = find_recurring(rows)
    assert len(found) == 1
    c = found[0]
    assert c.cadence == "monthly" and c.merchant == "netflix"
    assert c.monthly_cost_paise == 64900

def test_recurring_ignores_irregular():
    rows = _rows([("2026-04-05", 45000, "swiggy"),
                  ("2026-04-06", 82000, "swiggy"),
                  ("2026-04-20", 12000, "swiggy"),
                  ("2026-05-02", 99000, "swiggy")])
    assert find_recurring(rows) == []

def test_recurring_needs_min_occurrences():
    rows = _rows([("2026-06-01", 19900, "spotify"), ("2026-07-01", 19900, "spotify")])
    assert find_recurring(rows) == []

def test_recurring_weekly_normalizes_monthly_cost():
    rows = _rows([("2026-07-01", 10000, "gym"), ("2026-07-08", 10000, "gym"),
                  ("2026-07-15", 10000, "gym"), ("2026-07-22", 10000, "gym")])
    found = find_recurring(rows)
    assert found[0].cadence == "weekly"
    assert found[0].monthly_cost_paise == 43300  # 100.00 * 4.33


# ---------------- server tools (end-to-end, no MCP client needed) ----------------

def test_server_tools_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("POCKETLEDGER_DB", str(tmp_path / "srv.db"))
    import server
    server._ledger = None  # force fresh ledger with the env DB

    out = json.loads(server.pocketledger_log_expense(
        server.LogExpenseInput(amount=450, merchant="Swiggy", date="2026-07-01")))
    assert out["status"] == "logged" and out["category"] == "food"

    dup = json.loads(server.pocketledger_log_expense(
        server.LogExpenseInput(amount=450, merchant="Swiggy", date="2026-07-01")))
    assert dup["status"] == "duplicate"

    json.loads(server.pocketledger_set_budget(
        server.SetBudgetInput(category="food", monthly_limit=500)))
    budgets = json.loads(server.pocketledger_check_budgets(
        server.CheckBudgetsInput(month="2026-07")))
    assert budgets["budgets"][0]["status"] == "WARNING"  # 450/500 = 90%

    summary = json.loads(server.pocketledger_monthly_summary(
        server.MonthlySummaryInput(month="2026-07")))
    assert summary["total_spent"] == "\u20b9450.00"
