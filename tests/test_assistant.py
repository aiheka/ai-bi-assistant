"""Run with:  python -m unittest discover -s tests -v   (pytest also works)."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import database, queries
from app.errors import AssistantError
from app.intents import Vocabulary, validate_intent
from app.main import create_app
from app.service import Assistant

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "sales.csv"


def make_assistant(**kw):
    tmp = tempfile.mkdtemp()
    return Assistant(kw.pop("csv", CSV), Path(tmp) / "t.db", **kw)


class DataLoadingTests(unittest.TestCase):
    def test_invalid_rows_are_rejected_not_silently_dropped(self):
        a = make_assistant()
        self.assertEqual(a.load_report, {"source_rows": 51, "loaded": 49, "rejected": 2})
        conn = database.connect_readonly(a.db_path)
        rej = {r["transaction_id"]: r["reasons"] for r in conn.execute("SELECT * FROM rejected_records")}
        self.assertIn("TXN0034", rej)   # quantity 0
        self.assertIn("TXN0051", rej)   # missing unit price

    def test_returned_order_loaded_but_excluded_from_revenue(self):
        conn = database.connect_readonly(make_assistant().db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM sales WHERE transaction_id='TXN0033'").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM revenue_sales WHERE transaction_id='TXN0033'").fetchone()[0], 0)

    def test_messy_csv_edge_cases(self):
        text = ("Transaction_Id,Order_date,Product,Category,Region,Sales_channel,Quantity,Unit_price,Discount_amount,Status\n"
                "A1,01-02-2026,Pen,Misc,East,Online,2,10,,Completed\n"      # blank discount -> 0
                "A1,02-02-2026,Pen,Misc,East,Online,1,10,0,Completed\n"     # duplicate id
                "A2,31-02-2026,Pen,Misc,East,Online,1,10,0,Completed\n"     # impossible date
                "A3,03-02-2026,Pen,Misc,East,Online,1,10,50,Completed\n"    # discount > gross
                "A4,04-02-2026,Pen,Misc,East,Online,abc,10,0,Completed\n")  # bad quantity
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.csv"
            p.write_text(text)
            a = Assistant(p, Path(d) / "m.db")
            self.assertEqual(a.load_report["loaded"], 1)
            self.assertEqual(a.load_report["rejected"], 4)
            self.assertEqual(a.ask("What is the total revenue?")["rows"][0]["net_revenue"], 20.0)


class AnalyticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = make_assistant()

    def test_total_revenue(self):
        r = self.a.ask("What is the total revenue?")
        self.assertEqual(r["rows"][0]["net_revenue"], 1_439_200)
        self.assertTrue(r["verification"]["matches"])

    def test_top_product(self):
        r = self.a.ask("Which product generated the highest revenue?")
        self.assertEqual((r["rows"][0]["label"], r["rows"][0]["revenue"]), ("Monitor", 473_000))

    def test_top_region(self):
        self.assertEqual(self.a.ask("Which region generated the highest sales?")["rows"][0]["label"], "South")

    def test_monthly_trend(self):
        r = self.a.ask("What is the monthly revenue trend?")
        self.assertEqual([x["revenue"] for x in r["rows"]],
                         [210_250, 234_600, 223_900, 240_700, 318_500, 211_250])
        self.assertEqual(r["rows"][1]["change_vs_prev_pct"], 11.58)

    def test_month_comparison(self):
        r = self.a.ask("How did revenue change between January and February?")
        self.assertIn("rose", r["answer"])
        self.assertIn("+11.58%", r["answer"])

    def test_filters_and_aliases(self):
        r = self.a.ask("revenue of mouse in feb")
        self.assertEqual(r["rows"][0]["net_revenue"], 33_800)

    def test_lowest_and_top_n(self):
        self.assertEqual(self.a.ask("lowest region")["rows"][0]["label"], "West")
        self.assertEqual(len(self.a.ask("top 3 products")["answer"].split(";")), 3)

    def test_units_metric(self):
        self.assertIn("units", self.a.ask("Which sales channel sells the most units?")["answer"])

    def test_every_answer_is_cross_verified(self):
        for q in ["total revenue", "revenue by category", "monthly trend", "compare feb vs mar",
                  "highest month", "revenue for Monitor in the North region"]:
            v = self.a.ask(q)["verification"]
            self.assertTrue(v["checked"] and v["matches"], q)


class InvalidInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = make_assistant()

    def code(self, q):
        with self.assertRaises(AssistantError) as cm:
            self.a.ask(q)
        return cm.exception.code

    def test_empty_and_oversized(self):
        self.assertEqual(self.code("   "), "invalid_request")
        self.assertEqual(self.code("x" * 501), "invalid_request")
        self.assertEqual(self.code(None), "invalid_request")

    def test_out_of_scope(self):
        self.assertEqual(self.code("What is the weather today?"), "unsupported_question")

    def test_sql_injection_attempts_never_execute(self):
        self.assertEqual(self.code("DROP TABLE sales;"), "unsupported_question")
        self.assertEqual(self.code("revenue; DELETE FROM sales"), "unsupported_question")
        self.assertEqual(self.a.load_report["loaded"], 49)
        self.assertEqual(self.a.ask("total revenue")["rows"][0]["orders"], 48)

    def test_month_without_data(self):
        self.assertEqual(self.code("revenue in July"), "no_data")
        self.assertEqual(self.code("revenue in March 2024"), "no_data")

    def test_unknown_entity_is_refused_not_ignored(self):
        self.assertEqual(self.code("revenue for Tablet"), "unknown_term")

    def test_compare_needs_two_months(self):
        self.assertEqual(self.code("How did revenue change between two months?"), "missing_months")
        self.assertEqual(self.code("compare January"), "missing_months")


class SafetyTests(unittest.TestCase):
    def test_select_guard(self):
        queries.assert_select_only("SELECT 1")
        for bad in ["DELETE FROM sales", "SELECT 1; DROP TABLE sales", "UPDATE sales SET quantity=1",
                    "PRAGMA writable_schema=1"]:
            with self.assertRaises(AssistantError):
                queries.assert_select_only(bad)

    def test_database_connection_is_read_only(self):
        conn = database.connect_readonly(make_assistant().db_path)
        for stmt in ["DELETE FROM sales", "DROP TABLE sales", "UPDATE sales SET quantity = 0"]:
            with self.assertRaises(sqlite3.Error):
                conn.execute(stmt)

    def test_intent_validation_rejects_bad_llm_output(self):
        v = Vocabulary({"product": ["Monitor"], "region": ["North"], "category": [], "channel": []}, ["2026-01"])
        bad = [{"intent": "run_sql"}, {"intent": "top_n", "dimension": "password"},
               {"intent": "top_n", "dimension": "product", "limit": 9999},
               {"intent": "total_revenue", "filters": {"product": "x'; DROP TABLE sales;--"}},
               {"intent": "total_revenue", "months": ["2026-13"]}, "not a dict"]
        for raw in bad:
            with self.assertRaises(AssistantError, msg=str(raw)):
                validate_intent(raw, v)


class LLMIntegrationTests(unittest.TestCase):
    """The LLM client is injected, so these tests never touch the network."""

    def test_valid_llm_intent_is_used(self):
        client = lambda q, v: json.dumps({"intent": "top_n", "dimension": "category", "metric": "revenue"})
        r = make_assistant(llm_client=client).ask("which segment earns most?")
        self.assertEqual(r["interpretation"]["parser"], "llm")
        self.assertEqual(r["rows"][0]["label"], "Accessories")

    def test_malicious_llm_output_falls_back_to_rules(self):
        client = lambda q, v: json.dumps({"intent": "total_revenue",
                                          "filters": {"region": "North'; DROP TABLE sales;--"}})
        r = make_assistant(llm_client=client).ask("What is the total revenue?")
        self.assertEqual(r["interpretation"]["parser"], "rules")
        self.assertIn("rejected", r["interpretation"]["fallback_reason"])
        self.assertEqual(r["rows"][0]["net_revenue"], 1_439_200)

    def test_llm_outage_and_garbage_fall_back(self):
        def boom(q, v): raise TimeoutError()
        self.assertEqual(make_assistant(llm_client=boom).ask("total revenue")["interpretation"]["parser"], "rules")
        junk = lambda q, v: "I'm sorry, I can't do that"
        self.assertEqual(make_assistant(llm_client=junk).ask("total revenue")["interpretation"]["parser"], "rules")

    def test_llm_cannot_inject_numbers(self):
        client = lambda q, v: json.dumps({"intent": "total_revenue", "answer": "Revenue is 999999999"})
        r = make_assistant(llm_client=client).ask("total revenue")
        self.assertNotIn("999999999", r["answer"])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = create_app(assistant=make_assistant()).test_client()

    def test_ask_ok(self):
        r = self.client.post("/api/ask", json={"question": "What is the total revenue?"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])

    def test_bad_requests(self):
        self.assertEqual(self.client.post("/api/ask", data="nope").status_code, 400)
        self.assertEqual(self.client.post("/api/ask", json={"q": 1}).status_code, 400)
        self.assertEqual(self.client.post("/api/ask", json={"question": ""}).status_code, 400)
        self.assertEqual(self.client.post("/api/ask", json={"question": "weather?"}).status_code, 422)
        self.assertEqual(self.client.get("/api/ask").status_code, 405)

    def test_data_quality_endpoint_and_ui(self):
        dq = self.client.get("/api/data-quality").get_json()
        self.assertIn("TXN0051", dq["answer"])
        self.assertEqual(self.client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
