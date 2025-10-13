# mcp-sql-server-readonly-guard

> **Era:** 2025+ · Staff AI Engineer focus area (agent tool security). Technique from the MCP era, reimplemented in 2026 with Python 3.12 and the standard library.

## Problem

An agent should be able to inspect a SQLite database without being able to modify it, attach another database, load an extension, or hide a second statement behind comments. The server also needs bounded result sizes, deadlines, and cancellation so a query cannot monopolize the tool runtime.

## Method

The executable is a newline-delimited JSON-RPC 2.0 stdio server implementing MCP-style `initialize`, `tools/list`, `tools/call`, `ping`, and `notifications/cancelled`. It exposes one `query` tool. The official MCP Python SDK is not required.

```text
JSON-RPC request
      |
 tokenizer --> SELECT/WITH AST --> recursive CTE/subquery checks
                                      |
                              SQLite mode=ro connection
                              + query_only=ON
                              + action/function authorizer
                              + progress-handler deadline/cancel
                                      |
                              bounded cursor iteration
```

The parser accepts one `SELECT`, optionally preceded by read-only CTEs, including compounds and recursive CTEs. Comments are removed by the tokenizer rather than regular-expression substitution; quoted strings and identifiers remain tokens. Every CTE body and parenthesized statement beginning with a statement keyword is recursively parsed. Multiple statements and non-SELECT CTE/subqueries are rejected before execution. SQLite performs final grammar validation.

Execution uses three independent controls: a conservative AST policy, SQLite's authorizer (only `SELECT`, `READ`, `RECURSIVE`, and allowlisted functions), and a `mode=ro` plus `query_only=ON` connection. Each request owns its connection and cursor. The progress handler checks a monotonic deadline and request-local cancellation event; `finally` removes callbacks and closes both resources. At most `max_rows` rows are returned, with one extra row fetched to set `truncated`.

## Results

Measured on this CPU-only development machine with SQLite 3.45.1, 20 rows, 50 repetitions, and nine attack templates per repetition (`results/benchmark.json`):

| Metric | Result |
|---|---:|
| Attack attempts | 450 |
| Blocked | 450 (**100%**) |
| Rows remaining after attacks | 20 |
| Authorized SELECT p50 / p95 | 0.401 / 0.815 ms |
| Authorized SELECT mean | 0.442 ms |
| Blocked-query p50 / p95 | 0.039 / 0.217 ms |

The 53 tests execute real SQLite queries. They prove that writes, multi-statements, CTE writes, comment/semicolon evasion, attachment, and dangerous functions are denied without table changes; they also cover row truncation, deadlines, pre-start and in-flight cancellation, subsequent connection reuse, binary JSON encoding, and MCP tool errors. Key validation and authorizer behavior was mutation-checked by deliberately weakening it and confirming test failure.

## Reproduce

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check .
pytest -q
python scripts/benchmark.py
python scripts/smoke.py
python -m mcp_sql_guard.server --database ./existing.db --max-rows 100 --timeout-ms 1000
```

`smoke.py` starts the actual server subprocess and checks both an allowed query and a denied multi-statement write. All benchmark values are regenerated locally; no remote service or API key is used.

## Limitations

- This is MCP-style protocol support, not a compatibility test against every official SDK/version. It uses newline-delimited JSON-RPC over stdio, not HTTP transport.
- The SQL frontend is deliberately conservative and SQLite-specific. It is not a complete SQLite grammar; SQLite itself rejects malformed residual syntax. Only the query tool and read-only scalar/aggregate functions on the allowlist are available.
- The database path is operator-supplied and trusted. This is query authorization, not an OS sandbox for a malicious database file or SQLite extension.
- Deadlines and cancellation are cooperative at SQLite progress-handler intervals. The server does not enforce CPU, memory, or concurrency quotas beyond its worker pool and query limits.
- The benchmark is a small synthetic corpus, not a proof against every SQLite parser discrepancy. Defense in depth comes from combining AST validation, authorizer callbacks, and read-only connections.

## References

- [Model Context Protocol specification](https://modelcontextprotocol.io/specification/2025-03-26)
- [SQLite authorizer interface](https://www.sqlite.org/c3ref/set_authorizer.html)
- [SQLite URI filenames and `mode=ro`](https://www.sqlite.org/uri.html)
- [SQLite progress handler](https://www.sqlite.org/c3ref/progress_handler.html)
