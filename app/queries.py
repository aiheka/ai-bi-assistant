"""Deterministic, parameterised SQL for each Intent.

Only whitelisted column names are ever interpolated into SQL text (never user
text); all values go through `?` placeholders. A final `assert_select_only`
guard runs on every statement as defence in depth.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from .errors import AssistantError
from .intents import DIMENSIONS, Intent

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|pragma|vacuum|reindex)\b", re.I)


def assert_select_only(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped or not stripped.lower().startswith("select") or _FORBIDDEN.search(stripped):
        raise AssistantError("unsafe_query", "Blocked a non-SELECT statement.", status=500)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[dict]
    queries: list[dict]                       # what was executed (label, sql, params)
    summary: dict = field(default_factory=dict)
    check: dict = field(default_factory=dict)  # flat {key: number} compared by the verifier


def _run(conn: sqlite3.Connection, label: str, sql: str, params: list, log: list) -> list[dict]:
    assert_select_only(sql)
    log.append({"label": label, "sql": " ".join(sql.split()), "params": params})
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _where(intent: Intent) -> tuple[str, list]:
    clauses, params = [], []
    for dim, value in intent.filters.items():
        clauses.append(f"{DIMENSIONS[dim]} = ?")
        params.append(value)
    if intent.months:
        clauses.append(f"month IN ({','.join('?' * len(intent.months))})")
        params += intent.months
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _pct(new: float, old: float):
    return None if not old else round((new - old) / old * 100, 2)


def total_revenue(conn, intent: Intent) -> QueryResult:
    where, params = _where(intent)
    log: list = []
    row = _run(conn, "Revenue totals",
               "SELECT COALESCE(SUM(gross_amount),0) AS gross_sales, "
               "COALESCE(SUM(discount_amount),0) AS discounts, "
               "COALESCE(SUM(net_revenue),0) AS net_revenue, "
               "COUNT(*) AS orders, COALESCE(SUM(quantity),0) AS units "
               f"FROM revenue_sales{where}", params, log)[0]
    return QueryResult(list(row), [row], log, summary=row, check={"total": row["net_revenue"]})


def ranking(conn, intent: Intent) -> QueryResult:
    col = DIMENSIONS[intent.dimension]
    where, params = _where(intent)
    sort = {"revenue": "revenue", "units": "units", "orders": "orders"}[intent.metric]
    direction = "ASC" if intent.order == "asc" else "DESC"
    log: list = []
    rows = _run(conn, f"Ranking by {intent.dimension}",
                f"SELECT {col} AS label, SUM(net_revenue) AS revenue, SUM(quantity) AS units, "
                f"COUNT(*) AS orders FROM revenue_sales{where} GROUP BY {col} "
                f"ORDER BY {sort} {direction}, label ASC", params, log)
    total = sum(r["revenue"] for r in rows)
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["share_pct"] = round(r["revenue"] / total * 100, 1) if total else 0.0
    return QueryResult(["rank", "label", "revenue", "units", "orders", "share_pct"], rows, log,
                       summary={"total_revenue": total},
                       check={r["label"]: r[intent.metric] for r in rows})


def monthly_trend(conn, intent: Intent) -> QueryResult:
    where, params = _where(intent)
    log: list = []
    rows = _run(conn, "Revenue by month",
                "SELECT month, SUM(net_revenue) AS revenue, SUM(quantity) AS units, COUNT(*) AS orders "
                f"FROM revenue_sales{where} GROUP BY month ORDER BY month", params, log)
    prev = None
    for r in rows:
        r["change_vs_prev_pct"] = None if prev is None else _pct(r["revenue"], prev)
        prev = r["revenue"]
    return QueryResult(["month", "revenue", "units", "orders", "change_vs_prev_pct"], rows, log,
                       summary={"total_revenue": sum(r["revenue"] for r in rows)},
                       check={r["month"]: r["revenue"] for r in rows})


def compare_months(conn, intent: Intent) -> QueryResult:
    a, b = intent.month_a, intent.month_b
    where, params = _where(intent)
    join = " AND " if where else " WHERE "
    log: list = []
    totals = _run(conn, "Revenue in each month",
                  "SELECT month, SUM(net_revenue) AS revenue, SUM(quantity) AS units, COUNT(*) AS orders "
                  f"FROM revenue_sales{where}{join}month IN (?, ?) GROUP BY month",
                  params + [a, b], log)
    by_month = {r["month"]: r for r in totals}
    rev_a = by_month.get(a, {}).get("revenue", 0.0)
    rev_b = by_month.get(b, {}).get("revenue", 0.0)
    drivers = _run(conn, "Change by product",
                   "SELECT product, "
                   "SUM(CASE WHEN month = ? THEN net_revenue ELSE 0 END) AS revenue_a, "
                   "SUM(CASE WHEN month = ? THEN net_revenue ELSE 0 END) AS revenue_b "
                   f"FROM revenue_sales{where}{join}month IN (?, ?) GROUP BY product "
                   "ORDER BY (revenue_b - revenue_a) DESC, product",
                   [a, b] + params + [a, b], log)
    for d in drivers:
        d["change"] = d["revenue_b"] - d["revenue_a"]
    summary = {"month_a": a, "month_b": b, "revenue_a": rev_a, "revenue_b": rev_b,
               "change": rev_b - rev_a, "change_pct": _pct(rev_b, rev_a)}
    return QueryResult(["product", "revenue_a", "revenue_b", "change"], drivers, log, summary=summary,
                       check={a: rev_a, b: rev_b})


def execute(conn: sqlite3.Connection, intent: Intent) -> QueryResult:
    if intent.name == "total_revenue":
        return total_revenue(conn, intent)
    if intent.name in ("top_n", "breakdown"):
        return ranking(conn, intent)
    if intent.name == "monthly_trend":
        return monthly_trend(conn, intent)
    if intent.name == "compare_months":
        return compare_months(conn, intent)
    raise AssistantError("unsupported_question", "Unsupported analysis.")


def data_quality(conn: sqlite3.Connection) -> dict:
    log: list = []
    counts = _run(conn, "Row counts",
                  "SELECT COUNT(*) AS valid_rows, "
                  "SUM(CASE WHEN status = 'Completed' THEN 1 ELSE 0 END) AS completed, "
                  "SUM(CASE WHEN status <> 'Completed' THEN 1 ELSE 0 END) AS not_completed, "
                  "COALESCE(SUM(CASE WHEN status <> 'Completed' THEN net_revenue END),0) AS excluded_value "
                  "FROM sales", [], log)[0]
    rejected = _run(conn, "Rejected rows",
                    "SELECT row_number, transaction_id, reasons FROM rejected_records ORDER BY row_number",
                    [], log)
    notRev = _run(conn, "Non-completed orders",
                  "SELECT transaction_id, status, net_revenue AS value FROM sales WHERE status <> 'Completed' "
                  "ORDER BY transaction_id", [], log)
    notes = _run(conn, "Load notes", "SELECT transaction_id, note FROM load_notes", [], log)
    return {"valid_rows": counts["valid_rows"], "counted_in_revenue": counts["completed"],
            "not_completed": notRev, "excluded_value": counts["excluded_value"],
            "rejected": rejected, "notes": notes, "queries": log}
