"""Create a small synthetic SQLite database of monthly prescriptions (TRx)
with a few data-quality issues deliberately planted for the agent to find."""
import random
import sqlite3

BRANDS = ["Cardiomax", "Oncoviva"]
REGIONS = ["North", "South", "East", "West"]
MONTHS = [f"2026-{m:02d}" for m in range(1, 13)]


def build(path: str = "sales.db") -> str:
    random.seed(42)
    con = sqlite3.connect(path)
    con.executescript("""
        DROP TABLE IF EXISTS rx_sales;
        CREATE TABLE rx_sales (
            brand  TEXT    NOT NULL,  -- product brand
            region TEXT    NOT NULL,  -- sales region
            month  TEXT    NOT NULL,  -- YYYY-MM
            trx    INTEGER NOT NULL   -- total prescriptions
        );
    """)
    rows = []
    for brand in BRANDS:
        for region in REGIONS:
            base = random.randint(800, 1200)
            for month in MONTHS:
                # Planted issue 1 - gap: Oncoviva / West has no data for 2026-06
                if brand == "Oncoviva" and region == "West" and month == "2026-06":
                    continue
                trx = base + random.randint(-40, 40)
                # Planted issue 2 - spike: Cardiomax / South jumps in 2026-09
                if brand == "Cardiomax" and region == "South" and month == "2026-09":
                    trx *= 3
                rows.append((brand, region, month, trx))
    # Planted issue 3 - coverage: a new "Central" region is reported for Cardiomax only
    rows += [("Cardiomax", "Central", m, random.randint(300, 400)) for m in MONTHS]
    con.executemany("INSERT INTO rx_sales VALUES (?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return path


if __name__ == "__main__":
    print(f"Created {build()}")
