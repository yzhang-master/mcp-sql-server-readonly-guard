import pytest

from mcp_sql_guard.sql import SQLValidationError, parse_select, tokenize


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT '; -- not a comment' AS value",
        "SELECT /* comment ; DELETE FROM items */ 1",
        "SELECT value FROM items WHERE name = 'a''b'",
        "SELECT * FROM (SELECT 1 AS value) AS nested",
        "SELECT 1 UNION ALL SELECT 2",
        "WITH chosen AS (SELECT 1 AS value) SELECT value FROM chosen",
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT x FROM n",
        "WITH x(v) AS MATERIALIZED (SELECT 1) SELECT v FROM x;",
    ],
)
def test_authorized_select_syntax(sql: str) -> None:
    statement = parse_select(sql)
    assert statement.body.cores


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "INSERT INTO items VALUES (1)",
        "UPDATE items SET value = 2",
        "DELETE FROM items",
        "REPLACE INTO items VALUES (3)",
        "DROP TABLE items",
        "ALTER TABLE items ADD COLUMN other TEXT",
        "CREATE TABLE other(value)",
        "PRAGMA writable_schema=ON",
        "ATTACH DATABASE 'other.db' AS other",
        "VACUUM",
        "EXPLAIN SELECT 1",
        "VALUES (1)",
        "SELECT 1; DELETE FROM items",
        "SELECT 1; -- hidden\n DELETE FROM items",
        "SELECT 1 /* */ ; /* */ DROP TABLE items",
        "WITH gone AS (DELETE FROM items RETURNING *) SELECT * FROM gone",
        "WITH changed AS (UPDATE items SET value = 0 RETURNING value) SELECT * FROM changed",
        "WITH made AS (INSERT INTO items VALUES (9) RETURNING value) SELECT * FROM made",
        "WITH x AS (SELECT 1) DELETE FROM items",
        "SELECT * FROM (DELETE FROM items RETURNING *)",
        "SELECT 1 /* unterminated",
        "SELECT (1",
    ],
)
def test_rejects_non_select_and_evasion(sql: str) -> None:
    with pytest.raises(SQLValidationError):
        parse_select(sql)


def test_comments_and_quoted_tokens_do_not_change_statement_boundaries() -> None:
    tokens = tokenize("SELECT 'a;--b', \"strange;name\" /* ; DELETE */")
    assert [token.value for token in tokens] == ["SELECT", "'a;--b'", ",", '"strange;name"']
