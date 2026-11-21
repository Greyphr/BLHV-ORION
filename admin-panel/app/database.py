"""Read/write PostgreSQL access for the admin API.

This service never runs migrations: the bot remains the only migration runner.
Writes are restricted to real tables, use parameterised statements, and refuse to
touch the migration ledger.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

PROTECTED_TABLES = frozenset({"schema_migrations"})
INTEGER_TYPES = frozenset({"smallint", "integer", "bigint"})
NUMBER_TYPES = INTEGER_TYPES | frozenset({"real", "double precision", "numeric", "money"})
JSON_TYPES = frozenset({"json", "jsonb"})
TEXT_TYPES = frozenset({"character varying", "character", "text", "citext"})
TIME_TYPES = frozenset({"timestamp without time zone", "timestamp with time zone", "date", "time without time zone"})
UNSUPPORTED_TYPES = frozenset({"bytea"})


def quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def coerce(value: Any, data_type: str) -> tuple[Any, str]:
    """Return (python value, optional SQL cast) for a column write."""
    data_type = (data_type or "").lower()
    if data_type in UNSUPPORTED_TYPES or data_type.startswith("_"):
        raise ValueError(f"Unsupported column type for editing: {data_type}")
    if data_type in INTEGER_TYPES:
        return int(value), ""
    if data_type in NUMBER_TYPES:
        return float(value), ""
    if data_type == "boolean":
        if isinstance(value, bool):
            return value, ""
        text = str(value).strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True, ""
        if text in {"false", "0", "no", "off"}:
            return False, ""
        raise ValueError(f"Invalid boolean value: {value!r}")
    if data_type in JSON_TYPES:
        parsed = value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON: {exc}") from exc
        return json.dumps(parsed), f"::{data_type}"
    if data_type in TIME_TYPES:
        return str(value), f"::{data_type}"
    if data_type in TEXT_TYPES:
        return str(value), ""
    return str(value), ""


class AdminDB:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Database pool is not available")
        return self.pool

    async def tables(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT t.table_name,
                   (xpath('/row/c/text()',
                          query_to_xml(format('SELECT count(*) AS c FROM %I.%I',
                                              t.table_schema, t.table_name),
                                       false, true, '')))[1]::text::bigint AS row_count
            FROM information_schema.tables t
            WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
            ORDER BY t.table_name
            """
        )
        return [{"name": row["table_name"], "rows": int(row["row_count"])} for row in rows]

    async def _columns(self, table: str) -> list[dict[str, str]]:
        rows = await self._pool().fetch(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1
            ORDER BY ordinal_position
            """,
            table,
        )
        return [{"name": row["column_name"], "type": row["data_type"]} for row in rows]

    async def _primary_key(self, table: str) -> list[str]:
        rows = await self._pool().fetch(
            """
            SELECT k.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage k
              ON k.constraint_name = tc.constraint_name
             AND k.table_schema = tc.table_schema
             AND k.table_name = tc.table_name
            WHERE tc.table_schema = 'public'
              AND tc.table_name = $1
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY k.ordinal_position
            """,
            table,
        )
        return [row["column_name"] for row in rows]

    async def validate_table(self, table: str, *, write: bool = False) -> dict[str, Any]:
        known = {row["name"] for row in await self.tables()}
        if table not in known:
            raise LookupError(f"Unknown table: {table}")
        if write and table in PROTECTED_TABLES:
            raise PermissionError(f"Table {table} is protected")
        columns = await self._columns(table)
        primary_key = await self._primary_key(table)
        return {"name": table, "columns": columns, "primary_key": primary_key}

    async def table_rows(
        self, table: str, *, limit: int = 50, offset: int = 0, sort: str = "", desc: bool = False
    ) -> dict[str, Any]:
        meta = await self.validate_table(table)
        column_names = [column["name"] for column in meta["columns"]]
        order = ""
        if sort and sort in column_names:
            order = f" ORDER BY {quote_ident(sort)} {'DESC' if desc else 'ASC'}"
        total = await self._pool().fetchval(f"SELECT count(*) FROM {quote_ident(table)}")
        rows = await self._pool().fetch(
            f"SELECT * FROM {quote_ident(table)}{order} LIMIT $1 OFFSET $2",
            min(max(limit, 1), 200),
            max(offset, 0),
        )
        return {
            "table": table,
            "columns": meta["columns"],
            "primary_key": meta["primary_key"],
            "total": int(total),
            "rows": [{key: jsonable(value) for key, value in dict(row).items()} for row in rows],
        }

    async def insert_row(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        meta = await self.validate_table(table, write=True)
        types = {column["name"]: column["type"] for column in meta["columns"]}
        names, placeholders, params = [], [], []
        for index, (column, value) in enumerate(values.items(), start=1):
            if column not in types:
                raise ValueError(f"Unknown column: {column}")
            coerced, cast = coerce(value, types[column])
            names.append(quote_ident(column))
            placeholders.append(f"${index}{cast}")
            params.append(coerced)
        if not names:
            raise ValueError("No values supplied")
        sql = f"INSERT INTO {quote_ident(table)} ({', '.join(names)}) VALUES ({', '.join(placeholders)}) RETURNING *"
        row = await self._pool().fetchrow(sql, *params)
        return {key: jsonable(value) for key, value in dict(row).items()}

    async def update_row(self, table: str, key: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
        meta = await self.validate_table(table, write=True)
        pk = meta["primary_key"]
        if not pk or set(key) != set(pk):
            raise ValueError(f"Primary key required: {', '.join(pk) or '(none)'}")
        types = {column["name"]: column["type"] for column in meta["columns"]}
        assignments, params = [], []
        for column, value in values.items():
            if column not in types:
                raise ValueError(f"Unknown column: {column}")
            coerced, cast = coerce(value, types[column])
            params.append(coerced)
            assignments.append(f"{quote_ident(column)} = ${len(params)}{cast}")
        for column in pk:
            params.append(key[column])
            assignments.append(f"{quote_ident(column)} = ${len(params)}")
        sql = f"UPDATE {quote_ident(table)} SET {', '.join(assignments[:-len(pk)])} WHERE {' AND '.join(assignments[-len(pk):])} RETURNING *"
        row = await self._pool().fetchrow(sql, *params)
        if row is None:
            raise LookupError("Row not found")
        return {key_: jsonable(value) for key_, value in dict(row).items()}

    async def delete_row(self, table: str, key: dict[str, Any]) -> bool:
        meta = await self.validate_table(table, write=True)
        pk = meta["primary_key"]
        if not pk or set(key) != set(pk):
            raise ValueError(f"Primary key required: {', '.join(pk) or '(none)'}")
        clauses, params = [], []
        for column in pk:
            params.append(key[column])
            clauses.append(f"{quote_ident(column)} = ${len(params)}")
        result = await self._pool().execute(
            f"DELETE FROM {quote_ident(table)} WHERE {' AND '.join(clauses)}", *params
        )
        return result == "DELETE 1"

    async def config_all(self) -> list[dict[str, Any]]:
        rows = await self._pool().fetch("SELECT key, value FROM config ORDER BY key")
        return [{"key": row["key"], "value": jsonable(row["value"])} for row in rows]

    async def config_set(self, key: str, value: Any) -> None:
        await self._pool().execute(
            "INSERT INTO config (key, value) VALUES ($1, $2::jsonb) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            key,
            json.dumps(value),
        )

    async def migrations(self) -> list[dict[str, Any]]:
        try:
            rows = await self._pool().fetch(
                "SELECT version, applied_at FROM schema_migrations ORDER BY version"
            )
        except asyncpg.UndefinedTableError:
            return []
        return [
            {"version": row["version"], "applied_at": jsonable(row["applied_at"])} for row in rows
        ]

    async def _count(self, table: str) -> int:
        try:
            return int(await self._pool().fetchval(f"SELECT count(*) FROM {quote_ident(table)}"))
        except asyncpg.UndefinedTableError:
            return 0

    async def overview(self) -> dict[str, Any]:
        names = (
            "users", "tickets", "giveaways", "announcements", "scheduled_announcements",
            "polls", "mod_cases", "mod_reports", "events", "achievements", "ai_logs",
        )
        counts = {name: await self._count(name) for name in names}
        db_size = await self._pool().fetchval(
            "SELECT pg_size_pretty(pg_database_size(current_database()))"
        )
        return {**dict(counts), "database_size": db_size}
