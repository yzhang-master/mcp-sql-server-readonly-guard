"""Bounded SQLite execution with defense-in-depth read-only enforcement."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .sql import SQLValidationError, parse_select


class QueryError(RuntimeError):
    pass


class QueryDenied(QueryError):
    pass


class QueryTimeout(QueryError):
    pass


class QueryCancelled(QueryError):
    pass


@dataclass(frozen=True)
class QueryLimits:
    max_rows: int = 100
    timeout_ms: int = 1_000
    progress_interval: int = 100

    def __post_init__(self) -> None:
        if self.max_rows <= 0 or self.timeout_ms <= 0 or self.progress_interval <= 0:
            raise ValueError("query limits must be positive")


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool
    elapsed_ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "columns": list(self.columns),
            "rows": [list(row) for row in self.rows],
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
        }


_ALLOWED_FUNCTIONS = {
    "abs",
    "avg",
    "char",
    "coalesce",
    "concat",
    "concat_ws",
    "count",
    "format",
    "glob",
    "group_concat",
    "hex",
    "ifnull",
    "iif",
    "instr",
    "json",
    "json_array",
    "json_array_length",
    "json_extract",
    "json_group_array",
    "json_group_object",
    "json_object",
    "json_quote",
    "json_type",
    "json_valid",
    "last_insert_rowid",
    "length",
    "like",
    "likelihood",
    "likely",
    "lower",
    "ltrim",
    "max",
    "min",
    "nullif",
    "octet_length",
    "printf",
    "quote",
    "random",
    "replace",
    "round",
    "rtrim",
    "sign",
    "soundex",
    "sqlite_version",
    "substr",
    "substring",
    "sum",
    "total",
    "trim",
    "typeof",
    "unhex",
    "unicode",
    "unlikely",
    "upper",
}
_ALLOWED_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}


def _authorizer(action: int, arg1: str | None, arg2: str | None, *_: Any) -> int:
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        function_name = (arg2 or arg1 or "").lower()
        if function_name not in _ALLOWED_FUNCTIONS:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    return value


class QueryEngine:
    def __init__(self, database: str | Path, limits: QueryLimits | None = None):
        self.database = Path(database).resolve()
        self.limits = limits or QueryLimits()

    def execute(self, sql: str, cancel_event: threading.Event | None = None) -> QueryResult:
        started = time.perf_counter()
        deadline = started + self.limits.timeout_ms / 1_000
        cancellation = cancel_event or threading.Event()
        connection: sqlite3.Connection | None = None
        cursor: sqlite3.Cursor | None = None
        try:
            parse_select(sql)
            if cancellation.is_set():
                raise QueryCancelled("query cancelled before execution")
            uri = f"file:{quote(str(self.database))}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=0)
            connection.execute("PRAGMA query_only=ON")
            connection.set_authorizer(_authorizer)

            def progress() -> int:
                if cancellation.is_set():
                    return 1
                return int(time.perf_counter() >= deadline)

            connection.set_progress_handler(progress, self.limits.progress_interval)
            cursor = connection.cursor()
            cursor.execute(sql)
            columns = tuple(item[0] for item in cursor.description or ())
            rows: list[tuple[Any, ...]] = []
            truncated = False
            while True:
                if cancellation.is_set():
                    raise QueryCancelled("query cancelled")
                if time.perf_counter() >= deadline:
                    raise QueryTimeout(f"query exceeded {self.limits.timeout_ms} ms")
                row = cursor.fetchone()
                if row is None:
                    break
                if len(rows) == self.limits.max_rows:
                    truncated = True
                    break
                rows.append(tuple(_json_value(value) for value in row))
            elapsed_ms = (time.perf_counter() - started) * 1_000
            return QueryResult(columns, tuple(rows), truncated, elapsed_ms)
        except SQLValidationError as exc:
            raise QueryDenied(str(exc)) from exc
        except sqlite3.OperationalError as exc:
            if cancellation.is_set():
                raise QueryCancelled("query cancelled") from exc
            if time.perf_counter() >= deadline:
                raise QueryTimeout(f"query exceeded {self.limits.timeout_ms} ms") from exc
            message = str(exc).lower()
            if any(
                text in message
                for text in ("not authorized", "authorization denied", "no such function")
            ):
                raise QueryDenied("SQLite authorizer denied the query") from exc
            raise QueryError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise QueryError(str(exc)) from exc
        finally:
            if connection is not None:
                connection.set_progress_handler(None, 0)
                connection.set_authorizer(None)
            if cursor is not None:
                cursor.close()
            if connection is not None:
                connection.close()
