"""事务内的核心持久化规则：状态、事件、结算与通用定时投递。"""

from __future__ import annotations

from datetime import datetime, timedelta

from .events import AgentEvent
from .scheduler import ScheduledEvent, ScheduleRecord, ScheduleStatus, normalize_schedule_time
from .settlement import StateChangeProposal, StateVersionConflictError, settle_state
from .state import AgentState
from ..persistence.errors import (
    EventAlreadySettledError, EventConflictError, EventNotFoundError, ProposalConflictError,
    ScheduleConflictError, ScheduleNotFoundError, ScheduleVersionConflictError,
    StateAlreadyExistsError, StateNotFoundError, StorageError,
)
from ..persistence.ports import PersistenceSession, Row
from ..persistence.serialization import canonical_json, integer_column, optional_text_column, text_column


class CorePersistence:
    """使用外部提供的事务会话；不连接数据库、不自行提交或回滚。"""

    def __init__(self, session: PersistenceSession):
        self.session = session

    async def get_state(self, agent_id: str) -> AgentState | None:
        row = await self.session.fetch_one("agent_states", where={"agent_id": agent_id})
        return None if row is None else AgentState.model_validate_json(text_column(row, "state_json"))

    async def create_state(self, state: AgentState) -> None:
        state = AgentState.model_validate(state.model_dump())
        if await self.get_state(state.agent_id) is not None:
            raise StateAlreadyExistsError("角色状态已经存在，不能重新初始化")
        await self.session.insert("agent_states", {
            "agent_id": state.agent_id, "version": state.version, "state_json": state.model_dump_json(),
        })

    async def get_event(self, event_id: str) -> AgentEvent | None:
        row = await self.session.fetch_one("agent_events", where={"event_id": event_id})
        return None if row is None else AgentEvent.model_validate_json(text_column(row, "event_json"))

    async def record_event(self, event: AgentEvent) -> None:
        event = AgentEvent.model_validate(event.model_dump())
        serialized = canonical_json(event.model_dump(mode="json"))
        row = await self.session.fetch_one("agent_events", where={"event_id": event.event_id})
        if row is not None:
            if row["event_json"] != serialized:
                raise EventConflictError("同一事件 ID 的内容不能被改写")
            return
        await self.session.insert("agent_events", {"event_id": event.event_id, "event_json": serialized})

    async def get_committed_proposal(self, agent_id: str, event_id: str) -> StateChangeProposal | None:
        row = await self.session.fetch_one("state_settlements", where={"agent_id": agent_id, "event_id": event_id})
        return None if row is None else StateChangeProposal.model_validate_json(text_column(row, "proposal_json"))

    async def commit_proposal(self, proposal: StateChangeProposal, *, settled_at: datetime) -> AgentState:
        proposal = StateChangeProposal.model_validate(proposal.model_dump())
        serialized = canonical_json(proposal.model_dump(mode="json"))
        existing = await self.session.fetch_one("state_settlements", where={"proposal_id": proposal.proposal_id})
        if existing is not None:
            # 旧记录缺少可选时间/情绪字段，按模型默认值归一化后比较。
            previous = StateChangeProposal.model_validate_json(text_column(existing, "proposal_json"))
            if canonical_json(previous.model_dump(mode="json")) != serialized:
                raise ProposalConflictError("同一提案 ID 的内容不能被改写")
        state = await self.get_state(proposal.agent_id)
        if state is None:
            raise StateNotFoundError("目标角色状态不存在")
        if existing is not None:
            return state
        if await self.get_committed_proposal(proposal.agent_id, proposal.event_id) is not None:
            raise EventAlreadySettledError("同一角色的同一事件已经结算")
        if await self.get_event(proposal.event_id) is None:
            raise EventNotFoundError("请先记录提案依据的事件")
        result = settle_state(state, proposal, settled_at=settled_at)
        changed = await self.session.update("agent_states", {
            "version": result.version, "state_json": result.model_dump_json(),
        }, where={"agent_id": state.agent_id, "version": state.version})
        if changed != 1:
            raise StateVersionConflictError("数据库中的角色版本已经变化")
        await self.session.insert("state_settlements", {
            "proposal_id": proposal.proposal_id, "agent_id": proposal.agent_id, "event_id": proposal.event_id,
            "proposal_json": serialized, "result_state_json": result.model_dump_json(),
        })
        return result

    @staticmethod
    def schedule_record(row: Row) -> ScheduleRecord:
        record = ScheduleRecord(
            schedule=ScheduledEvent.model_validate_json(text_column(row, "schedule_json")),
            status=ScheduleStatus(text_column(row, "status")), attempts=integer_column(row, "attempts"),
            next_attempt_at=datetime.fromisoformat(text_column(row, "next_attempt_at")),
            last_error=optional_text_column(row, "last_error"),
        )
        if record.schedule.agent_id != row["agent_id"] or record.schedule.schedule_id != row["schedule_id"]:
            raise StorageError("调度索引字段与原计划不一致")
        return record

    async def get_schedule(self, agent_id: str, schedule_id: str) -> ScheduleRecord | None:
        row = await self.session.fetch_one("scheduled_events", where={"agent_id": agent_id, "schedule_id": schedule_id})
        return None if row is None else self.schedule_record(row)

    async def create_schedule(self, schedule: ScheduledEvent) -> ScheduleRecord:
        schedule = ScheduledEvent.model_validate(schedule.model_dump())
        serialized = canonical_json(schedule.model_dump(mode="json"))
        if await self.get_state(schedule.agent_id) is None:
            raise StateNotFoundError("请先创建计划所属角色")
        existing = await self.session.fetch_one("scheduled_events", where={"schedule_id": schedule.schedule_id})
        if existing is not None:
            if existing["schedule_json"] != serialized:
                raise ScheduleConflictError("同一调度 ID 的计划内容不能更改")
            return self.schedule_record(existing)
        await self.session.insert("scheduled_events", {
            "schedule_id": schedule.schedule_id, "agent_id": schedule.agent_id, "schedule_json": serialized,
            "status": "pending", "attempts": 0, "next_attempt_at": schedule.due_at.isoformat(timespec="microseconds"),
            "last_error": None,
        })
        return ScheduleRecord(schedule=schedule, status=ScheduleStatus.PENDING, attempts=0, next_attempt_at=schedule.due_at)

    async def list_due_schedules(self, agent_id: str, *, now: datetime, limit: int = 100) -> tuple[ScheduleRecord, ...]:
        now = normalize_schedule_time(now)
        if type(limit) is not int or limit < 1:
            raise ValueError("limit 必须是正整数")
        rows = await self.session.fetch_all(
            "scheduled_events", where={"agent_id": agent_id, "status": "pending"},
            at_most={"next_attempt_at": now.isoformat(timespec="microseconds")},
            order_by=("next_attempt_at", "schedule_id"), limit=limit,
        )
        return tuple(self.schedule_record(row) for row in rows)

    async def record_schedule_delivery(
        self, agent_id: str, schedule_id: str, *, expected_attempts: int,
        attempted_at: datetime, succeeded: bool, error: str | None = None,
    ) -> ScheduleRecord:
        attempted_at = normalize_schedule_time(attempted_at)
        if type(expected_attempts) is not int or expected_attempts < 0 or type(succeeded) is not bool:
            raise ValueError("投递次数或结果类型不合法")
        if succeeded and error is not None:
            raise ValueError("成功确认不能同时附带失败原因")
        if not succeeded and (not isinstance(error, str) or not error.strip()):
            raise ValueError("失败投递必须提供错误说明")
        current = await self.get_schedule(agent_id, schedule_id)
        if current is None:
            raise ScheduleNotFoundError("角色下没有找到定时计划")
        if current.status == ScheduleStatus.DELIVERED and succeeded:
            return current
        if current.status != ScheduleStatus.PENDING or current.attempts != expected_attempts:
            raise ScheduleVersionConflictError("调度状态或尝试次数已经变化")
        if attempted_at < current.next_attempt_at:
            raise ValueError("投递记录时间不能早于下次允许尝试的时间")
        attempts = current.attempts + 1
        status = ScheduleStatus.DELIVERED if succeeded else (
            ScheduleStatus.FAILED if attempts >= current.schedule.max_attempts else ScheduleStatus.PENDING
        )
        next_attempt_at = attempted_at if status != ScheduleStatus.PENDING else (
            attempted_at + timedelta(seconds=current.schedule.retry_delay_seconds)
        )
        description = None if succeeded else (error or "")[:2048]
        changed = await self.session.update("scheduled_events", {
            "status": status.value, "attempts": attempts,
            "next_attempt_at": next_attempt_at.isoformat(timespec="microseconds"), "last_error": description,
        }, where={"agent_id": agent_id, "schedule_id": schedule_id, "status": "pending", "attempts": expected_attempts})
        if changed != 1:
            raise ScheduleVersionConflictError("调度状态在写入前已经变化")
        return ScheduleRecord(
            schedule=current.schedule, status=status, attempts=attempts, next_attempt_at=next_attempt_at, last_error=description,
        )
