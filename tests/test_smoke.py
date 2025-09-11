from mcp_sql_guard import parse_select


def test_package_smoke() -> None:
    assert parse_select("SELECT 1").body.cores
