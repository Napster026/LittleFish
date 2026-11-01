"""持久化模拟活动服务：显式控制与定时推进，共用同一提交边界。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timezone

from .executor import ActivityContext
from .manager import ActivityManager
from .models import ActivityRecord, ActivityStatus, ExecutionMode
from .ports import ActivityStore
from .ticks import ActivityTick, build_activity_tick, parse_activity_tick
from ..core.events import AgentEvent
from ..core.scheduler import ScheduleStatus, normalize_schedule_time
from ..persistence.errors import ActivityNotFoundError


class ActivityServiceError(RuntimeError):
    """活动服务的角色、模式或定时事件不合法。"""


class ActivityService:
    """每个角色使用一个活动服务，只自动驱动 simulated 模式。

    本层允许数据库副作用，不作为 EventRouter 的纯提案处理函数注册。
    TimeScheduler 可直接使用 handle_event 作为回调；背景生命周期由
    调用方管理，服务自身没有后台循环。活动结果与下一次计划通过业务
    提交协议在一个事务中提交，不依赖具体 SQLite 实现。
    """

    def __init__(
        self, agent_id: str, storage: ActivityStore, manager: ActivityManager, *,
        tick_interval_seconds: float = 5.0, clock: Callable[[], datetime] | None = None,
    ):
        # 复用 tick 模型对角色及间隔的检查，不维护第二套规则。
        settings = ActivityTick(
            agent_id=agent_id, expected_revision=1, interval_seconds=tick_interval_seconds,
        )
        if clock is not None and not callable(clock):
            raise ValueError("clock 必须可调用")
        self.agent_id = settings.agent_id
        self._interval = settings.interval_seconds
        self._storage = storage
        self._manager = manager
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._lock = asyncio.Lock()

    async def _read(self, activity_id: str) -> ActivityRecord:
        record = await self._storage.get_activity(self.agent_id, activity_id)
        if record is None:
            raise ActivityNotFoundError("角色下没有找到活动实例")
        if record.instance.mode != ExecutionMode.SIMULATED:
            raise ActivityServiceError("当前活动服务只允许 simulated 模式")
        return record

    async def _apply(
        self, operation: str, record: ActivityRecord, now: datetime, *,
        interval_seconds: float, trigger_event: AgentEvent | None = None,
    ) -> ActivityRecord:
        context = ActivityContext(definition=record.definition, instance=record.instance, now=now)
        result = await getattr(self._manager, operation)(context)
        if trigger_event is not None and result.instance == record.instance:
            # 调度器按上限重试；应用层最终确认事务一并终结当前修订活动。
            raise ActivityServiceError("到期推进没有产生新活动修订，不能确认该 tick")
        next_schedule = None
        if result.instance.status == ActivityStatus.RUNNING:
            next_schedule = build_activity_tick(
                ActivityRecord(definition=record.definition, instance=result.instance),
                interval_seconds=interval_seconds,
            )
        return await self._storage.commit_activity_result(
            operation, context, result, settled_at=now,
            next_schedule=next_schedule, trigger_event=trigger_event,
        )

    async def _control(self, operation: str, activity_id: str) -> ActivityRecord:
        async with self._lock:
            record = await self._read(activity_id)
            return await self._apply(
                operation, record, normalize_schedule_time(self._clock()), interval_seconds=self._interval,
            )

    async def start_activity(self, activity_id: str) -> ActivityRecord:
        """启动预先创建的待开始活动，并原子保存第一次推进计划。"""
        return await self._control("start", activity_id)

    async def pause_activity(self, activity_id: str) -> ActivityRecord:
        """先计算截至当前的运行进度，再暂停；旧 tick 之后作为过期事件处理。"""
        return await self._control("pause", activity_id)

    async def resume_activity(self, activity_id: str) -> ActivityRecord:
        """恢复活动，按新修订安排下一次推进，暂停时间不计入进度。"""
        return await self._control("resume", activity_id)

    async def stop_activity(self, activity_id: str) -> ActivityRecord:
        """终止活动；若已达到完成条件，沿用执行器的完成语义。"""
        return await self._control("stop", activity_id)

    async def handle_event(self, event: AgentEvent) -> ActivityRecord:
        """接收已经保存的 tick，旧修订安全忽略，未来修订明确拒绝。"""
        event = AgentEvent.model_validate(event.model_dump())
        tick = parse_activity_tick(event)
        activity_id = event.activity_id
        if activity_id is None:
            raise ActivityServiceError("时钟事件缺少活动 ID")
        if tick.agent_id != self.agent_id:
            raise ActivityServiceError("时钟事件不属于当前角色")
        async with self._lock:
            plan = await self._storage.get_schedule(self.agent_id, event.event_id)
            if plan is None or plan.schedule.event != event or plan.status == ScheduleStatus.FAILED:
                raise ActivityServiceError("时钟事件没有匹配的有效持久化计划")
            record = await self._read(activity_id)
            now = normalize_schedule_time(self._clock())
            if now < event.timestamp:
                raise ActivityServiceError("尚未到达活动推进时间")
            if tick.expected_revision > record.instance.revision:
                raise ActivityServiceError("时钟事件依据的活动修订尚未出现")
            if tick.expected_revision < record.instance.revision:
                await self._storage.record_event(event)
                return record
            if record.instance.status != ActivityStatus.RUNNING:
                raise ActivityServiceError("当前修订不是运行中的活动")
            if plan.schedule != build_activity_tick(record, interval_seconds=tick.interval_seconds):
                raise ActivityServiceError("时钟计划与当前活动快照不匹配")
            return await self._apply(
                "step", record, now, interval_seconds=tick.interval_seconds, trigger_event=event,
            )
