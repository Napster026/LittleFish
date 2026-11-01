"""活动定义与实例模型；执行、状态迁移和持久化由后续组件负责。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any

from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator,
)

from ..persona.needs import NeedDelta


class ExecutionMode(str, Enum):
    """活动执行方式，模式声明不代表执行器已经实现。"""

    SIMULATED = "simulated"
    REAL = "real"
    HYBRID = "hybrid"


class ActivityStatus(str, Enum):
    """单次活动的生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


DurationValue = Annotated[float, Field(gt=0.0, allow_inf_nan=False)]
ProgressValue = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


def _validate_text(value: str) -> str:
    if not value.strip() or value != value.strip():
        raise ValueError("文本不能为空，也不能包含首尾空白")
    return value


def _validate_optional_number(value: Any) -> Any:
    if value is not None and type(value) not in (int, float):
        raise ValueError("数值必须是整数、浮点数或 None")
    return value


# noinspection PyNestedDecorators
class ActivityDefinition(BaseModel):
    """一种活动的规则，不保存某次活动的状态。

    预计时长可以未知，也不能作为真实活动已经完成的证据。
    completion_needs_delta 是完成效果的规则模板，省略表示没有需求效果。
    此模型不会自动发事件或结算收益；定义与执行器的实际能力检查后续实现。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    definition_id: StrictStr
    name: StrictStr
    supported_modes: tuple[ExecutionMode, ...] = Field(min_length=1)
    estimated_duration_seconds: DurationValue | None = None
    completion_needs_delta: NeedDelta | None = None

    @field_validator("definition_id", "name")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _validate_text(value)

    @field_validator("supported_modes", mode="before")
    @classmethod
    def validate_mode_sequence(cls, value: Any) -> Any:
        if type(value) not in (list, tuple):
            raise ValueError("supported_modes 必须是列表或元组")
        return value

    @field_validator("supported_modes")
    @classmethod
    def validate_unique_modes(cls, value: tuple[ExecutionMode, ...]) -> tuple[ExecutionMode, ...]:
        if len(set(value)) != len(value):
            raise ValueError("supported_modes 不能包含重复模式")
        return value

    @field_validator("estimated_duration_seconds", mode="before")
    @classmethod
    def validate_duration_input(cls, value: Any) -> Any:
        return _validate_optional_number(value)


# noinspection PyNestedDecorators
class ActivityInstance(BaseModel):
    """一次具体活动的只读状态数据。

    progress=None 表示进度未知；量化进度使用 0～1。待开始状态只能是
    未知或 0，完成状态必须是 1。模型不验证历史状态迁移，也不推断执行事实。
    仅凭实例数据不能检查某定义支持哪些模式，应通过创建函数完成关联检查。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    activity_id: StrictStr
    definition_id: StrictStr
    agent_id: StrictStr
    mode: ExecutionMode
    created_at: AwareDatetime
    updated_at: AwareDatetime
    status: ActivityStatus = ActivityStatus.PENDING
    progress: ProgressValue | None = None
    stage: StrictStr | None = None
    revision: Annotated[int, Field(strict=True, ge=0)] = 0

    @field_validator("activity_id", "definition_id", "agent_id", "stage")
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        return None if value is None else _validate_text(value)

    @field_validator("progress", mode="before")
    @classmethod
    def validate_progress_input(cls, value: Any) -> Any:
        return _validate_optional_number(value)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def parse_time(cls, value: Any) -> datetime:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            return value
        raise ValueError("活动时间必须是带时区的 datetime 或 ISO 时间字符串")

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_consistency(self) -> ActivityInstance:
        if self.updated_at < self.created_at:
            raise ValueError("活动更新时间不能早于创建时间")
        if self.status == ActivityStatus.PENDING and self.progress not in (None, 0.0):
            raise ValueError("待开始活动的进度只能是未知或 0")
        if self.status == ActivityStatus.COMPLETED and self.progress != 1.0:
            raise ValueError("已完成活动的进度必须为 1")
        return self


class ActivityRecord(BaseModel):
    """持久化的一次活动：固定定义快照与当前实例。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    definition: ActivityDefinition
    instance: ActivityInstance

    @model_validator(mode="after")
    def validate_association(self) -> ActivityRecord:
        if self.definition.definition_id != self.instance.definition_id:
            raise ValueError("活动记录的定义与实例不匹配")
        if self.instance.mode not in self.definition.supported_modes:
            raise ValueError("活动记录的定义不支持实例模式")
        return self


def create_activity_instance(
    definition: ActivityDefinition,
    *,
    activity_id: str,
    agent_id: str,
    mode: ExecutionMode,
    created_at: datetime,
) -> ActivityInstance:
    """根据定义创建待开始实例，不启动执行器，不写数据库。

    模拟实例初始进度为 0；真实和混合实例初始进度未知。身份由调用方
    分配，唯一性与角色存在性由后续管理及存储组件保证。
    """
    definition = ActivityDefinition.model_validate(definition.model_dump())
    instance = ActivityInstance(
        activity_id=activity_id,
        definition_id=definition.definition_id,
        agent_id=agent_id,
        mode=mode,
        created_at=created_at,
        updated_at=created_at,
    )
    if instance.mode not in definition.supported_modes:
        raise ValueError("活动定义不支持所选执行模式")
    if instance.mode == ExecutionMode.SIMULATED:
        # 重新构造以校验，不用跳过校验的复制接口。
        return ActivityInstance(**{**instance.model_dump(), "progress": 0.0})
    return instance
