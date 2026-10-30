"""最小角色状态模型，后续按功能逐步加入人格、情绪、活动等状态。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, field_validator

from persona.needs import NeedState


# noinspection PyNestedDecorators
class AgentState(BaseModel):
    """一个角色在某次结算后的状态。

    version 是该角色的状态修订号，不是插件版本或协议版本。初始为 0，
    后续由结算器在成功提交时递增；本模型不自动递增或执行并发检查。
    updated_at 表示本次状态的更新时间，必须显式提供，不在加载时重置。
    模型只负责校验和序列化，不写数据库，也不计算需求变化。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: StrictStr
    needs: NeedState
    updated_at: AwareDatetime
    version: Annotated[int, Field(strict=True, ge=0)] = 0

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
