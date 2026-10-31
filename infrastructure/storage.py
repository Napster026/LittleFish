"""SQLite 适配器：结构迁移、原始行读写与事务边界。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from persistence.errors import StorageNotOpenError, UnsupportedSchemaError
from persistence.ports import Row, PersistenceSession


_SCHEMA_V1 = (
    """CREATE TABLE IF NOT EXISTS agent_states (
    agent_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version >= 0),
    state_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS agent_events (
    event_id TEXT PRIMARY KEY,
    event_json TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS state_settlements (
    proposal_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agent_states(agent_id),
    event_id TEXT NOT NULL REFERENCES agent_events(event_id),
    proposal_json TEXT NOT NULL,
    result_state_json TEXT NOT NULL,
    UNIQUE (agent_id, event_id)
    )""",
)

_MIGRATE_V1_TO_V2 = (
    """CREATE TABLE activity_instances (
        activity_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL REFERENCES agent_states(agent_id),
        revision INTEGER NOT NULL CHECK (revision >= 0),
        definition_json TEXT NOT NULL,
        instance_json TEXT NOT NULL
    )""",
    """CREATE TABLE activity_commits (
        activity_id TEXT NOT NULL REFERENCES activity_instances(activity_id),
        from_revision INTEGER NOT NULL CHECK (from_revision >= 0),
        request_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        PRIMARY KEY (activity_id, from_revision)
    )""",
)

_MIGRATE_V2_TO_V3 = (
    """CREATE TABLE scheduled_events (
        schedule_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL REFERENCES agent_states(agent_id),
        schedule_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'delivered', 'failed')),
        attempts INTEGER NOT NULL CHECK (attempts >= 0),
        next_attempt_at TEXT NOT NULL,
        last_error TEXT
    )""",
    "CREATE INDEX scheduled_due_idx ON scheduled_events (agent_id, status, next_attempt_at, schedule_id)",
)


_COLUMNS = {
    "agent_states": {"agent_id", "version", "state_json"},
    "agent_events": {"event_id", "event_json"},
    "state_settlements": {"proposal_id", "agent_id", "event_id", "proposal_json", "result_state_json"},
    "activity_instances": {"activity_id", "agent_id", "revision", "definition_json", "instance_json"},
    "activity_commits": {"activity_id", "from_revision", "request_json", "result_json"},
    "scheduled_events": {"schedule_id", "agent_id", "schedule_json", "status", "attempts", "next_attempt_at", "last_error"},
}


class _SQLiteSession:
    """只在创建它的事务中有效；列名与表名严格使用白名单。"""

    def __init__(self, connection: aiosqlite.Connection):
        self.connection = connection
        self.active = True

    def _check(self, table: str, columns) -> None:
        if not self.active:
            raise StorageNotOpenError("事务会话已经结束")
        if table not in _COLUMNS or any(column not in _COLUMNS[table] for column in columns):
            raise ValueError("未知表或列")

    async def fetch_one(self, table: str, *, where: Row) -> Row | None:
        rows = await self.fetch_all(table, where=where, limit=1)
        return rows[0] if rows else None

    async def fetch_all(
        self, table: str, *, where: Row | None = None, at_most: Row | None = None,
        after: Row | None = None, order_by: tuple[str, ...] = (), limit: int = 100,
    ) -> tuple[Row, ...]:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit 必须是正整数")
        clauses, parameters = [], []
        self._check(table, order_by)
        for filters, operator in ((where, "="), (at_most, "<="), (after, ">")):
            self._check(table, filters or {})
            for key, value in (filters or {}).items():
                if value is None:
                    if operator != "=":
                        raise ValueError("范围条件不能与 NULL 比较")
                    clauses.append(f'"{key}" IS NULL')
                    continue
                clauses.append(f'"{key}" {operator} ?')
                parameters.append(value)
        sql = f'SELECT * FROM "{table}"'
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if order_by:
            sql += " ORDER BY " + ", ".join(f'"{key}"' for key in order_by)
        sql += " LIMIT ?"
        async with self.connection.execute(sql, (*parameters, limit)) as cursor:
            return tuple(dict(row) for row in await cursor.fetchall())

    async def insert(self, table: str, values: Row) -> None:
        self._check(table, values)
        if not values:
            raise ValueError("插入字段不能为空")
        columns = ", ".join(f'"{key}"' for key in values)
        placeholders = ", ".join("?" for _ in values)
        async with self.connection.execute(
            f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', tuple(values.values()),
        ):
            pass

    async def update(self, table: str, values: Row, *, where: Row) -> int:
        self._check(table, (*values, *where))
        if not values or not where:
            raise ValueError("更新字段与匹配条件不能为空")
        assignments = ", ".join(f'"{key}" = ?' for key in values)
        conditions = " AND ".join(
            f'"{key}" IS NULL' if value is None else f'"{key}" = ?' for key, value in where.items()
        )
        async with self.connection.execute(
            f'UPDATE "{table}" SET {assignments} WHERE {conditions}',
            (*values.values(), *(value for value in where.values() if value is not None)),
        ) as cursor:
            return cursor.rowcount


class SQLiteStorage:
    """仅负责 SQLite 连接、v1/v2→v3 迁移及行级事务，不解释业务 JSON。"""

    def __init__(self, database_path: str | Path):
        self.database_path = str(database_path)
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> SQLiteStorage:
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()

    async def open(self) -> None:
        async with self._lock:
            if self._connection is not None:
                return
            connection = await aiosqlite.connect(self.database_path, isolation_level=None)
            connection.row_factory = aiosqlite.Row
            try:
                for sql in ("PRAGMA foreign_keys = ON", "PRAGMA busy_timeout = 5000", "BEGIN IMMEDIATE"):
                    async with connection.execute(sql):
                        pass
                async with connection.execute("PRAGMA user_version") as cursor:
                    version = (await cursor.fetchone())[0]
                if version not in (0, 1, 2, 3):
                    raise UnsupportedSchemaError(f"不支持数据库结构版本 {version}")
                if version == 0:
                    for sql in _SCHEMA_V1:
                        async with connection.execute(sql):
                            pass
                    version = 1
                if version == 1:
                    for sql in _MIGRATE_V1_TO_V2:
                        async with connection.execute(sql):
                            pass
                    version = 2
                if version == 2:
                    for sql in _MIGRATE_V2_TO_V3:
                        async with connection.execute(sql):
                            pass
                    async with connection.execute("PRAGMA user_version = 3"):
                        pass
                await connection.commit()
            except BaseException:
                try:
                    await connection.rollback()
                finally:
                    await connection.close()
                raise
            self._connection = connection

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[PersistenceSession]:
        async with self._lock:
            connection = self._connection
            if connection is None:
                raise StorageNotOpenError("请先打开数据库")
            session = _SQLiteSession(connection)
            try:
                async with connection.execute("BEGIN IMMEDIATE"):
                    pass
                yield session
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
            finally:
                session.active = False
