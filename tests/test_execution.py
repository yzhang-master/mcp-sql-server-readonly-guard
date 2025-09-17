import sqlite3
import threading
import time

import pytest

from mcp_sql_guard.execution import (
    QueryCancelled,
    QueryDenied,
    QueryEngine,
    QueryLimits,
    QueryTimeout,
)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "data.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
        connection.executemany("INSERT INTO items VALUES (?, ?)", [(1, "one"), (2, "two")])
        connection.execute("CREATE TABLE denied_effects(marker TEXT)")
    return path


def table_counts(database) -> tuple[int, int]:
    with sqlite3.connect(database) as connection:
        items = connection.execute("SELECT count(*) FROM items").fetchone()[0]
        effects = connection.execute("SELECT count(*) FROM denied_effects").fetchone()[0]
    return items, effects


def test_executes_real_sql_and_preserves_binary_values(database) -> None:
    result = QueryEngine(database).execute("SELECT id, value, x'00ff' FROM items ORDER BY id")
    assert result.columns == ("id", "value", "x'00ff'")
    assert result.rows == ((1, "one", {"hex": "00ff"}), (2, "two", {"hex": "00ff"}))
    assert not result.truncated


def test_row_limit_fetches_one_extra_row_and_truncates(database) -> None:
    result = QueryEngine(database, QueryLimits(max_rows=1)).execute("SELECT id FROM items")
    assert result.rows == ((1,),)
    assert result.truncated


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM items",
        "INSERT INTO denied_effects VALUES ('insert')",
        "UPDATE items SET value = 'changed'",
        "SELECT 1; INSERT INTO denied_effects VALUES ('multi')",
        "WITH x AS (INSERT INTO denied_effects VALUES ('cte') RETURNING *) SELECT * FROM x",
        "SELECT 1; -- comment\nDELETE FROM items",
        "SELECT load_extension('/tmp/not-loaded')",
        "SELECT writefile('/tmp/not-written', 'no')",
        "ATTACH DATABASE ':memory:' AS other",
    ],
)
def test_denied_queries_have_no_effects(database, sql: str) -> None:
    before = table_counts(database)
    with pytest.raises(QueryDenied):
        QueryEngine(database).execute(sql)
    assert table_counts(database) == before


def test_time_limit_interrupts_long_recursive_query(database) -> None:
    sql = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT sum(x) FROM n"
    engine = QueryEngine(database, QueryLimits(timeout_ms=2, progress_interval=1))
    with pytest.raises(QueryTimeout):
        engine.execute(sql)


def test_cancellation_before_execution(database) -> None:
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(QueryCancelled):
        QueryEngine(database).execute("SELECT * FROM items", cancelled)


def test_cancellation_interrupts_running_query_and_cleans_connection(database) -> None:
    sql = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT sum(x) FROM n"
    cancelled = threading.Event()
    engine = QueryEngine(database, QueryLimits(timeout_ms=10_000, progress_interval=1))
    timer = threading.Timer(0.02, cancelled.set)
    started = time.monotonic()
    timer.start()
    try:
        with pytest.raises(QueryCancelled):
            engine.execute(sql, cancelled)
    finally:
        timer.cancel()
    assert time.monotonic() - started < 2
    assert QueryEngine(database).execute("SELECT count(*) FROM items").rows == ((2,),)


def test_connections_remain_usable_after_denial_and_timeout(database) -> None:
    engine = QueryEngine(database, QueryLimits(timeout_ms=2, progress_interval=1))
    for sql in ("DELETE FROM items", "SELECT load_extension('x')"):
        with pytest.raises(QueryDenied):
            engine.execute(sql)
    assert QueryEngine(database).execute("SELECT 1").rows == ((1,),)
