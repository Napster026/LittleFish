"""可插拔活动执行器协议、能力声明及输入输出模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, StrictBool, field_validator, model_validator,
)

from activity.models import ActivityDefinition, ActivityInstance
from core.events import AgentEvent


class ActivityExecutionError(ValueError):
    """当前执行模式、配置或状态不允许执行此操作。"""


class InvalidActivityStatusError(ActivityExecutionError):
    """活动生命周期状态不允许当前操作。"""


class ExecutorCapabilities(BaseModel):
    """执行器显式声明的可选能力，调用方不能默认所有执行器都支持。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    supports_pause: StrictBool = False
    supports_resume: StrictBool = False
    supports_snapshot: StrictBool = False


# noinspection PyNestedDecorators
class ActivityContext(BaseModel):
    """一次执行调用使用的定义、实例与当前时间；时间由调用方提供。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: ActivityDefinition
    instance: ActivityInstance
    now: AwareDatetime

    @field_validator("now", mode="before")
    @classmethod
    def parse_time(cls, value: Any) -> datetime:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            return value
        raise ValueError("now 必须是带时区的 datetime 或 ISO 时间字符串")

    @field_validator("now")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_association(self) -> ActivityContext:
        if self.instance.definition_id != self.definition.definition_id:
            raise ValueError("活动实例与定义不匹配")
        if self.instance.mode not in self.definition.supported_modes:
            raise ValueError("活动定义不支持实例的执行模式")
        if self.now < self.instance.updated_at:
            raise ValueError("执行时间不能早于活动更新时间")
        return self


class ActivityExecutionResult(BaseModel):
    """候选新实例与反馈事件；返回结果不代表已经持久化或结算收益。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance: ActivityInstance
    events: tuple[AgentEvent, ...] = ()

    @model_validator(mode="after")
    def validate_event_association(self) -> ActivityExecutionResult:
        for event in self.events:
            if event.activity_id != self.instance.activity_id:
                raise ValueError("反馈事件关联的活动实例不一致")
            if event.payload.get("agent_id", self.instance.agent_id) != self.instance.agent_id:
                raise ValueError("反馈事件关联的角色不一致")
        return self


@runtime_checkable
class ActivityExecutor(Protocol):
    """各模式共用生命周期接口，不规定统一的进度计算算法。"""

    @property
    def capabilities(self) -> ExecutorCapabilities: ...

    async def start(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def step(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def observe(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def pause(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def resume(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def stop(self, context: ActivityContext) -> ActivityExecutionResult: ...
    async def snapshot(self, context: ActivityContext) -> ActivityInstance: ...
