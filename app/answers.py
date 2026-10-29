"""Builds the human-readable answer from verified numbers.

All text is produced here by plain Python formatting from the query result.
No language model writes any figure shown to the user.
"""
from __future__ import annotations

from .intents import DIMENSIONS, Intent, month_label

DIM_TITLE = {"product": "product", "region": "region", "category": "category",
             "channel": "sales channel", "month": "month"}


def money(x: float) -> str:
    return f"{x:,.0f}" if float(x).is_integer() else f"{x:,.2f}"


def _label(dim: str, value: str) -> str:
    return month_label(value) if dim == "month" else value


def scope_text(intent: Intent) -> str:
    parts = [f"{k}: {v}" for k, v in intent.filters.items()]
    if len(intent.months) == 1:
        parts.append(month_label(intent.months[0]))
    elif intent.months:
        parts.append(f"{month_label(intent.months[0])} to {month_label(intent.months[-1])}")
    return f" ({', '.join(parts)})" if parts else ""


def compose(intent: Intent, result) -> str:
    s, scope = result.summary, scope_text(intent)

    if intent.name == "total_revenue":
        if s["orders"] == 0:
            return f"There are no completed sales{scope or ' in the data'}, so revenue is 0."
        return (f"Total revenue{scope} is {money(s['net_revenue'])}, from {s['orders']} completed order{'s' if s['orders'] != 1 else ''} "
                f"and {s['units']} units. That is gross sales of {money(s['gross_sales'])} "
                f"less discounts of {money(s['discounts'])}.")

    if intent.name in ("top_n", "breakdown"):
        rows, dim = result.rows, intent.dimension
        if not rows:
            return f"There are no completed sales{scope or ' in the data'}."
        metric = intent.metric
        word = {"revenue": "revenue", "units": "units sold", "orders": "orders"}[metric]
        fmt = money if metric == "revenue" else (lambda v: f"{v:,.0f}")
        if intent.name == "breakdown":
            lead = rows[0]
            return (f"Revenue by {DIM_TITLE[dim]}{scope}: {len(rows)} groups, total {money(s['total_revenue'])}. "
                    f"Largest is {_label(dim, lead['label'])} at {money(lead['revenue'])} "
                    f"({lead['share_pct']}% of the total).")
        n = intent.limit or 1
        direction = "highest" if intent.order == "desc" else "lowest"
        top = rows[:n]
        if n == 1:
            lead = top[0]
            tied = [r for r in rows if r[metric] == lead[metric]]
            msg = (f"The {DIM_TITLE[dim]} with the {direction} {word}{scope} is "
                   f"{_label(dim, lead['label'])} with {fmt(lead[metric])}")
            if metric == "revenue":
                msg += f" ({lead['share_pct']}% of total revenue)"
            msg += "."
            if len(tied) > 1:
                msg += " It is tied with " + ", ".join(_label(dim, r["label"]) for r in tied[1:]) + "."
            return msg
        listing = "; ".join(f"{i}. {_label(dim, r['label'])} ({fmt(r[metric])})" for i, r in enumerate(top, 1))
        return f"The {direction} {len(top)} {DIM_TITLE[dim]}s by {word}{scope}: {listing}."

    if intent.name == "monthly_trend":
        rows = result.rows
        if not rows:
            return f"There are no completed sales{scope or ' in the data'}."
        best = max(rows, key=lambda r: r["revenue"])
        worst = min(rows, key=lambda r: r["revenue"])
        first, last = rows[0], rows[-1]
        return (f"Monthly revenue{scope} went from {money(first['revenue'])} in {month_label(first['month'])} "
                f"to {money(last['revenue'])} in {month_label(last['month'])}. Peak: {month_label(best['month'])} "
                f"({money(best['revenue'])}); lowest: {month_label(worst['month'])} ({money(worst['revenue'])}).")

    if intent.name == "compare_months":
        a, b = month_label(s["month_a"]), month_label(s["month_b"])
        if s["revenue_a"] == 0 and s["revenue_b"] == 0:
            return f"There is no completed revenue{scope} in either {a} or {b}."
        verb = "rose" if s["change"] > 0 else "fell" if s["change"] < 0 else "did not change"
        pct = "" if s["change_pct"] is None else f" ({s['change_pct']:+.2f}%)"
        base = f"Revenue{scope} {verb} from {money(s['revenue_a'])} in {a} to {money(s['revenue_b'])} in {b}"
        if s["change"] == 0:
            return base + "."
        msg = f"{base}, a change of {s['change']:+,.0f}{pct}."
        if s["change_pct"] is None:
            msg += f" (No % change: {a} had no revenue.)"
        movers = [d for d in result.rows if d["change"] != 0]
        if movers:
            up = max(movers, key=lambda d: d["change"])
            down = min(movers, key=lambda d: d["change"])
            bits = []
            if up["change"] > 0:
                bits.append(f"biggest gain: {up['product']} ({up['change']:+,.0f})")
            if down["change"] < 0:
                bits.append(f"biggest drop: {down['product']} ({down['change']:+,.0f})")
            if bits:
                msg += " Drivers - " + "; ".join(bits) + "."
        return msg
    return ""
