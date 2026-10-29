"""Deterministic keyword/grammar parser. Works offline and is the fallback for the LLM."""
from __future__ import annotations

import re

from .errors import AssistantError
from .intents import FILTER_DIMENSIONS, Vocabulary

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
MONTH_PATTERN = re.compile(r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\b")
ISO_MONTH = re.compile(r"\b(20\d\d)-(0[1-9]|1[0-2])\b")
YEAR = re.compile(r"\b(20\d\d)\b")

DIM_WORDS = {
    "product": r"products?|items?|skus?", "region": r"regions?|areas?|zones?",
    "category": r"categor(?:y|ies)", "channel": r"channels?", "month": r"months?",
}
RANK_DESC = r"highest|top|best|most|maximum|max|largest|biggest|leading|greatest|bottom-?line"
RANK_ASC = r"lowest|least|worst|smallest|minimum|min|weakest|bottom"
TREND = r"trend|monthly|month[- ]by[- ]month|over time|by month|each month|per month|month[- ]wise|month over month|mom"
COMPARE = r"chang\w*|compar\w*|versus|vs\.?|differ\w*|grow\w*|increas\w*|decreas\w*|fell|fall\w*|rose|rise\w*|declin\w*|up or down"
QUALITY = r"data quality|invalid|missing|rejected|excluded|dirty|bad data|returned|returns|cleaned|clean"
TOTAL = r"total|overall|revenue|sales|income|turnover|earn\w*|made|how much"
UNITS = r"units?|quantit\w+|sold|selling|volume|pieces"
ORDERS = r"orders?|transactions?"


def _has(pattern: str, text: str) -> bool:
    return re.search(rf"\b(?:{pattern})\b", text) is not None


def _aliases(vocab: Vocabulary) -> dict[str, list[tuple[str, str]]]:
    """dimension -> [(regex, canonical)], incl. unique last-word aliases ('mouse' -> Wireless Mouse)."""
    out: dict[str, list[tuple[str, str]]] = {}
    for dim in FILTER_DIMENSIONS:
        values = vocab.values.get(dim, [])
        pairs = []
        for v in values:
            pairs.append((re.escape(v.lower()), v))
            if dim == "product":
                last = re.split(r"[\s-]+", v.lower())[-1]
                if len(last) >= 3 and sum(x.lower().endswith(last) for x in values) == 1:
                    pairs.append((re.escape(last), v))
        out[dim] = pairs
    return out


def _find_months(text: str, vocab: Vocabulary) -> list[str]:
    default_year = vocab.months[-1][:4] if vocab.months else "2026"
    explicit = YEAR.search(text)
    year = explicit.group(1) if explicit else default_year
    found: list[tuple[int, str]] = []
    for m in ISO_MONTH.finditer(text):
        found.append((m.start(), f"{m.group(1)}-{m.group(2)}"))
    for m in MONTH_PATTERN.finditer(text):
        found.append((m.start(), f"{year}-{MONTHS[m.group(1)]:02d}"))
    ordered = []
    for _, ym in sorted(found):
        if ym not in ordered:
            ordered.append(ym)
    return ordered


def parse(question: str, vocab: Vocabulary) -> dict:
    """Return a raw intent dict (still to be validated by validate_intent)."""
    q = " ".join(question.lower().replace("?", " ").replace(",", " ").split())

    filters: dict[str, str] = {}
    for dim, pairs in _aliases(vocab).items():
        for pattern, canonical in pairs:
            if re.search(rf"\b{pattern}s?\b", q):
                filters.setdefault(dim, canonical)

    months = _find_months(q, vocab)
    _reject_unknown_terms(q, vocab)
    dims = [d for d, p in DIM_WORDS.items() if _has(p, q) and d not in filters]

    if _has(QUALITY, q) and not _has(COMPARE, q):
        return {"intent": "data_quality"}

    base = {"filters": filters}
    is_compare = _has(COMPARE, q)
    is_trend = _has(TREND, q)

    if len(months) == 2 and is_compare:
        return {"intent": "compare_months", "month_a": months[0], "month_b": months[1], **base}
    if is_trend:
        return {"intent": "monthly_trend", "months": _range(months, vocab, q), **base}
    if is_compare:                                   # validator asks for the two months if absent
        return {"intent": "compare_months",
                **({"month_a": months[0], "month_b": months[1]} if len(months) == 2 else {}), **base}

    metric = "units" if _has(UNITS, q) else "orders" if _has(ORDERS, q) and dims else "revenue"
    ranking = _has(RANK_DESC, q) or _has(RANK_ASC, q)
    if ranking and dims:
        order = "asc" if _has(RANK_ASC, q) and not _has(RANK_DESC, q) else "desc"
        n = re.search(r"\b(?:top|bottom)\s+(\d+)\b", q)
        return {"intent": "top_n", "dimension": dims[0], "metric": metric, "order": order,
                "limit": int(n.group(1)) if n else 1, "months": _range(months, vocab, q), **base}

    if dims and re.search(r"\b(by|per|each|every|breakdown|split|across|wise)\b", q):
        return {"intent": "breakdown", "dimension": dims[0], "metric": metric,
                "months": _range(months, vocab, q), **base}

    if _has(TOTAL, q):
        return {"intent": "total_revenue", "months": _range(months, vocab, q), **base}

    return {"intent": "unsupported",
            "reason": "I can answer questions about revenue, rankings, monthly trends and month-to-month changes."}


def _range(months: list[str], vocab: Vocabulary, q: str) -> list[str]:
    """'January to March' (two months, no compare words) -> every month in the span."""
    if len(months) == 2 and re.search(r"\b(to|through|between|until|thru)\b|-", q):
        lo, hi = sorted(months)
        return [m for m in vocab.months if lo <= m <= hi] or months
    return months


_KNOWN_WORDS = set(MONTHS) | {
    "the", "a", "an", "all", "each", "every", "any", "total", "overall", "our", "my", "us", "year",
    "both", "general", "terms", "whole", "month", "months", "region", "regions", "product", "products",
    "category", "categories", "channel", "channels", "revenue", "sales", "units", "orders", "data",
    "item", "items", "area", "areas", "zone", "zones", "sku", "skus", "quantity", "volume",
    "this", "that", "these", "those", "it", "them", "me", "between", "and", "to", "from", "through",
}
_AFTER_PREPOSITION = re.compile(r"\b(?:for|of|about|regarding|from|in|on|at)\s+(?:the\s+)?([a-z][a-z0-9-]*)")


def _reject_unknown_terms(q: str, vocab: Vocabulary) -> None:
    """Refuse instead of silently ignoring an entity we do not know (e.g. 'revenue for Tablet')."""
    known = set(_KNOWN_WORDS)
    for values in vocab.values.values():
        for v in values:
            known.update(re.split(r"[\s-]+", v.lower()))
            known.update(v.lower().split())
            known.add(v.lower())
    for m in _AFTER_PREPOSITION.finditer(q):
        word = m.group(1)
        base = word[:-1] if word.endswith("s") else word
        if word not in known and base not in known and not word.isdigit():
            raise AssistantError(
                "unknown_term", f"I don't recognise '{word}' in the data, so I won't guess.",
                hints=[f"Available {d}s: {', '.join(v)}" for d, v in vocab.values.items()])
