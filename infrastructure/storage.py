"""SQLite 适配器：结构迁移、原始行读写与事务边界。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import aiosqlite

from ..persistence.errors import StorageNotOpenError, UnsupportedSchemaError
from ..persistence.ports import Row, PersistenceSession, SQLValue


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

_MIGRATE_V3_TO_V4 = (
    "ALTER TABLE activity_instances ADD COLUMN status TEXT NOT NULL DEFAULT 'unknown' "
    "CHECK (status IN ('unknown', 'pending', 'running', 'paused', 'completed', 'failed', 'cancelled'))",
    "CREATE INDEX activity_active_idx ON activity_instances (agent_id, status, activity_id)",
    """CREATE TABLE feedback_heads (
        agent_id TEXT NOT NULL REFERENCES agent_states(agent_id),
        scope_id TEXT NOT NULL,
        event_id TEXT NOT NULL REFERENCES agent_events(event_id),
        occurred_at TEXT NOT NULL,
        PRIMARY KEY (agent_id, scope_id)
    )""",
)

_MIGRATE_V4_TO_V5 = (
    """CREATE TABLE memories (
        memory_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL REFERENCES agent_states(agent_id),
        scope_id TEXT NOT NULL, kind TEXT NOT NULL, visibility TEXT NOT NULL,
        speaker_id TEXT, about_id TEXT, claim_type TEXT NOT NULL, provenance TEXT NOT NULL,
        topic_key TEXT NOT NULL, target_start_at TEXT, target_end_at TEXT, plan_status TEXT,
        sort_key TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 1), record_json TEXT NOT NULL
    )""",
    "CREATE INDEX memory_recent_idx ON memories (agent_id, scope_id, sort_key)",
    "CREATE INDEX memory_subject_idx ON memories (agent_id, scope_id, about_id, claim_type, sort_key)",
    "CREATE INDEX memory_merge_idx ON memories (agent_id, scope_id, about_id, claim_type, kind, provenance, sort_key)",
    "CREATE INDEX memory_target_idx ON memories (agent_id, scope_id, about_id, claim_type, target_end_at, memory_id)",
    """CREATE TABLE memory_revisions (
        memory_id TEXT NOT NULL REFERENCES memories(memory_id), revision INTEGER NOT NULL,
        action TEXT NOT NULL, source_event_id TEXT NOT NULL REFERENCES agent_events(event_id),
        record_json TEXT NOT NULL, PRIMARY KEY (memory_id, revision)
    )""",
    """CREATE TABLE memory_sources (
        memory_id TEXT NOT NULL REFERENCES memories(memory_id),
        source_event_id TEXT NOT NULL REFERENCES agent_events(event_id), draft_key TEXT NOT NULL,
        occurred_at TEXT NOT NULL, PRIMARY KEY (memory_id, source_event_id, draft_key)
    )""",
    "CREATE INDEX memory_source_time_idx ON memory_sources (memory_id, occurred_at, source_event_id)",
    """CREATE TABLE memory_terms (
        agent_id TEXT NOT NULL, scope_id TEXT NOT NULL, term TEXT NOT NULL,
        memory_id TEXT NOT NULL REFERENCES memories(memory_id), sort_key TEXT NOT NULL,
        PRIMARY KEY (agent_id, scope_id, term, memory_id)
    )""",
    "CREATE INDEX memory_term_recent_idx ON memory_terms (agent_id, scope_id, term, sort_key)",
    "CREATE INDEX memory_term_owner_idx ON memory_terms (memory_id)",
    """CREATE TABLE memory_receipts (
        agent_id TEXT NOT NULL REFERENCES agent_states(agent_id),
        source_event_id TEXT NOT NULL REFERENCES agent_events(event_id), draft_key TEXT NOT NULL,
        request_json TEXT NOT NULL, result_json TEXT NOT NULL,
        PRIMARY KEY (agent_id, source_event_id, draft_key)
    )""",
)

_MIGRATE_V5_TO_V6 = (
    "ALTER TABLE memories ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE memories ADD COLUMN index_version INTEGER NOT NULL DEFAULT 0 CHECK (index_version >= 0)",
    "CREATE INDEX memory_reindex_idx ON memories (index_version, memory_id)",
    "DROP INDEX memory_merge_idx",
    "CREATE INDEX memory_merge_time_idx ON memories (agent_id, scope_id, about_id, claim_type, kind, provenance, updated_at, memory_id)",
)

_MIGRATE_V6_TO_V7 = (
    """CREATE TABLE relationships (
        agent_id TEXT NOT NULL REFERENCES agent_states(agent_id), platform_id TEXT NOT NULL, user_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1), updated_at TEXT NOT NULL, profile_json TEXT NOT NULL,
        PRIMARY KEY (agent_id, platform_id, user_id)
    )""",
    """CREATE TABLE relationship_receipts (
        agent_id TEXT NOT NULL REFERENCES agent_states(agent_id),
        source_event_id TEXT NOT NULL REFERENCES agent_events(event_id), scope_id TEXT NOT NULL,
        platform_id TEXT, user_id TEXT, sort_key TEXT NOT NULL, receipt_json TEXT NOT NULL,
        PRIMARY KEY (agent_id, source_event_id)
    )""",
    "CREATE INDEX relationship_history_idx ON relationship_receipts (agent_id, platform_id, user_id, scope_id, sort_key)",
)

_COLUMNS = {
    "relationships": {"agent_id", "platform_id", "user_id", "revision", "updated_at", "profile_json"},
    "relationship_receipts": {"agent_id", "source_event_id", "scope_id", "platform_id", "user_id", "sort_key", "receipt_json"},
    "memories": {"memory_id", "agent_id", "scope_id", "kind", "visibility", "speaker_id", "about_id",
                 "claim_type", "provenance", "topic_key", "target_start_at", "target_end_at", "plan_status",
                 "sort_key", "revision", "record_json", "updated_at", "index_version"},
    "memory_revisions": {"memory_id", "revision", "action", "source_event_id", "record_json"},
    "memory_sources": {"memory_id", "source_event_id", "draft_key", "occurred_at"},
    "memory_terms": {"agent_id", "scope_id", "term", "memory_id", "sort_key"},
    "memory_receipts": {"agent_id", "source_event_id", "draft_key", "request_json", "result_json"},
    "agent_states": {"agent_id", "version", "state_json"},
    "agent_events": {"event_id", "event_json"},
    "state_settlements": {"proposal_id", "agent_id", "event_id", "proposal_json", "result_state_json"},
    "activity_instances": {"activity_id", "agent_id", "revision", "definition_json", "instance_json", "status"},
    "feedback_heads": {"agent_id", "scope_id", "event_id", "occurred_at"},
    "activity_commits": {"activity_id", "from_revision", "request_json", "result_json"},
    "scheduled_events": {"schedule_id", "agent_id", "schedule_json", "status", "attempts", "next_attempt_at", "last_error"},
}


class _SQLiteSession:
    """只在创建它的事务中有效；列名与表名严格使用白名单。"""

    def __init__(self, connection: aiosqlite.Connection):
        self.connection = connection
        self.active = True
        self._savepoint_sequence = 0

    async def _control(self, sql):
        async with self.connection.execute(sql):
            pass

    @asynccontextmanager
    async def savepoint(self):
        if not self.active:
            raise StorageNotOpenError("事务会话已经结束")
        self._savepoint_sequence += 1
        name=f"persona_sp_{self._savepoint_sequence}"
        await self._control(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            await self._control(f"ROLLBACK TO SAVEPOINT {name}")
            await self._control(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            await self._control(f"RELEASE SAVEPOINT {name}")

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
        before: Row | None = None, at_least: Row | None = None, descending: bool = False,
        where_in: dict[str, tuple[SQLValue, ...]] | None = None,
    ) -> tuple[Row, ...]:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit 必须是正整数")
        if type(descending) is not bool:
            raise ValueError("descending 必须为布尔值")
        clauses, parameters = [], []
        self._check(table, order_by)
        for filters, operator in ((where, "="), (at_most, "<="), (after, ">"), (before, "<"), (at_least, ">=")):
            self._check(table, filters or {})
            for key, value in (filters or {}).items():
                if value is None:
                    if operator != "=":
                        raise ValueError("范围条件不能与 NULL 比较")
                    clauses.append(f'"{key}" IS NULL')
                    continue
                clauses.append(f'"{key}" {operator} ?')
                parameters.append(value)
        self._check(table, where_in or {})
        empty=False
        total=sum(len(values) for values in (where_in or {}).values() if isinstance(values,tuple))
        if total>512:
            raise ValueError("批量条件最多允许 512 个值")
        for key,values in (where_in or {}).items():
            if not isinstance(values,tuple) or any(value is None for value in values):
                raise ValueError("批量条件必须为不含 NULL 的元组")
            if not values:
                empty=True
                continue
            clauses.append(f'"{key}" IN ('+",".join("?" for _ in values)+")")
            parameters.extend(values)
        if empty:
            return ()
        sql = f'SELECT * FROM "{table}"'
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if order_by:
            sql += " ORDER BY " + ", ".join(f'"{key}"'+(" DESC" if descending else " ASC") for key in order_by)
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


    async def delete(self, table: str, *, where: Row) -> int:
        self._check(table, where)
        if not where:
            raise ValueError("删除必须提供匹配条件")
        conditions = " AND ".join(f'"{key}" IS NULL' if value is None else f'"{key}" = ?' for key,value in where.items())
        async with self.connection.execute(f'DELETE FROM "{table}" WHERE {conditions}',
                                          tuple(value for value in where.values() if value is not None)) as cursor:
            return cursor.rowcount


class SQLiteStorage:
    """仅负责 SQLite 连接、v1/v2/v3/v4/v5→v6 迁移及行级事务，不解释业务 JSON。"""

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
                if version not in (0, 1, 2, 3, 4, 5, 6, 7):
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
                    version = 3
                if version == 3:
                    for sql in _MIGRATE_V3_TO_V4:
                        async with connection.execute(sql):
                            pass
                    version = 4
                if version == 4:
                    for sql in _MIGRATE_V4_TO_V5:
                        async with connection.execute(sql):
                            pass
                    version = 5
                if version == 5:
                    for sql in _MIGRATE_V5_TO_V6:
                        async with connection.execute(sql):
                            pass
                    version = 6
                if version == 6:
                    for sql in _MIGRATE_V6_TO_V7:
                        async with connection.execute(sql):
                            pass
                    version = 7
                # 补建辅助索引，不解释业务 JSON。

                async with connection.execute("CREATE INDEX IF NOT EXISTS scheduled_pending_idx "
                                              "ON scheduled_events (agent_id, status, schedule_id)"):
                    pass
                async with connection.execute("PRAGMA user_version = 7"):
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
