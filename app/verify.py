"""Independent cross-check of every answer.

The SQL path (database.py + queries.py) and this pandas path share NO code: this
module re-reads the raw CSV, applies the documented cleaning rules on its own,
and recomputes the headline figures. If the two disagree the API says so instead
of presenting a number as fact.
"""
from __future__ import annotations

from .intents import DIMENSIONS, Intent

try:
    import pandas as pd
except ImportError:  # verification is optional
    pd = None


def load_frame(csv_path: str):
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    df.columns = [c.strip().lower() for c in df.columns]
    for col in df.columns:
        df[col] = df[col].str.split().str.join(" ")
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["unit_price"] = pd.to_numeric(df["unit_price"], errors="coerce")
    df["discount_amount"] = pd.to_numeric(df["discount_amount"].replace("", "0"), errors="coerce")
    day = pd.to_datetime(df["order_date"], format="%d-%m-%Y", errors="coerce")
    df["month"] = day.dt.strftime("%Y-%m")
    df["gross"] = df["quantity"] * df["unit_price"]
    ok = (day.notna() & (df["quantity"] > 0) & (df["unit_price"] > 0)
          & (df["discount_amount"] >= 0) & (df["discount_amount"] <= df["gross"])
          & (df["quantity"] % 1 == 0) & ~df["transaction_id"].duplicated())
    df = df[ok].copy()
    df["net"] = df["gross"] - df["discount_amount"]
    return df[df["status"].str.lower() == "completed"]


def _subset(df, intent: Intent):
    for dim, value in intent.filters.items():
        df = df[df[DIMENSIONS[dim]] == value]
    if intent.months:
        df = df[df["month"].isin(intent.months)]
    return df


def recompute(df, intent: Intent) -> dict:
    sub = _subset(df, intent)
    if intent.name == "total_revenue":
        return {"total": float(sub["net"].sum())}
    if intent.name in ("top_n", "breakdown"):
        col = DIMENSIONS[intent.dimension]
        value = {"revenue": sub.groupby(col)["net"].sum(),
                 "units": sub.groupby(col)["quantity"].sum(),
                 "orders": sub.groupby(col)["net"].count()}[intent.metric]
        return {k: float(v) for k, v in value.items()}
    if intent.name == "monthly_trend":
        return {k: float(v) for k, v in sub.groupby("month")["net"].sum().items()}
    if intent.name == "compare_months":
        sums = sub.groupby("month")["net"].sum()
        return {m: float(sums.get(m, 0.0)) for m in (intent.month_a, intent.month_b)}
    return {}


def verify(df, intent: Intent, sql_check: dict) -> dict:
    if pd is None or df is None:
        return {"checked": False, "reason": "pandas not available"}
    other = recompute(df, intent)
    keys = set(other) | set(sql_check)
    bad = {k: {"sql": sql_check.get(k), "pandas": other.get(k)} for k in keys
           if abs((sql_check.get(k) or 0) - (other.get(k) or 0)) > 0.005}
    return {"checked": True, "matches": not bad, "figures_compared": len(keys), "mismatches": bad}
