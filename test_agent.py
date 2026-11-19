import sqlite3

import pytest

from agent import SchemaContext, ask, extract_filters, is_safe_select, route
from sample_data import build


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return build(str(tmp_path_factory.mktemp("d") / "sales.db"))


def test_value_index_grounds_filters(db):
    ctx = SchemaContext.from_db(sqlite3.connect(db))
    assert extract_filters("missing data for oncoviva in the west?", ctx) == {
        "brand": "Oncoviva", "region": "West"}


def test_routing():
    assert route("Any missing months?") == "gap_detection"
    assert route("Show big month over month changes") == "variance"
    assert route("Any spikes?") == "trend_anomaly"
    assert route("Which regions are not covered?") == "coverage"
    assert route("What is the total TRx?") is None


def test_gap_detection_finds_planted_gap(db):
    res = ask("Are there missing months for Oncoviva?", db)
    assert res["rows"] == [("Oncoviva", "West", "2026-06")]


def test_anomaly_finds_planted_spike(db):
    res = ask("Any spikes for Cardiomax?", db)
    assert ("Cardiomax", "South", "2026-09") in [r[:3] for r in res["rows"]]


def test_coverage_finds_missing_region(db):
    res = ask("Which regions are not covered for Oncoviva?", db)
    assert res["rows"] == [("Oncoviva", "Central")]


def test_guard_blocks_writes():
    assert is_safe_select("SELECT * FROM rx_sales")
    assert not is_safe_select("DELETE FROM rx_sales")
    assert not is_safe_select("SELECT 1; DROP TABLE rx_sales")
