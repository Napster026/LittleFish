"""角色需求与可选情绪快照，状态变化通过统一提案结算。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, field_validator, model_validator

from persona.needs import NeedState, parse_need_time
from persona.emotion import EmotionState


# noinspection PyNestedDecorators
class AgentState(BaseModel):
    """一个角色在某次结算后的状态。

    version 是该角色的状态修订号，不是插件版本或协议版本。初始为 0，
    后续由结算器在成功提交时递增；本模型不自动递增或执行并发检查。
    updated_at 表示本次状态的更新时间，必须显式提供，不在加载时重置。
    needs_updated_at 是独立的自然需求时间游标；None 表示尚未建立基线。
    emotion 为可选情绪状态，含独立游标与规则；None 表示未显式启用。
    模型只负责校验和序列化，不写数据库，也不计算需求变化。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: StrictStr
    needs: NeedState
    updated_at: AwareDatetime
    version: Annotated[int, Field(strict=True, ge=0)] = 0
    needs_updated_at: AwareDatetime | None = None
    emotion: EmotionState | None = None

    @field_validator("needs_updated_at", mode="before")
    @classmethod
    def parse_needs_updated_at(cls, value: Any) -> datetime | None:
        return parse_need_time(value)

    @model_validator(mode="after")
    def validate_needs_time(self) -> AgentState:
        if self.needs_updated_at is not None and self.needs_updated_at > self.updated_at:
            raise ValueError("需求时间游标不能晚于角色更新时间")
        if self.emotion is not None and self.emotion.updated_at > self.updated_at:
            raise ValueError("情绪时间游标不能晚于角色更新时间")
        return self

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        """保留精确身份，拒绝空白或带首尾空格的角色 ID。"""
        if not value.strip() or value != value.strip():
            raise ValueError("agent_id 不能为空，也不能包含首尾空白")
        return value

    @field_validator("updated_at", mode="before")
    @classmethod
    def parse_updated_at(cls, value: Any) -> datetime:
        """接受 datetime 或 ISO 时间字符串，拒绝 Unix 数字时间戳。"""
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, datetime):
            return value
        raise ValueError("updated_at 必须是带时区的 datetime 或 ISO 8601 字符串")

    @field_validator("updated_at")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        """通过时区校验后，统一转换为 UTC。"""
        return value.astimezone(timezone.utc)
