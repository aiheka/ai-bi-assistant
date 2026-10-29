"""The contract between "understanding" (rules or LLM) and "computing" (SQL).

The language layer may only produce an Intent. An Intent is a small, closed
vocabulary of validated values; it is never SQL. Every value is checked here
against whitelists and against the values that actually exist in the database,
so a hallucinated or malicious LLM answer cannot reach the query layer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import AssistantError

INTENTS = {"total_revenue", "top_n", "breakdown", "monthly_trend", "compare_months", "data_quality"}
DIMENSIONS = {"product": "product", "region": "region", "category": "category",
              "channel": "sales_channel", "month": "month"}
FILTER_DIMENSIONS = ("product", "region", "category", "channel")
METRICS = {"revenue", "units", "orders"}
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass
class Vocabulary:
    """Values that really exist in the database (the grounding for validation)."""
    values: dict[str, list[str]]          # dimension -> canonical values
    months: list[str]                     # sorted yyyy-mm that have revenue data

    def canonical(self, dimension: str, value: str) -> str | None:
        for v in self.values.get(dimension, []):
            if v.lower() == str(value).strip().lower():
                return v
        return None


@dataclass
class Intent:
    name: str
    dimension: str | None = None
    metric: str = "revenue"
    order: str = "desc"
    limit: int | None = None
    filters: dict[str, str] = field(default_factory=dict)
    months: list[str] = field(default_factory=list)   # filter / trend range
    month_a: str | None = None
    month_b: str | None = None

    def describe(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], {})}


def month_label(ym: str) -> str:
    names = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
    return f"{names[int(ym[5:7]) - 1]} {ym[:4]}"


def validate_intent(raw: dict, vocab: Vocabulary) -> Intent:
    """Turn an untrusted dict (rules parser or LLM output) into a safe Intent."""
    if not isinstance(raw, dict):
        raise AssistantError("unsupported_question", "Could not interpret the question.")
    name = raw.get("intent")
    if name == "unsupported":
        raise AssistantError("unsupported_question",
                             raw.get("reason") or "That question is outside what I can answer.")
    if name not in INTENTS:
        raise AssistantError("unsupported_question", "Could not map the question to a supported analysis.")

    intent = Intent(name=name)

    metric = raw.get("metric") or "revenue"
    if metric not in METRICS:
        raise AssistantError("unsupported_question", f"Unsupported metric '{metric}'.")
    intent.metric = metric

    order = raw.get("order") or "desc"
    if order not in ("asc", "desc"):
        raise AssistantError("unsupported_question", "Invalid sort order.")
    intent.order = order

    dim = raw.get("dimension")
    if dim is not None:
        if dim not in DIMENSIONS:
            raise AssistantError("unsupported_question", f"Cannot group by '{dim}'.")
        intent.dimension = dim
    if name in ("top_n", "breakdown") and intent.dimension is None:
        raise AssistantError("unsupported_question",
                             "Tell me what to group by: product, region, category, channel or month.")

    limit = raw.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise AssistantError("unsupported_question", "Limit must be a whole number from 1 to 50.")
        intent.limit = limit

    filters = raw.get("filters") or {}
    if not isinstance(filters, dict):
        raise AssistantError("unsupported_question", "Invalid filters.")
    for key, value in filters.items():
        if key not in FILTER_DIMENSIONS:
            raise AssistantError("unsupported_question", f"Cannot filter by '{key}'.")
        canon = vocab.canonical(key, value)
        if canon is None:
            raise AssistantError("no_data", f"No {key} called '{value}' exists in the data.",
                                 hints=[f"Available {key} values: {', '.join(vocab.values[key])}"])
        intent.filters[key] = canon

    months = raw.get("months") or []
    if not isinstance(months, list) or not all(isinstance(m, str) and MONTH_RE.match(m) for m in months):
        raise AssistantError("unsupported_question", "Months must look like 2026-03.")
    intent.months = sorted(set(months))

    if name == "compare_months":
        a, b = raw.get("month_a"), raw.get("month_b")
        if not (isinstance(a, str) and isinstance(b, str) and MONTH_RE.match(a) and MONTH_RE.match(b)):
            raise AssistantError("missing_months",
                                 "To compare, name exactly two months, e.g. 'How did revenue change between January and February?'")
        if a == b:
            raise AssistantError("missing_months", "Pick two different months to compare.")
        intent.month_a, intent.month_b = a, b

    needed = set(intent.months) | {m for m in (intent.month_a, intent.month_b) if m}
    absent = sorted(needed - set(vocab.months))
    if absent:
        raise AssistantError(
            "no_data",
            "There is no revenue data for " + ", ".join(month_label(m) for m in absent) + ".",
            hints=["Months with data: " + ", ".join(month_label(m) for m in vocab.months)])
    return intent
