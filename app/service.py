"""Orchestrates: question -> intent (LLM or rules) -> validate -> SQL -> verify -> answer."""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import answers, database, parser_llm, parser_rules, queries, verify
from .errors import AssistantError
from .intents import Intent, Vocabulary, month_label, validate_intent

MAX_QUESTION_LEN = 500
SQL_LIKE = re.compile(r"(;|--|/\*)|\b(drop|delete|truncate|insert|alter|update|attach|pragma|union)\b"
                      r".*\b(table|database|into|set|select|from|sales)\b|\bselect\b.*\bfrom\b", re.I)
EXAMPLES = [
    "What is the total revenue?",
    "Which product generated the highest revenue?",
    "What is the monthly revenue trend?",
    "Which region generated the highest sales?",
    "How did revenue change between February and March?",
    "Which sales channel sells the most units?",
    "Revenue for Monitor in the North region",
    "What data was excluded or invalid?",
]
SUPPORTED_HELP = [
    "Total revenue, optionally filtered by product, region, category, channel or month(s)",
    "Highest / lowest / top-N product, region, category, channel or month",
    "Revenue breakdown by any of those",
    "Monthly revenue trend",
    "Change between two months, with the products driving it",
]


class Assistant:
    def __init__(self, csv_path: str | Path, db_path: str | Path, parser_mode: str | None = None,
                 llm_client=None):
        self.csv_path, self.db_path = str(csv_path), str(db_path)
        self.parser_mode = (parser_mode or os.environ.get("BI_PARSER", "auto")).lower()
        self.llm_client = llm_client
        self.load_report = database.load_csv(csv_path, db_path)
        self.frame = None
        try:
            self.frame = verify.load_frame(self.csv_path) if verify.pd else None
        except Exception:                    # verification must never break the app
            self.frame = None
        self.vocab = self._vocabulary()

    # ---- setup ---------------------------------------------------------------
    def _vocabulary(self) -> Vocabulary:
        conn = database.connect_readonly(self.db_path)
        try:
            col = {"product": "product", "region": "region", "category": "category",
                   "channel": "sales_channel"}
            values = {d: [r[0] for r in conn.execute(
                f"SELECT DISTINCT {c} FROM revenue_sales ORDER BY {c}")] for d, c in col.items()}
            months = [r[0] for r in conn.execute("SELECT DISTINCT month FROM revenue_sales ORDER BY month")]
        finally:
            conn.close()
        return Vocabulary(values, months)

    # ---- interpretation --------------------------------------------------------
    def _interpret(self, question: str) -> tuple[Intent, dict]:
        meta = {"parser": "rules"}
        raw = None
        if self.parser_mode in ("auto", "llm") and (self.llm_client or os.environ.get("ANTHROPIC_API_KEY")):
            try:
                raw = parser_llm.parse(question, self.vocab, self.llm_client or parser_llm.call_anthropic)
                intent = validate_intent(raw, self.vocab)
                return intent, {"parser": "llm"}
            except AssistantError as exc:
                # Untrusted LLM output failed validation (or it refused): the deterministic
                # rule parser gets the final say, so a hallucination can never become an answer.
                if self.parser_mode == "llm":
                    raise
                meta["fallback_reason"] = f"LLM output rejected: {exc.message}"
            except Exception as exc:         # network, bad JSON, timeout...
                meta["fallback_reason"] = f"LLM unavailable ({type(exc).__name__}); used rule parser"
        raw = parser_rules.parse(question, self.vocab)
        return validate_intent(raw, self.vocab), meta

    # ---- public API ------------------------------------------------------------
    def ask(self, question) -> dict:
        if not isinstance(question, str) or not question.strip():
            raise AssistantError("invalid_request", "Please provide a non-empty question.", status=400)
        question = question.strip()
        if len(question) > MAX_QUESTION_LEN:
            raise AssistantError("invalid_request",
                                 f"Question is too long (max {MAX_QUESTION_LEN} characters).", status=400)
        if re.fullmatch(r"[\W_]+", question):
            raise AssistantError("invalid_request", "That does not look like a question.", status=400)

        if SQL_LIKE.search(question):
            raise AssistantError(
                "unsupported_question",
                "I only answer business questions in plain language; I never run SQL or modify data.",
                hints=SUPPORTED_HELP)

        try:
            intent, meta = self._interpret(question)
        except AssistantError as exc:
            if exc.code == "unsupported_question" and not exc.hints:
                exc.hints = SUPPORTED_HELP
            raise

        if intent.name == "data_quality":
            return self.data_quality(question, intent, meta)

        conn = database.connect_readonly(self.db_path)
        try:
            result = queries.execute(conn, intent)
        finally:
            conn.close()

        check = verify.verify(self.frame, intent, result.check)
        return {
            "ok": True,
            "question": question,
            "interpretation": {"intent": intent.describe(), **meta},
            "answer": answers.compose(intent, result),
            "columns": result.columns,
            "rows": result.rows,
            "queries": result.queries,
            "verification": check,
            "notes": self._notes(),
        }

    def _notes(self) -> list[str]:
        r = self.load_report
        notes = ["Revenue = quantity x unit price - discount, counting only 'Completed' orders. "
                 "Amounts are in the dataset's currency unit."]
        conn = database.connect_readonly(self.db_path)
        try:
            ret = conn.execute("SELECT COUNT(*) c FROM sales WHERE status <> 'Completed'").fetchone()["c"]
        finally:
            conn.close()
        if r["rejected"] or ret:
            notes.append(f"Excluded from revenue: {r['rejected']} invalid row(s) rejected at load and "
                         f"{ret} non-completed (e.g. returned) order(s). Ask 'what data was excluded?' for details.")
        return notes

    def data_quality(self, question: str | None = None, intent: Intent | None = None, meta: dict | None = None) -> dict:
        conn = database.connect_readonly(self.db_path)
        try:
            dq = queries.data_quality(conn)
        finally:
            conn.close()
        rej = dq["rejected"]
        parts = [f"{self.load_report['source_rows']} source rows: {dq['valid_rows']} loaded, {len(rej)} rejected."]
        if rej:
            parts.append("Rejected: " + "; ".join(
                f"{r['transaction_id']} ({r['reasons']})" for r in rej) + ".")
        if dq["not_completed"]:
            parts.append("Valid but not counted as revenue: " + "; ".join(
                f"{r['transaction_id']} {r['status']} (value {answers.money(r['value'])})"
                for r in dq["not_completed"]) + ".")
        rows = ([{"transaction_id": r["transaction_id"], "treatment": "rejected", "detail": r["reasons"]} for r in rej] +
                [{"transaction_id": r["transaction_id"], "treatment": "excluded from revenue",
                  "detail": f"status {r['status']}, value {answers.money(r['value'])}"} for r in dq["not_completed"]])
        return {"ok": True, "question": question, "interpretation": {"intent": {"name": "data_quality"}, **(meta or {})},
                "answer": " ".join(parts), "columns": ["transaction_id", "treatment", "detail"], "rows": rows,
                "queries": dq["queries"], "verification": {"checked": False, "reason": "not a computed figure"},
                "notes": ["Rejected rows are kept in the rejected_records table with their original values."]}
