"""异步 SQLite 存储：事件、角色状态与原子结算记录。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Any

import aiosqlite

from activity.executor import ActivityContext, ActivityExecutionResult
from activity.manager import ActivityManager
from activity.models import ActivityDefinition, ActivityInstance, ActivityRecord, ActivityStatus
from activity.rewards import ActivityNotCompletedError, InvalidCompletionEventError, build_completion_proposal
from core.events import AgentEvent
from core.settlement import SettlementTimeError, StateChangeProposal, StateVersionConflictError, settle_state
from core.state import AgentState


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


class StorageError(RuntimeError):
    """存储操作因业务约束或生命周期问题被拒绝。"""


class StorageNotOpenError(StorageError):
    """数据库尚未打开或已经关闭。"""


class UnsupportedSchemaError(StorageError):
    """数据库结构版本不受当前实现支持，不能自动降级。"""


class StateAlreadyExistsError(StorageError):
    """角色状态已存在，不能用初始化操作覆盖。"""


class StateNotFoundError(StorageError):
    """目标角色尚未创建。"""


class EventNotFoundError(StorageError):
    """提案依据事件尚未记录。"""


class EventConflictError(StorageError):
    """相同事件 ID 对应的内容不一致。"""


class ProposalConflictError(StorageError):
    """相同提案 ID 对应的内容不一致。"""


class EventAlreadySettledError(StorageError):
    """同一角色的同一事件已通过另一份提案结算。"""


class ActivityAlreadyExistsError(StorageError):
    """活动实例 ID 已存在，不能重新初始化或覆盖。"""


class ActivityNotFoundError(StorageError):
    """目标角色下没有找到活动实例。"""


class ActivityVersionConflictError(StorageError):
    """候选结果依据的实例快照与当前已保存状态不一致。"""


class ActivityDefinitionConflictError(StorageError):
    """活动开始后不能用同 ID 的另一份定义替换原规则。"""


class ActivityCommitConflictError(StorageError):
    """同一活动旧修订已经提交过不同的操作、时间或结果。"""


def _canonical_json(data: dict[str, Any]) -> str:
    """固定键顺序，避免仅因字典顺序不同而误报内容冲突。"""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))


async def _fetchone(connection: aiosqlite.Connection, sql: str, parameters: tuple = ()):
    async with connection.execute(sql, parameters) as cursor:
        return await cursor.fetchone()


async def _execute(connection: aiosqlite.Connection, sql: str, parameters: tuple = ()) -> int:
    async with connection.execute(sql, parameters) as cursor:
        return cursor.rowcount


class SQLiteStorage:
    """一个实例管理一个异步连接，通过事务串行提交本地状态。

    父目录由调用方创建。使用 async with 或显式调用 open/close。
    同一角色同一事件只允许一份汇总需求提案；相同提案重试返回当前
    最新状态，不再执行变化，也不把旧结算结果覆盖到新状态上。
    当前支持结构版本 2，自动以事务将版本 1 升级到 2。不提供外部动作原子性。
    """

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
        """打开数据库、启用外键并初始化表结构；不覆盖已有数据。"""
        async with self._lock:
            if self._connection is not None:
                return
            connection = await aiosqlite.connect(self.database_path, isolation_level=None)
            connection.row_factory = aiosqlite.Row
            try:
                await _execute(connection, "PRAGMA foreign_keys = ON")
                await _execute(connection, "PRAGMA busy_timeout = 5000")
                # 在写事务中读取版本，避免两个连接同时打开时重复执行迁移。
                await _execute(connection, "BEGIN IMMEDIATE")
                row = await _fetchone(connection, "PRAGMA user_version")
                version = row[0]
                if version not in (0, 1, 2):
                    raise UnsupportedSchemaError(f"不支持数据库结构版本 {version}")
                if version == 0:
                    for statement in _SCHEMA_V1:
                        await _execute(connection, statement)
                    version = 1
                if version == 1:
                    for statement in _MIGRATE_V1_TO_V2:
                        await _execute(connection, statement)
                    await _execute(connection, "PRAGMA user_version = 2")
                await connection.commit()
            except BaseException:
                try:
                    await connection.rollback()
                finally:
                    await connection.close()
                raise
            self._connection = connection

    async def close(self) -> None:
        """关闭连接；重复关闭不产生额外操作。"""
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    def _require_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise StorageNotOpenError("请先打开数据库")
        return self._connection

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """先取得写事务；任一步失败或被取消时回滚，成功才提交。"""
        async with self._lock:
            connection = self._require_connection()
            try:
                await _execute(connection, "BEGIN IMMEDIATE")
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def _read_state(self, connection: aiosqlite.Connection, agent_id: str) -> AgentState | None:
        row = await _fetchone(
            connection, "SELECT state_json FROM agent_states WHERE agent_id = ?", (agent_id,)
        )
        return None if row is None else AgentState.model_validate_json(row["state_json"])

    async def create_state(self, state: AgentState) -> None:
        """首次创建角色状态；已存在则报错，防止重启时重置。"""
        state = AgentState.model_validate(state.model_dump())
        async with self._transaction() as connection:
            if await self._read_state(connection, state.agent_id) is not None:
                raise StateAlreadyExistsError("角色状态已经存在，不能重新初始化")
            await _execute(
                connection,
                "INSERT INTO agent_states (agent_id, version, state_json) VALUES (?, ?, ?)",
                (state.agent_id, state.version, state.model_dump_json()),
            )

    async def get_state(self, agent_id: str) -> AgentState | None:
        """读取最新已提交状态；角色不存在时返回 None。"""
        async with self._lock:
            return await self._read_state(self._require_connection(), agent_id)

    async def record_event(self, event: AgentEvent) -> None:
        """记录事件；同 ID 同内容是重试，同 ID 不同内容则拒绝。"""
        event = AgentEvent.model_validate(event.model_dump())
        async with self._transaction() as connection:
            await self._record_event(connection, event)

    async def _record_event(self, connection: aiosqlite.Connection, event: AgentEvent) -> None:
        """复用事件去重规则，不另开事务，便于活动与反馈一起保存。"""
        serialized = _canonical_json(event.model_dump(mode="json"))
        row = await _fetchone(
            connection, "SELECT event_json FROM agent_events WHERE event_id = ?", (event.event_id,)
        )
        if row is not None:
            if row["event_json"] != serialized:
                raise EventConflictError("同一事件 ID 的内容不能被改写")
            return
        await _execute(
            connection, "INSERT INTO agent_events (event_id, event_json) VALUES (?, ?)",
            (event.event_id, serialized),
        )

    async def get_event(self, event_id: str) -> AgentEvent | None:
        """读取原始事件，保留来源、作用域和关联信息。"""
        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                "SELECT event_json FROM agent_events WHERE event_id = ?", (event_id,),
            )
            return None if row is None else AgentEvent.model_validate_json(row["event_json"])

    async def _read_activity(
        self, connection: aiosqlite.Connection, agent_id: str, activity_id: str
    ) -> ActivityRecord | None:
        row = await _fetchone(
            connection,
            "SELECT revision, definition_json, instance_json FROM activity_instances "
            "WHERE agent_id = ? AND activity_id = ?",
            (agent_id, activity_id),
        )
        if row is None:
            return None
        record = ActivityRecord(
            definition=ActivityDefinition.model_validate_json(row["definition_json"]),
            instance=ActivityInstance.model_validate_json(row["instance_json"]),
        )
        if (
            record.instance.agent_id != agent_id or record.instance.activity_id != activity_id
            or record.instance.revision != row["revision"]
        ):
            raise StorageError("活动索引字段与实例 JSON 不一致")
        return record

    async def create_activity(
        self, definition: ActivityDefinition, instance: ActivityInstance
    ) -> ActivityRecord:
        """保存待开始、修订 0 的活动及其固定规则快照；不启动或覆盖实例。"""
        record = ActivityRecord.model_validate({
            "definition": definition.model_dump(), "instance": instance.model_dump(),
        })
        initial = record.instance
        if (
            initial.status != ActivityStatus.PENDING or initial.revision != 0
            or initial.updated_at != initial.created_at
        ):
            raise ValueError("只能创建待开始、修订 0 且创建与更新时间一致的活动")
        async with self._transaction() as connection:
            if await self._read_state(connection, initial.agent_id) is None:
                raise StateNotFoundError("请先创建活动所属角色")
            exists = await _fetchone(
                connection, "SELECT activity_id FROM activity_instances WHERE activity_id = ?",
                (initial.activity_id,),
            )
            if exists is not None:
                raise ActivityAlreadyExistsError("活动实例 ID 已存在，不能覆盖初始化")
            await _execute(
                connection,
                "INSERT INTO activity_instances "
                "(activity_id, agent_id, revision, definition_json, instance_json) VALUES (?, ?, ?, ?, ?)",
                (initial.activity_id, initial.agent_id, initial.revision,
                 record.definition.model_dump_json(), initial.model_dump_json()),
            )
            return record

    async def get_activity(self, agent_id: str, activity_id: str) -> ActivityRecord | None:
        """按角色与实例 ID 读取原规则和最新实例，不存在或角色不符返回 None。"""
        async with self._lock:
            return await self._read_activity(self._require_connection(), agent_id, activity_id)

    async def commit_activity_result(
        self, operation: str, context: ActivityContext, result: ActivityExecutionResult,
        *, settled_at: datetime | None = None,
    ) -> ActivityRecord:
        """原子保存活动、反馈和已配置的完成需求效果。

        同一操作、原上下文和结果重试时返回当前最新活动记录；同一旧修订
        对应不同操作或结果时拒绝。无状态变化时只记录反馈事件，不占用该
        修订的提交记录。首次完成时结算原定义中的需求效果，默认结算时间
        为候选实例更新时间；可显式提供更晚的 settled_at。数据库不能
        撤销执行器已经发生的外部动作。
        """
        context = ActivityContext.model_validate(context.model_dump())
        result = ActivityExecutionResult.model_validate(result.model_dump())
        ActivityManager.validate_result(operation, context, result)
        previous, candidate = context.instance, result.instance
        request_json = _canonical_json({
            "operation": operation, "context": context.model_dump(mode="json"),
        })
        result_json = _canonical_json(result.model_dump(mode="json"))
        async with self._transaction() as connection:
            current = await self._read_activity(connection, previous.agent_id, previous.activity_id)
            if current is None:
                raise ActivityNotFoundError("角色下没有找到活动实例")
            if current.definition != context.definition:
                raise ActivityDefinitionConflictError("活动不能在执行中替换原定义快照")
            committed = await _fetchone(
                connection,
                "SELECT request_json, result_json FROM activity_commits "
                "WHERE activity_id = ? AND from_revision = ?",
                (previous.activity_id, previous.revision),
            )
            if committed is not None:
                if committed["request_json"] != request_json or committed["result_json"] != result_json:
                    raise ActivityCommitConflictError("同一旧修订已经提交过不同的操作或结果")
                if candidate.status == ActivityStatus.COMPLETED:
                    await self._settle_activity_completion(
                        connection, current,
                        candidate.updated_at if settled_at is None else settled_at,
                    )
                return current
            if current.instance != previous:
                raise ActivityVersionConflictError("活动原快照与数据库当前实例不一致")
            if candidate == previous:
                for event in result.events:
                    await self._record_event(connection, event)
                return current

            updated = await _execute(
                connection,
                "UPDATE activity_instances SET revision = ?, instance_json = ? "
                "WHERE activity_id = ? AND agent_id = ? AND revision = ?",
                (candidate.revision, candidate.model_dump_json(), previous.activity_id,
                 previous.agent_id, previous.revision),
            )
            if updated != 1:
                raise ActivityVersionConflictError("数据库中的活动修订已经变化")
            for event in result.events:
                await self._record_event(connection, event)
            await _execute(
                connection,
                "INSERT INTO activity_commits "
                "(activity_id, from_revision, request_json, result_json) VALUES (?, ?, ?, ?)",
                (previous.activity_id, previous.revision, request_json, result_json),
            )
            saved = ActivityRecord(definition=current.definition, instance=candidate)
            if candidate.status == ActivityStatus.COMPLETED:
                await self._settle_activity_completion(
                    connection, saved, candidate.updated_at if settled_at is None else settled_at,
                )
            return saved

    async def settle_activity_completion(
        self, agent_id: str, activity_id: str, *, settled_at: datetime
    ) -> AgentState:
        """从数据库完成记录补结算旧活动；安全重试返回角色当前状态。"""
        async with self._transaction() as connection:
            record = await self._read_activity(connection, agent_id, activity_id)
            if record is None:
                raise ActivityNotFoundError("角色下没有找到活动实例")
            return await self._settle_activity_completion(connection, record, settled_at)

    async def _settle_activity_completion(
        self, connection: aiosqlite.Connection, record: ActivityRecord, settled_at: datetime
    ) -> AgentState:
        instance = record.instance
        if instance.status != ActivityStatus.COMPLETED:
            raise ActivityNotCompletedError("活动尚未完成，不能结算完成效果")
        committed = await _fetchone(
            connection, "SELECT result_json FROM activity_commits WHERE activity_id = ? AND from_revision = ?",
            (instance.activity_id, instance.revision - 1),
        )
        if committed is None:
            raise InvalidCompletionEventError("已完成活动缺少对应的原子提交记录")
        result = ActivityExecutionResult.model_validate_json(committed["result_json"])
        if result.instance != instance:
            raise InvalidCompletionEventError("完成提交记录与当前活动实例不一致")
        events = [event for event in result.events if event.event_type == "ACTIVITY.completed"]
        if len(events) != 1:
            raise InvalidCompletionEventError("完成提交必须包含唯一的 ACTIVITY.completed 事件")
        event = events[0]
        logged = await _fetchone(
            connection, "SELECT event_json FROM agent_events WHERE event_id = ?", (event.event_id,),
        )
        if logged is None or AgentEvent.model_validate_json(logged["event_json"]) != event:
            raise InvalidCompletionEventError("完成事件与事件日志不一致")
        state = await self._read_state(connection, instance.agent_id)
        if state is None:
            raise StateNotFoundError("活动所属角色不存在")
        proposal = build_completion_proposal(record, event, state)
        if proposal is None:
            return state
        existing = await _fetchone(
            connection, "SELECT proposal_json FROM state_settlements WHERE agent_id = ? AND event_id = ?",
            (instance.agent_id, event.event_id),
        )
        if existing is not None:
            original = StateChangeProposal.model_validate_json(existing["proposal_json"])
            if original.model_dump(exclude={"expected_version"}) != proposal.model_dump(exclude={"expected_version"}):
                raise ProposalConflictError("完成事件已被不同规则结算，拒绝再次发放")
            return await self._commit_proposal(connection, original, settled_at)
        if (
            not isinstance(settled_at, datetime) or settled_at.utcoffset() is None
            or settled_at < instance.updated_at
        ):
            raise SettlementTimeError("首次完成结算时间必须带时区且不早于活动完成记录")
        return await self._commit_proposal(connection, proposal, settled_at)

    async def get_committed_proposal(
        self, agent_id: str, event_id: str
    ) -> StateChangeProposal | None:
        """读取该角色该事件已成功提交的原提案，供运行时安全重试。"""
        async with self._lock:
            row = await _fetchone(
                self._require_connection(),
                "SELECT proposal_json FROM state_settlements WHERE agent_id = ? AND event_id = ?",
                (agent_id, event_id),
            )
            return None if row is None else StateChangeProposal.model_validate_json(row["proposal_json"])

    async def commit_proposal(
        self, proposal: StateChangeProposal, *, settled_at: datetime
    ) -> AgentState:
        """原子执行去重、读取当前状态、结算、更新状态与记录提案。

        重试成功过的同一提案时，返回角色最新状态；不会再次校验旧预期
        版本或重新使用本次 settled_at，因为没有执行新结算。同 ID 内容
        不同会报错。同事件换 ID 的重复提案也报错，不能绕过去重。
        """
        proposal = StateChangeProposal.model_validate(proposal.model_dump())
        async with self._transaction() as connection:
            return await self._commit_proposal(connection, proposal, settled_at)

    async def _commit_proposal(
        self, connection: aiosqlite.Connection, proposal: StateChangeProposal, settled_at: datetime
    ) -> AgentState:
        """复用当前事务结算角色需求，避免活动提交中嵌套事务或重复取锁。"""
        serialized = _canonical_json(proposal.model_dump(mode="json"))
        existing = await _fetchone(
            connection, "SELECT proposal_json FROM state_settlements WHERE proposal_id = ?",
            (proposal.proposal_id,),
        )
        if existing is not None and existing["proposal_json"] != serialized:
            raise ProposalConflictError("同一提案 ID 的内容不能被改写")
        state = await self._read_state(connection, proposal.agent_id)
        if state is None:
            raise StateNotFoundError("目标角色状态不存在")
        if existing is not None:
            return state
        processed = await _fetchone(
            connection,
            "SELECT proposal_id FROM state_settlements WHERE agent_id = ? AND event_id = ?",
            (proposal.agent_id, proposal.event_id),
        )
        if processed is not None:
            raise EventAlreadySettledError("同一角色的同一事件已经结算")
        event = await _fetchone(
            connection, "SELECT event_id FROM agent_events WHERE event_id = ?", (proposal.event_id,),
        )
        if event is None:
            raise EventNotFoundError("请先记录提案依据的事件")

        result = settle_state(state, proposal, settled_at=settled_at)
        updated = await _execute(
            connection,
            "UPDATE agent_states SET version = ?, state_json = ? WHERE agent_id = ? AND version = ?",
            (result.version, result.model_dump_json(), state.agent_id, state.version),
        )
        if updated != 1:
            raise StateVersionConflictError("数据库中的角色版本已经变化")
        await _execute(
            connection,
            "INSERT INTO state_settlements "
            "(proposal_id, agent_id, event_id, proposal_json, result_state_json) VALUES (?, ?, ?, ?, ?)",
            (proposal.proposal_id, proposal.agent_id, proposal.event_id,
             serialized, result.model_dump_json()),
        )
        return result
