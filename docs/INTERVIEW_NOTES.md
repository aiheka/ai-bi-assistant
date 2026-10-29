# Be ready to explain (the brief says you will be interviewed)

1. **Why intents instead of text-to-SQL?** Smaller attack surface, deterministic numbers, easy to validate and test. Trade-off: narrower coverage.
2. **Where can the LLM still be wrong?** Picking the wrong intent or filter. Mitigations: whitelist validation, the answer shows the interpreted intent and SQL, rule-parser fallback, and an independent pandas check of the computed numbers.
3. **Is this agentic AI?** Not really; it is a single-step intent router. To make it agentic, wrap `queries.execute` as tools in LangChain / Google ADK so an agent can plan multi-step analyses (compare two months, then drill into the driver).
4. **Where would RAG fit?** Retrieve a metric glossary or business definitions ("revenue", returns policy) to ground interpretation. "Vectorless RAG" means retrieving from structured sources (SQL or keyword indexes) instead of embeddings, which is what this app already does for numbers.
5. **Data decisions to defend:** exclude Returned orders; reject quantity 0 and missing price rather than impute; day-first dates; discount treated as a per-transaction total.
6. **Modify live:** add a metric (e.g. average order value) = a new column in `queries.ranking` plus an entry in `intents.METRICS`; add a dimension = one line in `intents.DIMENSIONS` plus the word list in `parser_rules.DIM_WORDS`.
7. **Read-only defence in depth:** `mode=ro`, `PRAGMA query_only`, SQLite authorizer (SELECT/READ/FUNCTION only), `assert_select_only`, parameterised values.
8. **Numbers to remember:** 51 rows, 49 loaded, 2 rejected, 1 returned; total revenue 1,439,200; top product Monitor (473,000); top region South (411,200); peak month May (318,500).
