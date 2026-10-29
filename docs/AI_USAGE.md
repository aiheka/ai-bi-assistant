# AI Usage Report

> Written to match how this project was actually built. Before submitting, read it through and
> change anything that does not match your own experience: you will be interviewed on it.

## 1. Which AI tools were used
- **Claude** (chat assistant) for planning, code generation, review, and test writing.
- **Claude API (optional runtime feature):** the app can call a Claude model to interpret questions. It is off unless `ANTHROPIC_API_KEY` is set.
- No other AI coding tools were used.

## 2. How AI was used during development
- **Understand first:** the brief and the CSV were read and profiled before any code, which exposed three problem rows (TXN0033 Returned, TXN0034 quantity 0, TXN0051 missing unit price).
- **Architecture decision:** instead of free-form text-to-SQL, the AI only translates a question into a small validated "intent". Fixed, parameterised SQL computes the numbers and Python writes the answer text, so the model cannot invent figures or run destructive SQL.
- **Layer-by-layer generation:** database loading, intent validation, SQL queries, rule parser, LLM parser, API, UI, and tests were generated one at a time, with a quick smoke test after each layer.
- **Adversarial testing:** the AI was used to brainstorm bad inputs (SQL injection, unknown products, months with no data, ambiguous questions), and these became automated tests.
- **Independent verification:** a second, separate pandas calculation was written so each answer is cross-checked against the SQL result.

## 3. Problems encountered
| Problem | Effect |
|---|---|
| Unknown product (`revenue for Tablet`) was ignored | silently returned the grand total |
| `DROP TABLE sales;` contained the word "sales" | parsed as a revenue question |
| The word "drop" was in the compare keywords | false "compare months" error |
| `USB-C Hub` not recognised after splitting on hyphens | valid product refused |
| A hallucinated filter value from the LLM raised an error | should have fallen back to the rule parser |
| Shell brace expansion did not create project folders | missing directories during setup |

## 4. How they were debugged
- **Probing:** ran ~20 odd questions through the assistant and read each answer critically. This is how the "Tablet" and `DROP TABLE` problems were found.
- **Reproduce, fix, lock in:** each bug was reproduced with a one-line input, fixed at its cause, then added as a unit test so it cannot return.
- **Failing test as signal:** the LLM-fallback bug was caught by a test that fed the app a malicious LLM response.
- **Reference numbers:** totals were computed separately in pandas (1,439,200 revenue) and compared with the app's output before trusting it.

## 5. What I would improve with another day
- Support quarters and relative dates ("Q1", "last month").
- Conversation memory for follow-ups ("and in the South?").
- An agent loop (LangChain or Google ADK) that calls the query functions as tools for multi-step analysis.
- A RAG layer over a metric glossary and business definitions.
- CSV upload, Docker, and CI that runs the tests on every push.
- A labelled question set to measure rules-parser versus LLM accuracy, and a live test of the LLM path (only mocked so far).
