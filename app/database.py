"""Data management: CSV -> cleaned SQLite database + rejected-record audit trail.

Design rules (also documented in README):
  * Nothing is silently dropped. Every source row ends up in exactly one of
    `sales` (valid) or `rejected_records` (invalid, with the reason(s)).
  * `revenue_sales` is a VIEW over `sales` restricted to status = 'Completed'.
    All analytics read this view, so "Returned" orders can never leak into revenue.
  * Query connections are read-only at three levels: SQLite `mode=ro`,
    `PRAGMA query_only`, and a statement authorizer that only allows SELECT.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path

REQUIRED_COLUMNS = [
    "transaction_id", "order_date", "product", "category", "region",
    "sales_channel", "quantity", "unit_price", "discount_amount", "status",
]

SCHEMA = """
DROP VIEW  IF EXISTS revenue_sales;
DROP TABLE IF EXISTS sales;
DROP TABLE IF EXISTS rejected_records;
DROP TABLE IF EXISTS load_notes;

CREATE TABLE sales (
    transaction_id  TEXT PRIMARY KEY,
    order_date      TEXT NOT NULL,
    month           TEXT NOT NULL,
    product         TEXT NOT NULL,
    category        TEXT NOT NULL,
    region          TEXT NOT NULL,
    sales_channel   TEXT NOT NULL,
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    unit_price      REAL    NOT NULL CHECK (unit_price > 0),
    discount_amount REAL    NOT NULL CHECK (discount_amount >= 0),
    gross_amount    REAL    NOT NULL,
    net_revenue     REAL    NOT NULL,
    status          TEXT    NOT NULL
);
CREATE TABLE rejected_records (
    row_number     INTEGER,
    transaction_id TEXT,
    reasons        TEXT NOT NULL,
    raw_json       TEXT NOT NULL
);
CREATE TABLE load_notes (transaction_id TEXT, note TEXT);

CREATE VIEW revenue_sales AS SELECT * FROM sales WHERE status = 'Completed';
CREATE INDEX idx_sales_month ON sales(month);
"""


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _parse_date(text: str):
    # The file is day-first (25-01-2026 proves it). ISO is accepted too.
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _number(text: str):
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def load_csv(csv_path: str | Path, db_path: str | Path) -> dict:
    """(Re)build the database from the CSV. Returns a small load report."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("CSV file is empty")
        lookup = {name.strip().lower(): name for name in reader.fieldnames}
        missing = [c for c in REQUIRED_COLUMNS if c not in lookup]
        if missing:
            raise ValueError(f"CSV is missing required columns: {missing}")
        rows = [{c: row.get(lookup[c]) for c in REQUIRED_COLUMNS} for row in reader]

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    seen: set[str] = set()
    loaded = rejected = 0

    for number, raw in enumerate(rows, start=1):
        r = {k: _clean(v) for k, v in raw.items()}
        reasons: list[str] = []

        for col in ("transaction_id", "product", "category", "region", "sales_channel", "status"):
            if not r[col]:
                reasons.append(f"missing {col}")
        if r["transaction_id"] in seen:
            reasons.append("duplicate transaction_id")

        day = _parse_date(r["order_date"]) if r["order_date"] else None
        if day is None:
            reasons.append("missing order_date" if not r["order_date"]
                           else f"invalid order_date '{r['order_date']}'")

        qty = _number(r["quantity"]) if r["quantity"] else None
        if qty is None:
            reasons.append("missing or non-numeric quantity")
        elif qty <= 0 or qty != int(qty):
            reasons.append(f"invalid quantity {r['quantity']} (must be a positive whole number)")

        price = _number(r["unit_price"]) if r["unit_price"] else None
        if price is None:
            reasons.append("missing or non-numeric unit_price")
        elif price <= 0:
            reasons.append(f"invalid unit_price {r['unit_price']} (must be > 0)")

        note = None
        discount = 0.0
        if r["discount_amount"] == "":
            note = "discount_amount was blank; treated as 0"
        else:
            discount = _number(r["discount_amount"])
            if discount is None or discount < 0:
                reasons.append(f"invalid discount_amount '{r['discount_amount']}'")

        if not reasons and discount > qty * price:
            reasons.append("discount_amount exceeds gross amount")

        if reasons:
            rejected += 1
            conn.execute("INSERT INTO rejected_records VALUES (?,?,?,?)",
                         (number, r["transaction_id"] or None, "; ".join(reasons),
                          json.dumps(raw)))
            continue

        seen.add(r["transaction_id"])
        conn.execute(
            "INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (r["transaction_id"], day.isoformat(), day.strftime("%Y-%m"), r["product"],
             r["category"], r["region"], r["sales_channel"], int(qty), price, discount,
             qty * price, qty * price - discount, r["status"].capitalize()))
        if note:
            conn.execute("INSERT INTO load_notes VALUES (?,?)", (r["transaction_id"], note))
        loaded += 1

    conn.commit()
    conn.close()
    return {"source_rows": len(rows), "loaded": loaded, "rejected": rejected}


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Read-only connection with an authorizer that only permits SELECT."""
    conn = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}

    def authorizer(action, *_):
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)
    return conn
