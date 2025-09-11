"""MCP-style newline-delimited JSON-RPC 2.0 stdio server."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TextIO

from .execution import QueryEngine, QueryError, QueryLimits

PROTOCOL_VERSION = "2025-03-26"
_QUERY_TOOL = {
    "name": "query",
    "description": "Execute one authorized SELECT statement against the read-only database",
    "inputSchema": {
        "type": "object",
        "properties": {"sql": {"type": "string"}},
        "required": ["sql"],
        "additionalProperties": False,
    },
}


class MCPStdioServer:
    def __init__(
        self,
        engine: QueryEngine,
        input_stream: TextIO = sys.stdin,
        output_stream: TextIO = sys.stdout,
        max_workers: int = 4,
    ):
        self.engine = engine
        self.input = input_stream
        self.output = output_stream
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._write_lock = threading.Lock()
        self._requests_lock = threading.Lock()
        self._cancellations: dict[str | int, threading.Event] = {}

    def serve(self) -> None:
        try:
            for line in self.input:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                    self._dispatch(message)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    self._send_error(None, -32600, str(exc))
        finally:
            with self._requests_lock:
                cancellations = list(self._cancellations.values())
            for event in cancellations:
                event.set()
            self._executor.shutdown(wait=True, cancel_futures=True)

    def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            self._send_error(None, -32600, "expected a JSON-RPC 2.0 object")
            return
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/cancelled":
            params = message.get("params") or {}
            with self._requests_lock:
                event = self._cancellations.get(params.get("requestId"))
            if event is not None:
                event.set()
            return
        if request_id is None:
            return
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            self._send_error(None, -32600, "request id must be a string or integer")
            return
        if method == "initialize":
            self._send_result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "mcp-sql-readonly", "version": "0.1.0"},
                },
            )
        elif method == "ping":
            self._send_result(request_id, {})
        elif method == "tools/list":
            self._send_result(request_id, {"tools": [_QUERY_TOOL]})
        elif method == "tools/call":
            self._start_query(request_id, message.get("params"))
        else:
            self._send_error(request_id, -32601, f"unknown method: {method}")

    def _start_query(self, request_id: str | int, params: Any) -> None:
        if not isinstance(params, dict) or params.get("name") != "query":
            self._send_error(request_id, -32602, "expected the query tool")
            return
        arguments = params.get("arguments")
        if (
            not isinstance(arguments, dict)
            or set(arguments) != {"sql"}
            or not isinstance(arguments["sql"], str)
        ):
            self._send_error(request_id, -32602, "arguments must contain only a SQL string")
            return
        event = threading.Event()
        with self._requests_lock:
            if request_id in self._cancellations:
                self._send_error(request_id, -32600, "duplicate request id")
                return
            self._cancellations[request_id] = event
        future = self._executor.submit(self.engine.execute, arguments["sql"], event)
        future.add_done_callback(lambda done: self._finish_query(request_id, done))

    def _finish_query(self, request_id: str | int, future: Any) -> None:
        with self._requests_lock:
            self._cancellations.pop(request_id, None)
        try:
            result = future.result()
            payload = result.as_dict()
            is_error = False
        except QueryError as exc:
            payload = {"error": type(exc).__name__, "message": str(exc)}
            is_error = True
        except Exception as exc:
            self._send_error(request_id, -32603, f"internal query error: {exc}")
            return
        self._send_result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
                "structuredContent": payload,
                "isError": is_error,
            },
        )

    def _send_result(self, request_id: str | int | None, result: dict[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _send_error(self, request_id: str | int | None, code: int, message: str) -> None:
        error = {"code": code, "message": message}
        self._write({"jsonrpc": "2.0", "id": request_id, "error": error})

    def _write(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            self.output.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.output.flush()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--max-rows", type=int, default=100)
    parser.add_argument("--timeout-ms", type=int, default=1_000)
    args = parser.parse_args(argv)
    limits = QueryLimits(max_rows=args.max_rows, timeout_ms=args.timeout_ms)
    MCPStdioServer(QueryEngine(args.database, limits)).serve()


if __name__ == "__main__":
    main()
