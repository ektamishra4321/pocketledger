"""PocketLedger MCP server.

Personal finance tools exposed to Claude Desktop (or any MCP client) over stdio.
The LLM is the interface; every tool below is deterministic Python + SQLite.

Run directly for a smoke test:  python server.py --selftest
"""

from __future__ import annotations

import json
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from categorizer import RULES, categorize, normalize_merchant
from db import Ledger, normalize_date, paise_to_rupees_str, rupees_to_paise
from importer import import_csv_file
from recurring import find_recurring

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:  # pragma: no cover
    raise SystemExit("MCP SDK missing. Run: pip install \"mcp[cli]\"") from e

mcp = FastMCP("pocketledger_mcp")

_ledger: Ledger | None = None


def get_ledger() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger()
    return _ledger


def _txn_dict(r) -> dict:
    return {
        "id": r["id"], "date": r["txn_date"],
        "amount": paise_to_rupees_str(r["amount_paise"]),
        "merchant": r["merchant"], "category": r["category"],
        "note": r["note"], "source": r["source"],
    }


# ---------------------------------------------------------------- log_expense

class LogExpenseInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    amount: float = Field(..., description="Expense amount in rupees, e.g. 450 or 129.50", gt=0)
    merchant: str = Field(..., description="Who was paid, e.g. 'Swiggy', 'auto rickshaw'", min_length=1, max_length=120)
    category: Optional[str] = Field(default=None, description="Category like food/transport/rent. Leave empty to auto-categorize from merchant keywords.")
    note: Optional[str] = Field(default="", description="Optional free-text note", max_length=200)
    date: Optional[str] = Field(default="today", description="Date: YYYY-MM-DD, DD-MM-YYYY, 'today' or 'yesterday'")


