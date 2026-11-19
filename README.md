# Grounded Text-to-SQL Data Quality Agent

Ask data-quality questions in plain English and get answers from governed, tested SQL —
not hallucinated queries.

```
"Are there any missing months for Oncoviva?"
        │
        ▼
 1. Grounding  ── schema + value index ──► filters: {brand: "Oncoviva"}
 2. Routing    ── keyword intent ────────► playbook: gap_detection
 3. Execution  ── parameterised SQL ─────► Oncoviva | West | 2026-06
        │
        └── no playbook matches? ──► LLM writes SQL from the grounded schema
                                     ──► read-only guard ──► execute
```

## Why this design
LLMs writing SQL from scratch tend to invent filter values (`'Onco Viva'`, `'WEST'`) and
produce queries nobody has reviewed. This agent:

- **Grounds** every question in the real schema and a **value index** (distinct values of
  low-cardinality columns), so "west" maps to the real value `West`.
- **Routes** common questions to **QC playbooks** — reviewed, parameterised SQL:

  | Playbook | Finds |
  |---|---|
  | `gap_detection` | brand/region combinations missing a month |
  | `variance` | month-over-month changes above 30% |
  | `trend_anomaly` | months far above/below the brand/region average |
  | `coverage` | regions with no data at all for a brand |

- **Falls back to an LLM** (optional) for anything else, with the schema and allowed values
  in the prompt, then rejects any SQL that is not a single read-only `SELECT`.

## Quick start
```bash
python sample_data.py                     # synthetic DB with 3 planted data issues
python agent.py "Are there any missing months for Oncoviva?"
python agent.py "Show month over month changes for Cardiomax in the South"
pytest -q                                 # 6 tests
```

```
Q: Show month over month changes for Cardiomax in the South
Routed to: playbook:variance   Filters: {'brand': 'Cardiomax', 'region': 'South'}

brand | region | month | prev_trx | trx | pct_change
Cardiomax | South | 2026-09 | 800 | 2532 | 216.5
Cardiomax | South | 2026-10 | 2532 | 828 | -67.3
```

Optional LLM fallback: `export ANTHROPIC_API_KEY=...` (model can be changed with `ANTHROPIC_MODEL`).
No other dependencies — the agent uses only the Python standard library.

## Ideas for extension
- Embedding-based retrieval over table/column descriptions for larger schemas
- An evaluation set of question → expected-rows pairs to measure accuracy
- Run against Databricks SQL / Unity Catalog instead of SQLite

*All data is synthetic; brand names are made up.*
