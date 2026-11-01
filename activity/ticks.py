"""模拟活动的单次推进事件；计划构造为纯函数，不访问存储或执行器。"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Annotated, Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from .executor import ActivityContext, ActivityExecutionResult
from .models import ActivityInstance, ActivityRecord, ActivityStatus, ExecutionMode
from ..core.events import AgentEvent, EventProvenance
from ..core.scheduler import ScheduledEvent, ScheduleRecord, ScheduleStatus


TICK_EVENT_TYPE = "INTERNAL.activity_tick"
# 持久化事件协议标识，不是 Python 模块路径；保留原值以兼容已有计划。
TICK_SOURCE = "activity.runtime"


# noinspection PyNestedDecorators
class ActivityTick(BaseModel):
    """tick 的严格 payload；修订定位输入快照，间隔随计划保存。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: StrictStr
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    interval_seconds: Annotated[float, Field(ge=0.001, le=86400, allow_inf_nan=False)]

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("角色 ID 不能为空，也不能包含首尾空白")
        return value

    @field_validator("interval_seconds", mode="before")
    @classmethod
    def validate_interval(cls, value: Any) -> Any:
        if type(value) not in (int, float):
            raise ValueError("推进间隔必须是数值，不能是布尔值或字符串")
        return value


def parse_activity_tick(event: AgentEvent) -> ActivityTick:
    """核对内部事件信封，持久化计划真实性由活动服务及业务提交规则检查。"""
    if (
        event.event_type != TICK_EVENT_TYPE or event.source != TICK_SOURCE
        or event.provenance != EventProvenance.OBSERVED or event.activity_id is None
    ):
        raise ValueError("不是有效的活动时钟事件")
    return ActivityTick.model_validate(event.payload)


def build_activity_tick(record: ActivityRecord, *, interval_seconds: float = 5.0) -> ScheduledEvent:
    """为运行中的模拟活动建立下一次单次计划，末次缩短至剩余时长。"""
    record = ActivityRecord.model_validate(record.model_dump())
    instance, definition = record.instance, record.definition
    if instance.mode != ExecutionMode.SIMULATED or instance.status != ActivityStatus.RUNNING:
        raise ValueError("只有运行中的模拟活动才能安排自动推进")
    if instance.progress is None or definition.estimated_duration_seconds is None:
        raise ValueError("自动推进需要明确的模拟进度和时长")
    tick = ActivityTick(
        agent_id=instance.agent_id, expected_revision=instance.revision,
        interval_seconds=interval_seconds,
    )
    remaining = (1.0 - instance.progress) * definition.estimated_duration_seconds
    # datetime 的最小精度为微秒，确保下一次时间严格递增。
    delay = max(0.000001, min(tick.interval_seconds, remaining))
    due_at = instance.updated_at + timedelta(seconds=delay)
    identity = json.dumps(
        ["persona.activity.tick", instance.agent_id, instance.activity_id, instance.revision],
        ensure_ascii=False, separators=(",", ":"),
    )
    identifier = f"activity-tick:{uuid5(NAMESPACE_URL, identity)}"
    event = AgentEvent(
        event_id=identifier, event_type=TICK_EVENT_TYPE, timestamp=due_at,
        source=TICK_SOURCE, provenance=EventProvenance.OBSERVED,
        activity_id=instance.activity_id, correlation_id=instance.activity_id,
        payload=tick.model_dump(mode="json"),
    )
    return ScheduledEvent(
        schedule_id=identifier, agent_id=instance.agent_id, event=event, due_at=due_at,
    )


def build_tick_exhaustion_result(
    record: ActivityRecord, failed_plan: ScheduleRecord,
) -> tuple[ActivityContext, ActivityExecutionResult]:
    """将当前修订的耗尽事实转为失败候选，不调用执行器或发放完成效果。"""
    record = ActivityRecord.model_validate(record.model_dump())
    failed_plan = ScheduleRecord.model_validate(failed_plan.model_dump())
    tick = parse_activity_tick(failed_plan.schedule.event)
    if failed_plan.status != ScheduleStatus.FAILED or not failed_plan.last_error:
        raise ValueError("只有带失败原因的耗尽计划才能使活动失败")
    if failed_plan.schedule != build_activity_tick(record, interval_seconds=tick.interval_seconds):
        raise ValueError("耗尽计划不对应当前运行活动快照")
    # 使用最后一次失败确认时间，重启恢复不刷新既有事实的时间。
    now = failed_plan.next_attempt_at
    if now < failed_plan.schedule.due_at:
        raise ValueError("失败确认时间不能早于原计划到期时间")
    context = ActivityContext(definition=record.definition, instance=record.instance, now=now)
    instance = ActivityInstance(**{
        **record.instance.model_dump(), "status": ActivityStatus.FAILED,
        "updated_at": context.now, "revision": record.instance.revision + 1,
    })
    identity = json.dumps(
        ["persona.activity.tick_exhausted", failed_plan.schedule.schedule_id,
         instance.agent_id, instance.activity_id, instance.revision],
        ensure_ascii=False, separators=(",", ":"),
    )
    event = AgentEvent(
        event_id=str(uuid5(NAMESPACE_URL, identity)), event_type="ACTIVITY.failed",
        timestamp=context.now, source="activity.service", provenance=EventProvenance.SIMULATED,
        activity_id=instance.activity_id, correlation_id=instance.activity_id,
        causation_id=failed_plan.schedule.event.event_id,
        payload={
            "agent_id": instance.agent_id, "definition_id": instance.definition_id,
            "mode": instance.mode.value, "status": instance.status.value,
            "progress": instance.progress, "revision": instance.revision,
            "reason": "tick_attempts_exhausted", "schedule_id": failed_plan.schedule.schedule_id,
            "attempts": failed_plan.attempts, "last_error": failed_plan.last_error,
        },
    )
    return context, ActivityExecutionResult(instance=instance, events=(event,))
