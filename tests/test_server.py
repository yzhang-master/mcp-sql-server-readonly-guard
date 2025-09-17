import io
import json
import sqlite3

from mcp_sql_guard.execution import QueryEngine, QueryLimits
from mcp_sql_guard.server import MCPStdioServer


def message(request_id, method, params=None):
    value = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        value["params"] = params
    return value


def run_server(database, messages):
    input_stream = io.StringIO("".join(json.dumps(item) + "\n" for item in messages))
    output_stream = io.StringIO()
    MCPStdioServer(QueryEngine(database), input_stream, output_stream).serve()
    return [json.loads(line) for line in output_stream.getvalue().splitlines()]


def test_initialize_list_and_call_query_tool(tmp_path) -> None:
    database = tmp_path / "server.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE numbers(value INTEGER)")
        connection.execute("INSERT INTO numbers VALUES (42)")
    responses = run_server(
        database,
        [
            message(1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            message(2, "tools/list"),
            message(
                "q", "tools/call", {"name": "query", "arguments": {"sql": "SELECT * FROM numbers"}}
            ),
        ],
    )
    assert responses[0]["result"]["capabilities"]["tools"] == {"listChanged": False}
    assert responses[1]["result"]["tools"][0]["name"] == "query"
    query_response = responses[2]["result"]
    assert not query_response["isError"]
    assert query_response["structuredContent"]["rows"] == [[42]]


def test_denied_query_is_a_tool_error_not_a_protocol_crash(tmp_path) -> None:
    database = tmp_path / "server.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items(value INTEGER)")
    responses = run_server(
        database,
        [message(8, "tools/call", {"name": "query", "arguments": {"sql": "DROP TABLE items"}})],
    )
    result = responses[0]["result"]
    assert result["isError"]
    assert result["structuredContent"]["error"] == "QueryDenied"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 0


def test_json_rpc_validation_errors(tmp_path) -> None:
    database = tmp_path / "empty.db"
    with sqlite3.connect(database):
        pass
    responses = run_server(
        database,
        [
            message(1, "unknown/method"),
            message(2, "tools/call", {"name": "query", "arguments": {"sql": 3}}),
        ],
    )
    assert responses[0]["error"]["code"] == -32601
    assert responses[1]["error"]["code"] == -32602


def test_cancellation_notification_stops_in_flight_query(tmp_path) -> None:
    database = tmp_path / "cancel.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE items(value INTEGER)")
    input_stream = io.StringIO(
        json.dumps(
            message(
                "long",
                "tools/call",
                {
                    "name": "query",
                    "arguments": {
                        "sql": (
                            "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) "
                            "SELECT sum(x) FROM n"
                        )
                    },
                },
            )
        )
        + "\n"
        + json.dumps(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "long"}}
        )
        + "\n"
    )
    output_stream = io.StringIO()
    engine = QueryEngine(database, QueryLimits(timeout_ms=10_000, progress_interval=1))
    MCPStdioServer(engine, input_stream, output_stream).serve()
    response = json.loads(output_stream.getvalue())
    assert response["result"]["isError"]
    assert response["result"]["structuredContent"]["error"] == "QueryCancelled"
