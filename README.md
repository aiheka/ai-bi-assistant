# Mini AI Business Intelligence Assistant

Ask sales questions in plain English and get an answer, the supporting figures, the exact SQL that
produced them, and an independent cross-check. Built for the *AI-Assisted Development Challenge (GET)*.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m app.main                                     # open http://127.0.0.1:5000
python -m unittest discover -s tests -v                # 28 tests, no network needed
```

Optional LLM interpretation (the app works fully without it):

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # enables LLM question understanding
export BI_LLM_MODEL=claude-sonnet-5-5      # optional model override
export BI_PARSER=auto                      # auto (LLM, then rules) | rules | llm
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/ask` | `{"question": "..."}` -> answer, rows, SQL, verification, notes |
| GET | `/api/data-quality` | what was rejected / excluded, and why |
| GET | `/api/examples`, `/api/health` | sample questions; load report |

Errors are structured: `{"ok": false, "error": {"code", "message", "hints"}}` with HTTP 400 (bad request),
422 (unsupported question, unknown entity, month with no data, missing months) or 500 (generic message; details only in logs).

## Architecture

```
 Browser UI --POST /api/ask--> Flask (main.py)
                                   |
                              service.Assistant
        +--------------------------+-------------------------------+
        v                          v                               v
 1 UNDERSTAND                2 VALIDATE                       3 COMPUTE
 parser_llm (optional)  -->  intents.validate_intent  -->  queries.py (parameterised SELECT)
 parser_rules (fallback)     whitelists + values that       read-only SQLite connection
 output: intent JSON only    exist in the database          (mode=ro + query_only + authorizer)
                                                                   |
                                  4 VERIFY  verify.py re-reads the raw CSV with pandas,
                                    recomputes the same figures, flags any mismatch
                                                                   |
                                  5 ANSWER  answers.py builds the text in Python from the numbers
```

**Key decision: the AI never writes SQL and never writes numbers.** It only translates a question into a small
*intent* (e.g. `top_n`, `dimension=region`, `filters={...}`). The intent is validated against whitelists and the
values that really exist in the database, then deterministic, parameterised SQL runs. The sentence the user reads is
formatted by Python from the query result. This removes the two classic text-to-SQL failure modes: destructive or
incorrect SQL, and hallucinated figures. If the LLM is down, returns junk, or tries something odd, the deterministic
rule parser answers instead (covered by tests).

## Data storage and handling

CSV -> SQLite (`data/sales.db`, rebuilt on every start). Tables: `sales` (valid rows with computed `gross_amount`
and `net_revenue`), `rejected_records` (invalid rows + reasons + original values), `load_notes`. The view
`revenue_sales` is `sales` where status = `Completed`; every analytic query reads only this view.

| Source row | Issue | Treatment |
|---|---|---|
| TXN0033 | status `Returned` | stored, **excluded from revenue** (value 19,000 reported separately) |
| TXN0034 | quantity 0 | **rejected** |
| TXN0051 | missing unit_price | **rejected** (not guessed or imputed) |

Other rules: dates are day-first (`25-01-2026` proves it); blank discount -> 0 (noted); discount > gross, non-numeric
values, duplicate IDs and missing required fields -> rejected.
**Revenue = quantity x unit price - discount_amount**, completed orders only.
*Assumption:* `discount_amount` is a per-transaction total, not per unit (the data cannot confirm this).
Currency is not stated, so none is displayed.

Result on the supplied file: 51 rows -> 49 loaded, 2 rejected; **total revenue 1,439,200** from 48 completed orders.

## Supported questions

All five example questions (total revenue; highest product; monthly trend; highest region; change between two
months, including which products drove the change), plus lowest / top-N of product, region, category, channel or
month; `by <dimension>` breakdowns; units or orders as the metric; filters (`revenue for Monitor in North in March`);
month ranges (`January to March`); and `what data was excluded?`.

## How incorrect answers are prevented

1. LLM output is untrusted: strict whitelist validation; unknown intents, columns or values are rejected and the rule parser takes over.
2. Unknown entities are refused (`revenue for Tablet` -> error) instead of silently ignored, which would have returned the grand total.
3. Months with no data (`July`, `March 2024`) return an error listing the months that exist.
4. Ambiguous compare questions (`between two months`) ask for the months instead of guessing.
5. SQL-looking input is refused. Execution is read-only at three layers, plus a SELECT-only guard on every statement.
6. Every figure is recomputed by an independent pandas pipeline; the UI shows a check badge, or a warning on mismatch.

## Tests (28)

`tests/test_assistant.py` covers: data cleaning (including a messy synthetic CSV), the five example analytics with
exact expected numbers, invalid input (empty, oversized, out-of-scope, SQL injection, no-data month, unknown entity,
missing months), read-only and validation guards, mocked-LLM paths (valid, malicious, outage, garbage, number
injection), and API status codes.

## Known limitations

- One dataset (Jan-Jun 2026). No relative dates (`last month`), quarters (`Q1`) or other years.
- One metric and one dimension per question; no forecasting or open-ended "why" analysis (beyond product drivers in a month comparison).
- The rule parser is keyword-based, so unusual phrasings may be refused. LLM mode widens coverage.
- `discount_amount` semantics and day-first dates are assumptions (see above).
- Flask dev server only; no auth or rate limiting.
- The live-LLM path is tested with mocks; it has not been exercised against the real API here.

## With another day of work

Quarter and relative-date support; conversation memory for follow-ups ("and in the South?"); an agent loop
(LangChain / Google ADK) that calls these intents as tools for multi-step analysis; a RAG layer over a metric
glossary; CSV upload; Docker and CI; a labelled question set to measure rules-vs-LLM accuracy.

## Deliverables checklist

| Required | Where |
|---|---|
| Source code in Git | this repository (`app/`, `tests/`) |
| README with setup and execution | this file, "Quick start" |
| Architecture diagram / explanation | this file, "Architecture" |
| Automated tests | `tests/test_assistant.py` (28 tests) |
| AI usage document | `docs/AI_USAGE.md` |