@mcp.tool(
    name="pocketledger_log_expense",
    annotations={"title": "Log an expense", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def pocketledger_log_expense(params: LogExpenseInput) -> str:
    """Log a single expense to the local ledger. Auto-categorizes if no category
    is given (deterministic keyword rules). Duplicate-safe: identical
    date+amount+merchant+note is rejected as a duplicate."""
    ledger = get_ledger()
    txn_date = normalize_date(params.date or "today")
    paise = rupees_to_paise(params.amount)
    category = (params.category or "").lower().strip() or categorize(params.merchant, params.note or "")
    txn_id, inserted = ledger.add_transaction(
        txn_date, paise, normalize_merchant(params.merchant),
        note=params.note or "", category=category, source="manual",
    )
    if not inserted:
        return json.dumps({"status": "duplicate",
                           "message": "Identical transaction already exists; nothing added."})
    return json.dumps({"status": "logged", "id": txn_id, "date": txn_date,
                       "amount": paise_to_rupees_str(paise), "category": category,
                       "merchant": normalize_merchant(params.merchant)})


# ---------------------------------------------------------- import_statement

class ImportStatementInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    csv_path: str = Field(..., description="Absolute path to a bank/card statement CSV file on this machine, e.g. C:\\Users\\me\\Downloads\\statement.csv")


@mcp.tool(
    name="pocketledger_import_statement",
    annotations={"title": "Import a bank statement CSV", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def pocketledger_import_statement(params: ImportStatementInput) -> str:
    """Parse a bank/card statement CSV and import its transactions.
    Detects common Indian bank column layouts (Debit/Credit columns, or a single
    Amount column with DR/CR). Debits become expenses (auto-categorized), credits
    become income. Re-importing the same file adds nothing (hash dedupe)."""
    import os
    if not os.path.isfile(params.csv_path):
        return json.dumps({"status": "error",
                           "message": f"File not found: {params.csv_path}. "
                                      f"Give the full absolute path to the CSV."})
    res = import_csv_file(params.csv_path, get_ledger())
    return json.dumps({"status": "done", "imported": res.imported,
                       "duplicates_skipped": res.duplicates, "rows_skipped": res.skipped,
                       "errors": res.errors[:5]})


# ------------------------------------------------------------ query_expenses

class QueryExpensesInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    category: Optional[str] = Field(default=None, description="Filter by category, e.g. 'food'")
    start_date: Optional[str] = Field(default=None, description="Earliest date (YYYY-MM-DD)")
    end_date: Optional[str] = Field(default=None, description="Latest date (YYYY-MM-DD)")
    merchant: Optional[str] = Field(default=None, description="Substring match on merchant/note, e.g. 'swiggy'")
    limit: int = Field(default=25, description="Max rows to return", ge=1, le=200)


@mcp.tool(
    name="pocketledger_query_expenses",
    annotations={"title": "Query transactions", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def pocketledger_query_expenses(params: QueryExpensesInput) -> str:
    """Search transactions by category, date range, and/or merchant substring.
    Returns newest first, plus the sum of matched expenses."""
    ledger = get_ledger()
    sd = normalize_date(params.start_date) if params.start_date else None
    ed = normalize_date(params.end_date) if params.end_date else None
    rows = ledger.query(category=params.category, start_date=sd, end_date=ed,
                        merchant_like=params.merchant, limit=params.limit)
    total = sum(r["amount_paise"] for r in rows if r["amount_paise"] > 0)
    return json.dumps({"count": len(rows),
                       "expense_total_of_shown": paise_to_rupees_str(total),
                       "transactions": [_txn_dict(r) for r in rows]})


# ----------------------------------------------------------- monthly_summary

class MonthlySummaryInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    month: str = Field(..., description="Month as YYYY-MM, e.g. '2026-07'",
                       pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


@mcp.tool(
    name="pocketledger_monthly_summary",
    annotations={"title": "Monthly spending summary", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def pocketledger_monthly_summary(params: MonthlySummaryInput) -> str:
    """Spending summary for a month: total, per-category breakdown, top merchants,
    and comparison with the previous month."""
    ledger = get_ledger()
    month = params.month
    y, m = map(int, month.split("-"))
    prev = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"

    total = ledger.monthly_total(month)
    prev_total = ledger.monthly_total(prev)
    delta = total - prev_total
    cats = [{"category": r["category"], "total": paise_to_rupees_str(r["total_paise"]),
             "transactions": r["n"]} for r in ledger.monthly_by_category(month)]
    tops = [{"merchant": r["merchant"], "total": paise_to_rupees_str(r["total_paise"]),
             "visits": r["n"]} for r in ledger.top_merchants(month)]
    return json.dumps({
        "month": month, "total_spent": paise_to_rupees_str(total),
        "vs_previous_month": {"previous": paise_to_rupees_str(prev_total),
                              "change": paise_to_rupees_str(delta),
                              "direction": "more" if delta > 0 else ("less" if delta < 0 else "same")},
        "by_category": cats, "top_merchants": tops,
    })


# ---------------------------------------------------------------- set_budget

class SetBudgetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    category: str = Field(..., description="Category to budget, e.g. 'food'", min_length=1, max_length=50)
    monthly_limit: float = Field(..., description="Monthly limit in rupees, e.g. 6000", gt=0)


@mcp.tool(
    name="pocketledger_set_budget",
    annotations={"title": "Set a category budget", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def pocketledger_set_budget(params: SetBudgetInput) -> str:
    """Set (or update) a monthly spending limit for a category."""
    get_ledger().set_budget(params.category, rupees_to_paise(params.monthly_limit))
    return json.dumps({"status": "saved", "category": params.category.lower().strip(),
                       "monthly_limit": paise_to_rupees_str(rupees_to_paise(params.monthly_limit))})


# -------------------------------------------------------------- check_budgets

class CheckBudgetsInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    month: Optional[str] = Field(default=None, description="Month as YYYY-MM. Defaults to the current month.",
                                 pattern=r"^\d{4}-(0[1-9]|1[0-2])$")


@mcp.tool(
    name="pocketledger_check_budgets",
    annotations={"title": "Check budgets", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def pocketledger_check_budgets(params: CheckBudgetsInput) -> str:
    """Compare this month's spending against every budget. Flags categories that
    are over limit or above 80% of limit."""
    from datetime import date as _date
    ledger = get_ledger()
    month = params.month or _date.today().strftime("%Y-%m")
    spent_by_cat = {r["category"]: r["total_paise"] for r in ledger.monthly_by_category(month)}
    out = []
    for b in ledger.budgets():
        spent = spent_by_cat.get(b["category"], 0)
        limit = b["monthly_limit_paise"]
        pct = round(100 * spent / limit, 1) if limit else 0.0
        status = "OVER" if spent > limit else ("WARNING" if pct >= 80 else "ok")
        out.append({"category": b["category"], "limit": paise_to_rupees_str(limit),
                    "spent": paise_to_rupees_str(spent), "used_pct": pct, "status": status})
    if not out:
        return json.dumps({"month": month, "budgets": [],
                           "message": "No budgets set yet. Use pocketledger_set_budget first."})
    return json.dumps({"month": month, "budgets": out})


# ------------------------------------------------------------- find_recurring

class FindRecurringInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="pocketledger_find_recurring",
    annotations={"title": "Find recurring charges", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def pocketledger_find_recurring(params: FindRecurringInput) -> str:
    """Detect subscriptions and recurring charges (weekly/monthly/quarterly/yearly)
    from transaction history using deterministic amount+interval analysis.
    Shows each one's normalized per-month cost, biggest first."""
    charges = find_recurring(get_ledger().all_expense_txns())
    total_monthly = sum(c.monthly_cost_paise for c in charges)
    return json.dumps({
        "recurring_count": len(charges),
        "estimated_total_monthly_cost": paise_to_rupees_str(total_monthly),
        "charges": [{"merchant": c.merchant, "cadence": c.cadence,
                     "typical_amount": paise_to_rupees_str(c.typical_amount_paise),
                     "occurrences": c.occurrences, "last_seen": c.last_date,
                     "monthly_equivalent": paise_to_rupees_str(c.monthly_cost_paise)}
                    for c in charges],
    })


# --------------------------------------------------------------- recategorize

class RecategorizeInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    transaction_id: int = Field(..., description="The transaction id (from query results)", ge=1)
    category: str = Field(..., description="New category", min_length=1, max_length=50)


@mcp.tool(
    name="pocketledger_recategorize",
    annotations={"title": "Fix a transaction's category", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def pocketledger_recategorize(params: RecategorizeInput) -> str:
    """Change the category of an existing transaction by id."""
    ok = get_ledger().recategorize(params.transaction_id, params.category)
    if not ok:
        return json.dumps({"status": "error",
                           "message": f"No transaction with id {params.transaction_id}. "
                                      f"Use pocketledger_query_expenses to find ids."})
    return json.dumps({"status": "updated", "id": params.transaction_id,
                       "category": params.category.lower().strip()})


# ------------------------------------------------------------ list_categories

class ListCategoriesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


@mcp.tool(
    name="pocketledger_list_categories",
    annotations={"title": "List known categories", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def pocketledger_list_categories(params: ListCategoriesInput) -> str:
    """List all categories the auto-categorizer knows, with example keywords."""
    return json.dumps({"categories": [
        {"category": cat, "example_keywords": kws[:4]} for cat, kws in RULES
    ] + [{"category": "uncategorized", "example_keywords": ["(fallback)"]}]})


def _selftest() -> None:
    import os, tempfile
    os.environ["POCKETLEDGER_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
    print(pocketledger_log_expense(LogExpenseInput(amount=450, merchant="Swiggy")))
    print(pocketledger_monthly_summary(MonthlySummaryInput(
        month=__import__("datetime").date.today().strftime("%Y-%m"))))
    print("SELFTEST OK")


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        _selftest()
    else:
        mcp.run()  # stdio transport for Claude Desktop
