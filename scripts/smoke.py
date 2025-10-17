"""Exercise the actual stdio server in a subprocess without an MCP SDK."""

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


def request(request_id, method, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message)


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "smoke.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE items(value TEXT)")
            connection.execute("INSERT INTO items VALUES ('ok')")
        messages = [
            request(1, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}}),
            request(2, "tools/list"),
            request(
                3,
                "tools/call",
                {"name": "query", "arguments": {"sql": "SELECT value FROM items"}},
            ),
            request(
                4,
                "tools/call",
                {"name": "query", "arguments": {"sql": "SELECT 1; DROP TABLE items"}},
            ),
        ]
        process = subprocess.run(
            [sys.executable, "-m", "mcp_sql_guard.server", "--database", str(database)],
            input="\n".join(messages) + "\n",
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        by_id = {response["id"]: response for response in responses}
        assert set(by_id) == {1, 2, 3, 4}
        assert by_id[3]["result"]["structuredContent"]["rows"] == [["ok"]]
        assert by_id[4]["result"]["isError"]
        print(json.dumps(responses, indent=2))


if __name__ == "__main__":
    main()
