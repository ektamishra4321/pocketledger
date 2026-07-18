"""PocketLedger database layer.

All money math and storage is deterministic SQLite. No ML anywhere in this file:
the layers that judge the money contain no ML.

Amounts are stored in integer paise (1 INR = 100 paise) to avoid float errors.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

DEFAULT_DB_PATH = str(Path.home() / ".pocketledger" / "pocketledger.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_date TEXT NOT NULL,              -- ISO YYYY-MM-DD
    amount_paise INTEGER NOT NULL,       -- positive = expense, negative = income/credit
    merchant TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'uncategorized',
    source TEXT NOT NULL DEFAULT 'manual',  -- 'manual' | 'statement'
    dedupe_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS idx_txn_category ON transactions(category);

CREATE TABLE IF NOT EXISTS budgets (
    category TEXT PRIMARY KEY,
    monthly_limit_paise INTEGER NOT NULL
);
"""


def rupees_to_paise(amount: float | int | str) -> int:
    """Convert a rupee amount to integer paise, safely via string decimal math."""
    s = str(amount).strip().replace(",", "").replace("\u20b9", "")
    if s.startswith("(") and s.endswith(")"):  # accounting negatives
        s = "-" + s[1:-1]
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if not s or s == ".":
        raise ValueError(f"Not a valid amount: {amount!r}")
    whole, _, frac = s.partition(".")
    whole = whole or "0"
    frac = (frac + "00")[:2]
    if not whole.isdigit() or not frac.isdigit():
        raise ValueError(f"Not a valid amount: {amount!r}")
    paise = int(whole) * 100 + int(frac)
    return -paise if neg else paise


def paise_to_rupees_str(paise: int) -> str:
    sign = "-" if paise < 0 else ""
    p = abs(paise)
    return f"{sign}\u20b9{p // 100}.{p % 100:02d}"


def make_dedupe_hash(txn_date: str, amount_paise: int, merchant: str, note: str = "") -> str:
    raw = f"{txn_date}|{amount_paise}|{merchant.lower().strip()}|{note.lower().strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def normalize_date(value: str) -> str:
    """Accept common Indian bank/user date formats, return ISO YYYY-MM-DD."""
    value = value.strip()
    if value.lower() in ("today", ""):
        return date.today().isoformat()
    if value.lower() == "yesterday":
        from datetime import timedelta
        return (date.today() - timedelta(days=1)).isoformat()
    fmts = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y",
            "%d-%b-%y", "%d/%m/%y", "%d.%m.%Y", "%Y/%m/%d"]
    for fmt in fmts:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(
        f"Unrecognized date: {value!r}. Use YYYY-MM-DD, DD-MM-YYYY, DD/MM/YYYY, "
        f"'today' or 'yesterday'."
    )


class Ledger:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.environ.get("POCKETLEDGER_DB", DEFAULT_DB_PATH)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the web chat (Flask) serves each request on a
        # different thread. Writes are serialized through self._lock.
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(SCHEMA)

    # ---------- writes ----------

    def add_transaction(self, txn_date: str, amount_paise: int, merchant: str,
                        note: str = "", category: str = "uncategorized",
                        source: str = "manual") -> tuple[int | None, bool]:
        """Insert a transaction. Returns (row_id, inserted). Duplicate → (None, False)."""
        h = make_dedupe_hash(txn_date, amount_paise, merchant, note)
        try:
            with self._lock:
                cur = self.conn.execute(
                    "INSERT INTO transactions (txn_date, amount_paise, merchant, note, "
                    "category, source, dedupe_hash) VALUES (?,?,?,?,?,?,?)",
                    (txn_date, amount_paise, merchant, note, category, source, h),
                )
                self.conn.commit()
            return cur.lastrowid, True
        except sqlite3.IntegrityError:
            return None, False

    def set_budget(self, category: str, monthly_limit_paise: int) -> None:
        with self._lock:
            self._set_budget_locked(category, monthly_limit_paise)

    def _set_budget_locked(self, category: str, monthly_limit_paise: int) -> None:
        self.conn.execute(
            "INSERT INTO budgets (category, monthly_limit_paise) VALUES (?,?) "
            "ON CONFLICT(category) DO UPDATE SET monthly_limit_paise=excluded.monthly_limit_paise",
            (category.lower().strip(), monthly_limit_paise),
        )
        self.conn.commit()

    def recategorize(self, txn_id: int, category: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE transactions SET category=? WHERE id=?", (category.lower().strip(), txn_id)
            )
            self.conn.commit()
        return cur.rowcount > 0

    # ---------- reads ----------

    def query(self, category: str | None = None, start_date: str | None = None,
              end_date: str | None = None, merchant_like: str | None = None,
              limit: int = 50) -> list[sqlite3.Row]:
        sql = "SELECT * FROM transactions WHERE 1=1"
        args: list = []
        if category:
            sql += " AND category = ?"
            args.append(category.lower().strip())
        if start_date:
            sql += " AND txn_date >= ?"
            args.append(start_date)
        if end_date:
            sql += " AND txn_date <= ?"
            args.append(end_date)
        if merchant_like:
            sql += " AND (merchant LIKE ? OR note LIKE ?)"
            args.extend([f"%{merchant_like}%", f"%{merchant_like}%"])
        sql += " ORDER BY txn_date DESC, id DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        return self.conn.execute(sql, args).fetchall()

    def month_bounds(self, month: str) -> tuple[str, str]:
        """month = 'YYYY-MM' → (first_day, last_day) ISO strings."""
        y, m = month.split("-")
        y, m = int(y), int(m)
        start = date(y, m, 1)
        end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        from datetime import timedelta
        return start.isoformat(), (end - timedelta(days=1)).isoformat()

    def monthly_by_category(self, month: str) -> list[sqlite3.Row]:
        start, end = self.month_bounds(month)
        return self.conn.execute(
            "SELECT category, SUM(amount_paise) AS total_paise, COUNT(*) AS n "
            "FROM transactions WHERE txn_date BETWEEN ? AND ? AND amount_paise > 0 "
            "GROUP BY category ORDER BY total_paise DESC",
            (start, end),
        ).fetchall()

    def monthly_total(self, month: str) -> int:
        start, end = self.month_bounds(month)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount_paise),0) AS t FROM transactions "
            "WHERE txn_date BETWEEN ? AND ? AND amount_paise > 0",
            (start, end),
        ).fetchone()
        return row["t"]

    def top_merchants(self, month: str, limit: int = 5) -> list[sqlite3.Row]:
        start, end = self.month_bounds(month)
        return self.conn.execute(
            "SELECT merchant, SUM(amount_paise) AS total_paise, COUNT(*) AS n "
            "FROM transactions WHERE txn_date BETWEEN ? AND ? AND amount_paise > 0 "
            "AND merchant != '' GROUP BY merchant ORDER BY total_paise DESC LIMIT ?",
            (start, end, limit),
        ).fetchall()

    def budgets(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM budgets ORDER BY category").fetchall()

    def all_expense_txns(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM transactions WHERE amount_paise > 0 ORDER BY merchant, txn_date"
        ).fetchall()

    def close(self):
        self.conn.close()
