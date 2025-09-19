"""Measure authorized-query latency and adversarial-query blocking on local SQLite."""

import argparse
import json
import math
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path

from mcp_sql_guard.execution import QueryDenied, QueryEngine, QueryLimits

ATTACKS = (
    "DELETE FROM items",
    "INSERT INTO items VALUES (999, 'attack')",
    "UPDATE items SET value = 'attack'",
    "DROP TABLE items",
    "SELECT 1; DELETE FROM items",
    "SELECT 1; -- evade\n INSERT INTO items VALUES (1000, 'attack')",
    "WITH x AS (DELETE FROM items RETURNING *) SELECT * FROM x",
    "WITH x AS (UPDATE items SET value = 'attack' RETURNING *) SELECT * FROM x",
    "SELECT load_extension('/tmp/not-loaded')",
)


def percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires observations")
    ordered = sorted(values)
    return ordered[math.ceil(probability * len(ordered)) - 1]


def run(database: Path, repeat: int) -> dict:
    engine = QueryEngine(database, QueryLimits(max_rows=100, timeout_ms=1_000))
    latencies_ms: list[float] = []
    blocked_latencies_ms: list[float] = []
    blocked = 0
    for _ in range(repeat):
        started = time.perf_counter()
        result = engine.execute("SELECT id, upper(value) FROM items ORDER BY id")
        latencies_ms.append((time.perf_counter() - started) * 1_000)
        assert len(result.rows) == 20
        for sql in ATTACKS:
            started = time.perf_counter()
            try:
                engine.execute(sql)
            except QueryDenied:
                blocked += 1
            finally:
                blocked_latencies_ms.append((time.perf_counter() - started) * 1_000)
    with sqlite3.connect(database) as connection:
        remaining_rows = connection.execute("SELECT count(*) FROM items").fetchone()[0]
    attempts = repeat * len(ATTACKS)
    return {
        "timestamp": datetime.now().astimezone().isoformat(),
        "sqlite_version": sqlite3.sqlite_version,
        "repetitions": repeat,
        "authorized_queries": repeat,
        "authorized_latency_ms": {
            "p50": percentile(latencies_ms, 0.50),
            "p95": percentile(latencies_ms, 0.95),
            "mean": sum(latencies_ms) / len(latencies_ms),
        },
        "attack_queries": attempts,
        "blocked_queries": blocked,
        "blocked_query_rate": blocked / attempts,
        "blocked_latency_ms": {
            "p50": percentile(blocked_latencies_ms, 0.50),
            "p95": percentile(blocked_latencies_ms, 0.95),
        },
        "database_rows_after_attacks": remaining_rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=50)
    parser.add_argument("--output", type=Path, default=Path("results/benchmark.json"))
    args = parser.parse_args()
    if args.repeat <= 0:
        parser.error("--repeat must be positive")
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "benchmark.db"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
            connection.executemany(
                "INSERT INTO items VALUES (?, ?)", [(index, f"item-{index}") for index in range(20)]
            )
        report = run(database, args.repeat)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
