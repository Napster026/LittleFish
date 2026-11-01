"""活动业务的事务内提交规则，不依赖 SQLite 或其他具体存储实现。"""

from __future__ import annotations

from datetime import datetime

from .executor import ActivityContext, ActivityExecutionResult
from .manager import ActivityManager
from .models import ActivityDefinition, ActivityInstance, ActivityRecord, ActivityStatus, ExecutionMode
from .rewards import ActivityNotCompletedError, InvalidCompletionEventError, build_completion_proposal
from .ticks import build_activity_tick, build_tick_exhaustion_result, parse_activity_tick
from ..core.events import AgentEvent
from ..core.persistence import CorePersistence
from ..core.scheduler import ScheduledEvent, ScheduleRecord, ScheduleStatus
from ..core.settlement import SettlementTimeError
from ..core.state import AgentState
from ..persistence.errors import (
    ActivityAlreadyExistsError, ActivityCommitConflictError, ActivityDefinitionConflictError,
    ActivityNotFoundError, ActivityVersionConflictError, ProposalConflictError, StateNotFoundError, StorageError,
)
from ..persistence.ports import PersistenceSession
from ..persistence.serialization import canonical_json, text_column


class ActivityPersistence:
    """活动规则决定写什么，传入会话负责写入；与核心规则共享同一事务。"""

    def __init__(self, session: PersistenceSession, core: CorePersistence):
        self.session = session
        self.core = core

    async def get_activity(self, agent_id: str, activity_id: str) -> ActivityRecord | None:
        row = await self.session.fetch_one("activity_instances", where={"agent_id": agent_id, "activity_id": activity_id})
        if row is None:
            return None
        record = ActivityRecord(
            definition=ActivityDefinition.model_validate_json(text_column(row, "definition_json")),
            instance=ActivityInstance.model_validate_json(text_column(row, "instance_json")),
        )
        if (
            record.instance.agent_id != agent_id or record.instance.activity_id != activity_id
            or record.instance.revision != row["revision"]
            or row.get("status", "unknown") not in ("unknown", record.instance.status.value)
        ):
            raise StorageError("活动索引字段与实例 JSON 不一致")
        return record

    async def create_activity(self, definition: ActivityDefinition, instance: ActivityInstance) -> ActivityRecord:
        record = ActivityRecord.model_validate({"definition": definition.model_dump(), "instance": instance.model_dump()})
        initial = record.instance
        if initial.status != ActivityStatus.PENDING or initial.revision != 0 or initial.updated_at != initial.created_at:
            raise ValueError("只能创建待开始、修订 0 且创建与更新时间一致的活动")
        if await self.core.get_state(initial.agent_id) is None:
            raise StateNotFoundError("请先创建活动所属角色")
        if await self.session.fetch_one("activity_instances", where={"activity_id": initial.activity_id}) is not None:
            raise ActivityAlreadyExistsError("活动实例 ID 已存在，不能覆盖初始化")
        await self.session.insert("activity_instances", {
            "activity_id": initial.activity_id, "agent_id": initial.agent_id, "revision": initial.revision,
            "status": initial.status.value,
            "definition_json": record.definition.model_dump_json(), "instance_json": initial.model_dump_json(),
        })
        return record

    async def get_committed_events(self, record: ActivityRecord) -> tuple[AgentEvent, ...]:
        if record.instance.revision == 0:
            return ()
        row = await self.session.fetch_one("activity_commits", where={
            "activity_id": record.instance.activity_id, "from_revision": record.instance.revision - 1,
        })
        if row is None:
            return ()
        result = ActivityExecutionResult.model_validate_json(text_column(row, "result_json"))
        if result.instance != record.instance:
            raise StorageError("活动提交与当前快照不一致")
        return result.events

    async def reconcile_status_index(self):
        """旧 JSON 的状态仅回填索引列；不改实例、修订或状态结算。"""
        while True:
            rows = await self.session.fetch_all("activity_instances", where={"status": "unknown"}, order_by=("activity_id",))
            if not rows:
                return
            for row in rows:
                record = await self.get_activity(row["agent_id"], row["activity_id"])
                changed = await self.session.update("activity_instances", {"status": record.instance.status.value},
                    where={"activity_id": row["activity_id"], "status": "unknown"})
                if changed != 1:
                    raise StorageError("活动状态索引回填冲突")

    async def list_active_activities(self, agent_id: str) -> tuple[ActivityRecord, ...]:
        """按状态索引查询活跃实例，不再解析历史终态活动。"""
        records = []
        for status in (ActivityStatus.PENDING, ActivityStatus.RUNNING, ActivityStatus.PAUSED):
            after_id = None
            while True:
                rows = await self.session.fetch_all("activity_instances", where={"agent_id": agent_id, "status": status.value},
                    after=None if after_id is None else {"activity_id": after_id}, order_by=("activity_id",))
                if not rows: break
                for row in rows:
                    record = await self.get_activity(agent_id, row["activity_id"])
                    if record is not None:
                        records.append(record)
                after_id = rows[-1]["activity_id"]
        return tuple(sorted(records, key=lambda record: record.instance.activity_id))

    async def commit_activity_result(
        self, operation: str, context: ActivityContext, result: ActivityExecutionResult, *,
        settled_at: datetime | None = None, next_schedule: ScheduledEvent | None = None,
        trigger_event: AgentEvent | None = None,
    ) -> ActivityRecord:
        context = ActivityContext.model_validate(context.model_dump())
        result = ActivityExecutionResult.model_validate(result.model_dump())
        ActivityManager.validate_result(operation, context, result)
        previous, candidate = context.instance, result.instance
        request = {"operation": operation, "context": context.model_dump(mode="json")}
        expected_trigger: ScheduledEvent | None = None
        if next_schedule is not None:
            next_schedule = ScheduledEvent.model_validate(next_schedule.model_dump())
            tick = parse_activity_tick(next_schedule.event)
            expected_schedule = build_activity_tick(
                ActivityRecord(definition=context.definition, instance=candidate), interval_seconds=tick.interval_seconds,
            )
            if next_schedule != expected_schedule:
                raise ValueError("下一次计划必须匹配候选活动及其修订")
            request["next_schedule"] = next_schedule.model_dump(mode="json")
        if trigger_event is not None:
            trigger_event = AgentEvent.model_validate(trigger_event.model_dump())
            tick = parse_activity_tick(trigger_event)
            if (
                operation != "step" or tick.agent_id != previous.agent_id
                or trigger_event.activity_id != previous.activity_id
                or tick.expected_revision != previous.revision or trigger_event.timestamp > context.now
            ):
                raise ValueError("触发事件必须匹配本次推进的角色、实例、旧修订和时间")
            if candidate.status == ActivityStatus.RUNNING and next_schedule is None:
                raise ValueError("时钟推进后仍运行的活动必须安排下一次计划")
            expected_trigger = build_activity_tick(
                ActivityRecord(definition=context.definition, instance=previous), interval_seconds=tick.interval_seconds,
            )
            if trigger_event != expected_trigger.event:
                raise ValueError("触发事件与原活动快照对应的计划不一致")
            if next_schedule is not None and parse_activity_tick(next_schedule.event).interval_seconds != tick.interval_seconds:
                raise ValueError("同一时钟链必须沿用持久化计划的推进间隔")
            request["trigger_event"] = trigger_event.model_dump(mode="json")
        request_json = canonical_json(request)
        result_json = canonical_json(result.model_dump(mode="json"))
        current = await self.get_activity(previous.agent_id, previous.activity_id)
        if current is None:
            raise ActivityNotFoundError("角色下没有找到活动实例")
        if current.definition != context.definition:
            raise ActivityDefinitionConflictError("活动不能在执行中替换原定义快照")
        if trigger_event is not None:
            trigger = await self.core.get_schedule(previous.agent_id, trigger_event.event_id)
            if trigger is None or trigger.schedule != expected_trigger or trigger.status == ScheduleStatus.FAILED:
                raise ValueError("触发事件缺少匹配的有效持久化计划")
            await self.core.record_event(trigger_event)
        committed = await self.session.fetch_one("activity_commits", where={
            "activity_id": previous.activity_id, "from_revision": previous.revision,
        })
        if committed is not None:
            if committed["request_json"] != request_json or committed["result_json"] != result_json:
                raise ActivityCommitConflictError("同一旧修订已经提交过不同的操作或结果")
            if candidate.status == ActivityStatus.COMPLETED:
                await self._settle_completed(current, candidate.updated_at if settled_at is None else settled_at)
            if next_schedule is not None:
                await self.core.create_schedule(next_schedule)
            return current
        if current.instance != previous:
            raise ActivityVersionConflictError("活动原快照与数据库当前实例不一致")
        if candidate == previous:
            for event in result.events:
                await self.core.record_event(event)
            if next_schedule is not None:
                await self.core.create_schedule(next_schedule)
            return current
        changed = await self.session.update("activity_instances", {
            "revision": candidate.revision, "instance_json": candidate.model_dump_json(), "status": candidate.status.value,
        }, where={"activity_id": previous.activity_id, "agent_id": previous.agent_id, "revision": previous.revision})
        if changed != 1:
            raise ActivityVersionConflictError("数据库中的活动修订已经变化")
        for event in result.events:
            await self.core.record_event(event)
        await self.session.insert("activity_commits", {
            "activity_id": previous.activity_id, "from_revision": previous.revision,
            "request_json": request_json, "result_json": result_json,
        })
        saved = ActivityRecord(definition=current.definition, instance=candidate)
        if candidate.status == ActivityStatus.COMPLETED:
            await self._settle_completed(saved, candidate.updated_at if settled_at is None else settled_at)
        if next_schedule is not None:
            await self.core.create_schedule(next_schedule)
        return saved

    async def settle_activity_completion(self, agent_id: str, activity_id: str, *, settled_at: datetime) -> AgentState:
        record = await self.get_activity(agent_id, activity_id)
        if record is None:
            raise ActivityNotFoundError("角色下没有找到活动实例")
        return await self._settle_completed(record, settled_at)

    async def _settle_completed(self, record: ActivityRecord, settled_at: datetime) -> AgentState:
        instance = record.instance
        if instance.status != ActivityStatus.COMPLETED:
            raise ActivityNotCompletedError("活动尚未完成，不能结算完成效果")
        committed = await self.session.fetch_one("activity_commits", where={
            "activity_id": instance.activity_id, "from_revision": instance.revision - 1,
        })
        if committed is None:
            raise InvalidCompletionEventError("已完成活动缺少对应的原子提交记录")
        result = ActivityExecutionResult.model_validate_json(text_column(committed, "result_json"))
        if result.instance != instance:
            raise InvalidCompletionEventError("完成提交记录与当前活动实例不一致")
        events = [event for event in result.events if event.event_type == "ACTIVITY.completed"]
        if len(events) != 1:
            raise InvalidCompletionEventError("完成提交必须包含唯一的 ACTIVITY.completed 事件")
        event = events[0]
        if await self.core.get_event(event.event_id) != event:
            raise InvalidCompletionEventError("完成事件与事件日志不一致")
        state = await self.core.get_state(instance.agent_id)
        if state is None:
            raise StateNotFoundError("活动所属角色不存在")
        proposal = build_completion_proposal(record, event, state)
        if proposal is None:
            return state
        original = await self.core.get_committed_proposal(instance.agent_id, event.event_id)
        if original is not None:
            if original.model_dump(exclude={"expected_version"}) != proposal.model_dump(exclude={"expected_version"}):
                raise ProposalConflictError("完成事件已被不同规则结算，拒绝再次发放")
            return await self.core.commit_proposal(original, settled_at=settled_at)
        if not isinstance(settled_at, datetime) or settled_at.utcoffset() is None or settled_at < instance.updated_at:
            raise SettlementTimeError("首次完成结算时间必须带时区且不早于活动完成记录")
        return await self.core.commit_proposal(proposal, settled_at=settled_at)

    async def fail_exhausted_tick(self, plan: ScheduleRecord) -> None:
        try:
            tick = parse_activity_tick(plan.schedule.event)
        except ValueError:
            return
        activity_id = plan.schedule.event.activity_id
        if activity_id is None or tick.agent_id != plan.schedule.agent_id:
            return
        record = await self.get_activity(plan.schedule.agent_id, activity_id)
        if (
            record is None or record.instance.mode != ExecutionMode.SIMULATED
            or record.instance.status != ActivityStatus.RUNNING or record.instance.revision != tick.expected_revision
        ):
            return
        try:
            context, result = build_tick_exhaustion_result(record, plan)
        except ValueError:
            return
        await self.core.record_event(plan.schedule.event)
        await self.commit_activity_result("step", context, result)

    async def reconcile_exhausted_ticks(self) -> None:
        after_id: str | None = None
        while True:
            rows = await self.session.fetch_all(
                "scheduled_events", where={"status": "failed"},
                after=None if after_id is None else {"schedule_id": after_id}, order_by=("schedule_id",),
            )
            if not rows:
                break
            for row in rows:
                await self.fail_exhausted_tick(self.core.schedule_record(row))
            after_id = text_column(rows[-1], "schedule_id")
