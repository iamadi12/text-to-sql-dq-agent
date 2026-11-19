"""A grounded text-to-SQL agent for data-quality questions.

How it answers a question:
1. Grounding  - reads the database schema and builds a *value index*
                (distinct values of text columns), so filters like "Oncoviva"
                or "west" map to real values instead of hallucinated ones.
2. Routing    - sends the question to a tested QC playbook
                (gap detection, MoM variance, trend anomaly, coverage).
3. Fallback   - questions no playbook covers go to an LLM (optional,
                set ANTHROPIC_API_KEY) with the schema and value index in the
                prompt; the generated SQL must pass a read-only guard.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.request
from dataclasses import dataclass, field

# ---------------------------------------------------------------- grounding

@dataclass
class SchemaContext:
    tables: dict[str, list[str]] = field(default_factory=dict)
    value_index: dict[str, dict[str, set[str]]] = field(default_factory=dict)

    @classmethod
    def from_db(cls, con: sqlite3.Connection, max_distinct: int = 50) -> "SchemaContext":
        ctx = cls()
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            cols = con.execute(f"PRAGMA table_info({table})").fetchall()
            ctx.tables[table] = [f"{c[1]} {c[2]}" for c in cols]
            ctx.value_index[table] = {}
            for c in cols:
                if c[2].upper() == "TEXT":
                    vals = con.execute(
                        f"SELECT DISTINCT {c[1]} FROM {table} LIMIT {max_distinct + 1}").fetchall()
                    if len(vals) <= max_distinct:  # only index low-cardinality columns
                        ctx.value_index[table][c[1]] = {v[0] for v in vals}
        return ctx

    def describe(self) -> str:
        lines = []
        for t, cols in self.tables.items():
            lines.append(f"TABLE {t} ({', '.join(cols)})")
            for col, vals in self.value_index.get(t, {}).items():
                lines.append(f"  allowed values for {col}: {sorted(vals)}")
        return "\n".join(lines)


def extract_filters(question: str, ctx: SchemaContext, table: str = "rx_sales") -> dict[str, str]:
    """Map words in the question to real column values via the value index."""
    q = question.lower()
    found = {}
    for col, vals in ctx.value_index.get(table, {}).items():
        for v in vals:
            if re.search(rf"\b{re.escape(v.lower())}\b", q):
                found[col] = v
    return found

# ---------------------------------------------------------------- playbooks

PLAYBOOKS = {
    "gap_detection": {
        "keywords": ["gap", "missing", "hole"],
        "description": "brand/region combinations missing a month of data",
        "sql": """
            WITH months AS (SELECT DISTINCT month FROM rx_sales),
                 pairs  AS (SELECT DISTINCT brand, region FROM rx_sales {where})
            SELECT p.brand, p.region, m.month AS missing_month
            FROM pairs p CROSS JOIN months m
            LEFT JOIN rx_sales s
              ON s.brand = p.brand AND s.region = p.region AND s.month = m.month
            WHERE s.trx IS NULL
            ORDER BY 1, 2, 3""",
    },
    "variance": {
        "keywords": ["variance", "change", "drop", "jump", "mom", "month over month"],
        "description": "month-over-month TRx changes above 30%",
        "sql": """
            WITH t AS (
              SELECT brand, region, month, trx,
                     LAG(trx) OVER (PARTITION BY brand, region ORDER BY month) AS prev_trx
              FROM rx_sales {where})
            SELECT brand, region, month, prev_trx, trx,
                   ROUND(100.0 * (trx - prev_trx) / prev_trx, 1) AS pct_change
            FROM t
            WHERE prev_trx IS NOT NULL AND ABS(1.0 * (trx - prev_trx) / prev_trx) > 0.30
            ORDER BY ABS(pct_change) DESC""",
    },
    "trend_anomaly": {
        "keywords": ["anomaly", "anomalies", "spike", "outlier", "unusual"],
        "description": "months more than 1.5x or less than 0.5x the brand/region average",
        "sql": """
            WITH a AS (SELECT brand, region, AVG(trx) AS avg_trx
                       FROM rx_sales {where} GROUP BY brand, region)
            SELECT s.brand, s.region, s.month, s.trx, ROUND(a.avg_trx, 0) AS avg_trx
            FROM rx_sales s JOIN a ON s.brand = a.brand AND s.region = a.region
            WHERE s.trx > 1.5 * a.avg_trx OR s.trx < 0.5 * a.avg_trx
            ORDER BY s.trx / a.avg_trx DESC""",
    },
    "coverage": {
        "keywords": ["coverage", "which regions", "covered", "not reported"],
        "description": "regions with no data at all for a brand",
        "sql": """
            WITH b AS (SELECT DISTINCT brand FROM rx_sales {where}),
                 r AS (SELECT DISTINCT region FROM rx_sales)
            SELECT b.brand, r.region AS region_without_data
            FROM b CROSS JOIN r
            WHERE NOT EXISTS (SELECT 1 FROM rx_sales s
                              WHERE s.brand = b.brand AND s.region = r.region)
            ORDER BY 1, 2""",
    },
}


def route(question: str) -> str | None:
    q = question.lower()
    for name, pb in PLAYBOOKS.items():
        if any(k in q for k in pb["keywords"]):
            return name
    return None


def build_playbook_sql(name: str, filters: dict[str, str]) -> tuple[str, list[str]]:
    where, params = "", []
    if filters:
        where = "WHERE " + " AND ".join(f"{col} = ?" for col in filters)
        params = list(filters.values())
    return PLAYBOOKS[name]["sql"].format(where=where), params

# ---------------------------------------------------------------- LLM fallback + guard

FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|pragma|replace)\b", re.I)


def is_safe_select(sql: str) -> bool:
    s = sql.strip().rstrip(";")
    return (s.lower().startswith(("select", "with")) and ";" not in s
            and not FORBIDDEN.search(s))


def llm_sql(question: str, ctx: SchemaContext) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("No playbook matched and ANTHROPIC_API_KEY is not set.")
    prompt = (f"You write SQLite queries.\n{ctx.describe()}\n"
              "Use only the tables, columns and allowed values above. "
              "Return one read-only SELECT statement and nothing else.\n"
              f"Question: {question}")
    body = json.dumps({"model": os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
                       "max_tokens": 400,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = json.load(r)["content"][0]["text"]
    return re.sub(r"^```(sql)?|```$", "", text.strip(), flags=re.M).strip()

# ---------------------------------------------------------------- agent

def ask(question: str, db: str = "sales.db") -> dict:
    con = sqlite3.connect(db)
    ctx = SchemaContext.from_db(con)
    filters = extract_filters(question, ctx)
    playbook = route(question)
    if playbook:
        sql, params = build_playbook_sql(playbook, filters)
        source = f"playbook:{playbook}"
    else:
        sql, params, source = llm_sql(question, ctx), [], "llm"
    if not is_safe_select(sql):
        raise ValueError("Generated SQL rejected by read-only guard")
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    con.close()
    return {"source": source, "filters": filters, "sql": " ".join(sql.split()),
            "columns": cols, "rows": rows}


def main() -> None:
    question = " ".join(sys.argv[1:]) or "Are there any missing months for Oncoviva?"
    res = ask(question)
    print(f"Q: {question}\nRouted to: {res['source']}   Filters: {res['filters']}\n")
    print(" | ".join(res["columns"]))
    for row in res["rows"]:
        print(" | ".join(str(v) for v in row))
    print(f"\n{len(res['rows'])} issue(s) found")


if __name__ == "__main__":
    main()
