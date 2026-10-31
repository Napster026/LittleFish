"""持久化定时事件的异步投递器；存储通过协议注入，避免依赖具体数据库。"""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Protocol

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator,
)

from core.events import AgentEvent
from core.state import AgentState


def normalize_schedule_time(value: datetime) -> datetime:
    """调度接口使用带时区的 datetime，统一以 UTC 比较与保存。"""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("调度时间必须是带时区的 datetime")
    return value.astimezone(timezone.utc)


# noinspection PyNestedDecorators
class ScheduledEvent(BaseModel):
    """一次逻辑投递，重试沿用原事件 ID、内容与时间，不表示周期任务。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schedule_id: StrictStr
    agent_id: StrictStr
    event: AgentEvent
    due_at: AwareDatetime
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=100)] = 3
    retry_delay_seconds: Annotated[float, Field(gt=0, le=86400, allow_inf_nan=False)] = 5.0

    @field_validator("schedule_id", "agent_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("调度及角色 ID 不能为空，也不能包含首尾空白")
        return value

    @field_validator("due_at", mode="before")
    @classmethod
    def parse_due_at(cls, value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return normalize_schedule_time(value)

    @field_validator("retry_delay_seconds", mode="before")
    @classmethod
    def validate_delay(cls, value: Any) -> Any:
        if type(value) not in (int, float):
            raise ValueError("重试间隔必须是数值，不能是布尔值或字符串")
        return value


class ScheduleStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class ScheduleRecord(BaseModel):
    """原计划与当前投递结果；attempts 表示已记录的尝试次数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schedule: ScheduledEvent
    status: ScheduleStatus
    attempts: Annotated[int, Field(strict=True, ge=0)]
    next_attempt_at: AwareDatetime
    last_error: StrictStr | None = None

    @model_validator(mode="after")
    def validate_attempts(self) -> ScheduleRecord:
        if self.attempts > self.schedule.max_attempts:
            raise ValueError("尝试次数超出配置上限")
        if self.status == ScheduleStatus.PENDING and self.attempts >= self.schedule.max_attempts:
            raise ValueError("耗尽尝试次数后不能继续等待重试")
        if self.status == ScheduleStatus.DELIVERED and self.attempts == 0:
            raise ValueError("投递成功必须记录一次尝试")
        if self.status == ScheduleStatus.FAILED and self.attempts != self.schedule.max_attempts:
            raise ValueError("失败终态必须已经耗尽尝试次数")
        return self


class ScheduleStore(Protocol):
    async def get_state(self, agent_id: str) -> AgentState | None: ...
    async def create_schedule(self, schedule: ScheduledEvent) -> ScheduleRecord: ...
    async def list_due_schedules(
        self, agent_id: str, *, now: datetime, limit: int = 100
    ) -> tuple[ScheduleRecord, ...]: ...
    async def record_schedule_delivery(
        self, agent_id: str, schedule_id: str, *, expected_attempts: int,
        attempted_at: datetime, succeeded: bool, error: str | None = None,
    ) -> ScheduleRecord: ...


@dataclass(frozen=True)
class ScheduleDispatchReport:
    delivered: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


class SchedulerError(RuntimeError):
    """调度器生命周期或投递配置错误。"""


