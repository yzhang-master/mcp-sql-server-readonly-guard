from mcp_sql_guard import healthcheck


def test_package_smoke() -> None:
    assert healthcheck()