class TimeScheduler:
    """每个角色部署一个调度器；可手动 run_due，也可后台轮询。

    已到期的持久化 pending 任务会在重新启动后继续尝试。投递与数据库
    确认不是一个事务，确认前异常退出可能重新投递；接收方必须按原事件
    ID 去重。这里不提供周期规则、取消、人工重置或多实例抢占租约。
    """

    def __init__(
        self, agent_id: str, storage: ScheduleStore,
        deliver: Callable[[AgentEvent], Awaitable[object]], *,
        poll_interval: float = 1.0, clock: Callable[[], datetime] | None = None,
    ):
        if not isinstance(agent_id, str):
            raise ValueError("agent_id 必须是字符串")
        AgentState.validate_agent_id(agent_id)
        if not callable(deliver) or (clock is not None and not callable(clock)):
            raise ValueError("投递回调与时钟必须可调用")
        if type(poll_interval) not in (int, float) or not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("轮询间隔必须是有限正数")
        self.agent_id = agent_id
        self._storage = storage
        self._deliver = deliver
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._poll_interval = poll_interval
        self._wake = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._dispatch_lock = asyncio.Lock()
        self._dispatch_task: asyncio.Task | None = None
        self._worker: asyncio.Task[None] | None = None
        self._stopping = False
        self._last_error: str | None = None

    @property
    def is_running(self) -> bool:
        return self._worker is not None and not self._worker.done() and not self._stopping

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def __aenter__(self) -> TimeScheduler:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.stop()

    async def schedule(self, event: ScheduledEvent) -> ScheduleRecord:
        event = ScheduledEvent.model_validate(event.model_dump())
        if event.agent_id != self.agent_id:
            raise SchedulerError("不能为其他角色安排事件")
        record = await self._storage.create_schedule(event)
        self._wake.set()
        return record

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._worker is not None and not self._worker.done():
                if self._stopping:
                    raise SchedulerError("请等待调度器停止完成")
                return
            if await self._storage.get_state(self.agent_id) is None:
                raise SchedulerError("请先创建角色状态")
            self._stopping = False
            self._worker = asyncio.create_task(self._run(), name=f"scheduler:{self.agent_id}")

    async def stop(self) -> None:
        if asyncio.current_task() in (self._worker, self._dispatch_task):
            raise SchedulerError("投递回调不能等待自身调度器停止")
        async with self._lifecycle_lock:
            worker = self._worker
            if worker is None:
                return
            self._stopping = True
            self._wake.set()
        # 不取消正在发生的投递；取消停止等待也不撤回后台停止过程。
        try:
            await asyncio.shield(worker)
        finally:
            if worker.done():
                async with self._lifecycle_lock:
                    if self._worker is worker:
                        self._worker = None

    async def run_due(self) -> ScheduleDispatchReport:
        """处理一批已到期事件，普通投递失败记入数据库后继续其他任务。"""
        if asyncio.current_task() is self._dispatch_task:
            raise SchedulerError("投递回调不能递归等待 run_due")
        async with self._dispatch_lock:
            self._dispatch_task = asyncio.current_task()
            delivered, failed = [], []
            try:
                due = await self._storage.list_due_schedules(
                    self.agent_id, now=normalize_schedule_time(self._clock()),
                )
                for record in due:
                    if self._stopping and asyncio.current_task() is self._worker:
                        break
                    event = AgentEvent.model_validate(record.schedule.event.model_dump())
                    try:
                        pending = self._deliver(event)
                        if not inspect.isawaitable(pending):
                            raise SchedulerError("投递回调必须返回可等待对象")
                        await pending
                    except Exception as error:
                        description = f"{type(error).__name__}: {error}"[:2048]
                        await self._storage.record_schedule_delivery(
                            self.agent_id, record.schedule.schedule_id,
                            expected_attempts=record.attempts,
                            attempted_at=normalize_schedule_time(self._clock()),
                            succeeded=False, error=description,
                        )
                        self._last_error = description
                        failed.append(record.schedule.schedule_id)
                    else:
                        # 确认失败时保留 pending，后续沿用原事件 ID 重投。
                        await self._storage.record_schedule_delivery(
                            self.agent_id, record.schedule.schedule_id,
                            expected_attempts=record.attempts,
                            attempted_at=normalize_schedule_time(self._clock()), succeeded=True,
                        )
                        delivered.append(record.schedule.schedule_id)
                if not failed:
                    self._last_error = None
                return ScheduleDispatchReport(tuple(delivered), tuple(failed))
            finally:
                self._dispatch_task = None

    async def _run(self) -> None:
        try:
            while not self._stopping:
                self._wake.clear()
                try:
                    await self.run_due()
                except Exception as error:
                    self._last_error = f"{type(error).__name__}: {error}"[:2048]
                if self._stopping:
                    break
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._stopping = True
